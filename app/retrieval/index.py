"""Hybrid retrieval: BM25 (lexical) + dense embeddings, fused with reciprocal rank fusion.

Access control happens before scoring: `search` only ever scores chunks whose
doc_id is in `allowed_doc_ids`, so a restricted chunk can never be returned,
whatever the query.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Iterable, Optional, Protocol

from app.ingestion.models import Chunk

log = logging.getLogger(__name__)

_STOP = set("""a an the and or of to in on for is are was were be been do does did i me my we our you your
it its this that these those what which who whom how when where why can could should would will shall may might
must with by from at as about into than then there here have has had get gets got am any some""".split())


def _stem(tok: str) -> str:
    for suf in ("ings", "ing", "ied", "ies", "ed", "es", "s"):
        if tok.endswith(suf) and len(tok) - len(suf) >= 3:
            base = tok[: -len(suf)]
            return base + "y" if suf in ("ied", "ies") else base
    return tok


def tokenize(text: str) -> list[str]:
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [_stem(t) for t in toks if t not in _STOP]


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class FastEmbedder:
    """Local CPU embedding model (ONNX via fastembed). No network at query time."""

    def __init__(self, model_name: str) -> None:
        import os

        from fastembed import TextEmbedding

        cache_dir = os.environ.get("EMBEDDING_CACHE_DIR") or None
        try:  # the Docker image bakes the model in, so never reach out to the network at start-up
            self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir, local_files_only=bool(cache_dir))
        except TypeError:
            self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(map(float, v)) for v in self._model.embed(texts)]


@dataclass
class ScoredChunk:
    chunk: Chunk
    score: float
    lexical: float
    dense: float


class HybridIndex:
    K1, B, RRF_K = 1.4, 0.75, 60
    LEXICAL_RATIO = 0.3
    DENSE_FLOOR, DENSE_MARGIN = 0.55, 0.12

    def __init__(self, chunks: list[Chunk], titles: dict[str, str], embedder: Optional[Embedder] = None) -> None:
        self.chunks = chunks
        self._titles = titles
        self._docs_tokens = [tokenize(f"{titles.get(c.doc_id, '')} {c.text}") for c in chunks]
        self._tf = [Counter(toks) for toks in self._docs_tokens]
        self._len = [len(t) for t in self._docs_tokens]
        self._embedder = embedder
        self._vectors: Optional[list[list[float]]] = None
        if embedder is not None and chunks:
            try:
                vecs = embedder.embed([f"{titles.get(c.doc_id, '')}\n{c.text}" for c in chunks])
                self._vectors = [_normalise(v) for v in vecs]
            except Exception:
                log.warning("embedding failed; using lexical retrieval only", exc_info=True)
                self._vectors = None

    @property
    def dense_enabled(self) -> bool:
        return self._vectors is not None

    def _bm25(self, query_tokens: list[str], i: int, idf: dict[str, float], avg: float) -> float:
        tf, dl = self._tf[i], self._len[i]
        score = 0.0
        for t in query_tokens:
            f = tf.get(t, 0)
            if not f:
                continue
            score += idf.get(t, 0.0) * f * (self.K1 + 1) / (f + self.K1 * (1 - self.B + self.B * dl / avg))
        return score

    def _stats(self, candidates: list[int]) -> tuple[dict[str, float], float]:
        """IDF and average length over the requester's allowed chunks only, so restricted
        documents cannot influence which allowed chunks are ranked or cut off."""
        df: Counter = Counter()
        for i in candidates:
            df.update(self._tf[i].keys())
        n = len(candidates)
        idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}
        avg = (sum(self._len[i] for i in candidates) / n) if n else 1.0
        return idf, avg or 1.0

    def search(self, queries: Iterable[str], allowed_doc_ids: set[str], limit: int = 40) -> list[ScoredChunk]:
        queries = [q for q in queries if q and q.strip()]
        candidates = [i for i, c in enumerate(self.chunks) if c.doc_id in allowed_doc_ids]
        if not candidates or not queries:
            return []
        lexical: dict[int, float] = defaultdict(float)
        dense: dict[int, float] = defaultdict(float)
        idf, avg = self._stats(candidates)
        for q in queries:
            qt = tokenize(q)
            for i in candidates:
                lexical[i] = max(lexical[i], self._bm25(qt, i, idf, avg))
        if self._vectors is not None and self._embedder is not None:
            try:
                qvecs = [_normalise(v) for v in self._embedder.embed(queries)]
                for i in candidates:
                    dense[i] = max(sum(a * b for a, b in zip(qv, self._vectors[i], strict=True)) for qv in qvecs)
            except Exception:
                log.warning("query embedding failed; lexical only", exc_info=True)
                dense.clear()

        lex_rank = {i: r for r, i in enumerate(sorted(candidates, key=lambda i: -lexical[i]))}
        dense_rank = {i: r for r, i in enumerate(sorted(candidates, key=lambda i: -dense[i]))} if dense else {}
        # Relative cut-offs: keep chunks reasonably close to the best match on either signal, so
        # passages that only share generic words ("days", "year") are not sent to the LLM.
        top_lex = max(lexical.values(), default=0.0)
        top_dense = max(dense.values(), default=0.0)
        results = []
        for i in candidates:
            lex_ok = lexical[i] > 0 and lexical[i] >= self.LEXICAL_RATIO * top_lex
            dense_ok = bool(dense) and dense[i] >= max(self.DENSE_FLOOR, top_dense - self.DENSE_MARGIN)
            if not (lex_ok or dense_ok):
                continue
            score = 1.0 / (self.RRF_K + lex_rank[i]) if lexical[i] > 0 else 0.0
            if dense:
                score += 1.0 / (self.RRF_K + dense_rank[i])
            results.append(ScoredChunk(self.chunks[i], score, lexical[i], dense.get(i, 0.0)))
        results.sort(key=lambda s: (-s.score, s.chunk.chunk_id))
        return results[:limit]


def _normalise(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]
