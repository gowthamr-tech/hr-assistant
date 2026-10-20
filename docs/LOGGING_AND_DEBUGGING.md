# Logging and debugging

## 1. Logging system

| Aspect | Implementation |
|---|---|
| Framework | Python `logging`, configured by `app/logging_setup.py::setup_logging(level, log_file)` (called in `main.lifespan`) |
| Format | One JSON object per line (`JsonFormatter`): `ts`, `level`, `logger`, `msg`, then any `extra` fields |
| Destinations | stdout always; also `LOG_FILE` if set |
| Level | `LOG_LEVEL` (default `INFO`); `httpx`, `anthropic`, `openai`, `google` and `fastembed` are set to `WARNING` |
| Privacy | `PiiScrubFilter` on every handler, including uvicorn's; exceptions are logged as `exc_type` only |
| Correlation | `request_id` (UUID4, generated in `routes.chat`, returned in the response) on every chat-related line |

uvicorn's own startup and access lines (e.g. `"POST /chat HTTP/1.1" 200`) are plain text, not JSON.

## 2. Events

| `msg` | Level | Component | Key fields |
|---|---|---|---|
| `ingestion finished` | INFO | `ingestion/pipeline.py` | `documents`, `chunks`, `ocr_documents`, `failed_documents`, `holidays`, `dense_retrieval`, `seconds` |
| `document ingestion failed` | ERROR | pipeline | `doc_id`, `error` (type) |
| `skipping malformed manifest entry` | ERROR | pipeline | `index`, `error`, `detail` |
| `manifest entry has unknown doc_type; treated as low authority` | WARNING | pipeline | `doc_id`, `doc_type` |
| `calendar document yielded no holiday dates` | WARNING | pipeline | `doc_id` |
| `embedding model unavailable; lexical retrieval only` | WARNING | pipeline | — |
| `embedding failed; using lexical retrieval only` / `query embedding failed; lexical only` | WARNING | `retrieval/index.py` | — |
| `service ready` | INFO | `main.py` | `provider`, `model` |
| `startup failed` | ERROR | `main.py` | `exc_type` |
| `LLM client could not be created; chat will degrade` | ERROR | `main.py` | `exc_type` |
| `plan` | INFO | `orchestrator._interpret` | `request_id`, `category`, `reads`, `action`, `needs_documents` |
| `llm call` | INFO | `llm/client.py` | `event: "llm_call"`, `purpose` (`interpret` / `extract_claims` / `compose_answer`), `provider`, `model`, **`corpus_tokens`**, `latency_ms`, `status`, `request_id` |
| `llm call blocked: corpus budget exceeded` | ERROR | client | `corpus_tokens`, `limit` |
| `gemini model unavailable; retrying` | WARNING | client | `model`, `code` |
| `refusal fallback not accepted; disabling it` | WARNING | client (Anthropic) | — |
| `precedence resolved` | INFO | `rag.gather_facts` | `rule`, `winners`, `losers`, `decided_by`, `conflict` |
| `fact dropped to stay within corpus budget` | WARNING | `rag._fact_blocks` | `rule` |
| `compose failed; using template answer` | WARNING | `rag.compose` | `error`, `cause` |
| `llm unavailable for request` | WARNING | `orchestrator.handle` | `request_id` |
| `request failed` | ERROR | `orchestrator.handle` | `error` (type) |
| `write action failed` | ERROR | `_execute_pending` | `tool`, `exc_type` |
| `chat handled` | INFO | `orchestrator.handle` | `employee_id`, `role`, `route`, `tools` (`name:status`), `citations` (doc IDs), `pending` |
| `session ownership violation` | WARNING | `routes.chat` | `employee_id` |
| `request timed out` | ERROR | `routes.chat` | `request_id` |
| `unhandled error` | ERROR | `routes.chat` | `exc_type` |

Sample request trace (values shortened):

```json
{"ts": "…", "level": "INFO", "logger": "app.orchestration.orchestrator", "msg": "plan", "request_id": "6a1f…", "category": "hr_question", "reads": [], "action": "none", "needs_documents": true}
{"ts": "…", "level": "INFO", "logger": "app.llm.client", "msg": "llm call", "event": "llm_call", "purpose": "interpret", "provider": "gemini", "model": "gemini-3-flash-preview", "corpus_tokens": 0, "latency_ms": 1800, "status": "ok", "request_id": "6a1f…"}
{"ts": "…", "level": "INFO", "logger": "app.llm.client", "msg": "llm call", "event": "llm_call", "purpose": "extract_claims", "corpus_tokens": 1717, "status": "ok", "request_id": "6a1f…"}
{"ts": "…", "level": "INFO", "logger": "app.orchestration.rag", "msg": "precedence resolved", "request_id": "6a1f…", "rule": "sick_leave_days_per_year", "winners": ["POL-LEAVE-GLOBAL-V2"], "losers": ["FAQ-LEAVE", "POL-LEAVE-GLOBAL-V1"], "decided_by": "recency", "conflict": false}
{"ts": "…", "level": "INFO", "logger": "app.orchestration.orchestrator", "msg": "chat handled", "request_id": "6a1f…", "employee_id": "E2004", "role": "employee", "route": "rag", "tools": [], "citations": ["POL-LEAVE-GLOBAL-V2"], "pending": false}
```

## 3. Commands

```bash
# Start
docker compose up --build                     # or: docker compose up -d
set -a && . ./.env && set +a && .venv/bin/python -m uvicorn app.main:app --port 8000   # local

# Logs (Docker service name: assistant)
docker compose logs assistant                 # all
docker compose logs -f assistant              # follow
docker compose logs assistant | grep '"request_id": "<id>"'   # one request

# Readiness
curl -s -w ' %{http_code}\n' localhost:8000/health

# Tests
make test                                     # or: .venv/bin/python -m pytest -q
```

## 4. Diagnosis guide

| Symptom | Check | Typical cause |
|---|---|---|
| `/health` stays `503 {"status":"starting"}` | `ingestion finished` missing; look at the latest lines | OCR or embedding still running (about 10 s normally) |
| `/health` is `503 {"status":"error"}` | `startup failed` + `exc_type` | `DB_PATH` missing tables, or an unreadable `MANIFEST_PATH` |
| Answer is `no_answer` unexpectedly | `plan.needs_documents`; `llm call` with `purpose=extract_claims` missing (no sources) or present with no `precedence resolved` lines (no claims) | The document isn't eligible for that role, region or date; or a wording mismatch; check `ingestion finished.failed_documents` |
| Wrong or missing citation | `precedence resolved` (`winners`, `decided_by`) | The extractor used different rule keys, so the "losing" document became a separate rule |
| `refuse` where you expected data | `chat handled.role`; does the target's `manager_id` match? | Permission rule (expected) or name resolution (`_resolve_target` only searches visible employees) |
| A tool call with `status: error` | the answer text holds the specific reason; `tools` in `chat handled` | Business rule (balance, overlap, past date) |
| `write action failed` | `exc_type` | SQLite locked beyond `busy_timeout`, or a read-only `data/` mount (check `docker-entrypoint.sh` uid) |
| `clarify` "couldn't process that request" | `llm call status=error`, `gemini model unavailable`, `llm unavailable for request`, `request timed out` | Missing or invalid key, provider overload, quota |
| Every LLM call fails at once | `LLM client could not be created` at startup | Wrong `LLM_PROVIDER` or missing credentials |
| `corpus_tokens` near 3,000 / `fact dropped…` | `llm call` lines | Many long matching sections; expected, the budget is enforced |

## 5. Recommended improvements (not implemented)

- Log per-stage timings (retrieval, extraction, composition) on `chat handled`.
- Log the selected source IDs per request (currently only the doc IDs of winning facts appear, through
  `precedence resolved` and the citations).
- Emit uvicorn access logs as JSON.
