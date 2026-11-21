# Developer guide

## 1. Repository layout

```
app/
  main.py               # FastAPI app, lifespan, initialise()
  config.py             # Settings, load_settings(), get_settings()
  state.py              # STATE: ready flag, kb, orchestrator
  logging_setup.py      # JSON logs + PiiScrubFilter
  api/routes.py         # /chat, /health, / (UI)
  static/index.html     # optional test UI
  db/database.py        # connect, write_transaction, verify_and_migrate
  db/repositories.py    # all SQL
  ingestion/            # models, extractors, chunker, holidays, pipeline
  retrieval/index.py    # HybridIndex, FastEmbedder, tokenize
  policy/               # eligibility, precedence, calendar
  llm/                  # client (providers, budget), prompts (system prompts + schemas), tokens
  orchestration/        # orchestrator, rag, guards, responses
  security/             # access (role table), pii (masking/scrubbing)
  tools/leave_tools.py  # the five tools
tests/                  # see TESTING_GUIDE.md
Dockerfile, docker-compose.yml, docker-entrypoint.sh, Makefile, requirements*.txt, .env.example
README.md, DESIGN.md (assessment deliverables), docs/
```

## 2. Main execution path

`uvicorn app.main:app` → `lifespan` → `setup_logging` → background `initialise()` → `STATE.ready`.

Per request: `routes.chat` → `Orchestrator.handle` → LangGraph `self.graph.invoke` →
(`execute_pending` | `guards` → `interpret` → `ticket_agent` / `leave_propose` / `leave_read` /
`policy_agent`) → `route_contract` → storage (in `handle`) → JSON response.

## 3. Configuration

All settings come from environment variables, loaded once by `config.get_settings()` (cached).

| Variable | Default | Used for |
|---|---|---|
| `CORPUS_DIR`, `MANIFEST_PATH`, `DB_PATH` | `./data/...` | inputs |
| `APP_TODAY` | system date | "today" for every date rule (`Settings.today()`) |
| `LLM_PROVIDER` | `anthropic` | `anthropic` / `openai` / `gemini` (or `google`) / `ollama` (`fake` is accepted for tests) |
| `LLM_MODEL`, `LLM_ROUTER_MODEL` | per provider (`_DEFAULT_MODELS`) | main model; interpreter model |
| `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GOOGLE_API_KEY` / `GEMINI_API_KEY` | — | credentials |
| `ANTHROPIC_EFFORT`, `ANTHROPIC_REFUSAL_FALLBACK` | `low`, `true` | Anthropic request options |
| `GOOGLE_GENAI_USE_VERTEXAI`, `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` | —, —, `global` | Vertex AI (with a project set: gcloud credentials, no key) |
| `GEMINI_FALLBACK_MODELS`, `GEMINI_THINKING_LEVEL` | `gemini-3.5-flash,gemini-2.5-flash`, `low` | Gemini resilience and speed |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | local open-source models |
| `LLM_TIMEOUT_S`, `LLM_MAX_RETRIES`, `REQUEST_DEADLINE_S` | `20`, `1`, `27` | time limits (raising the deadline above 27 also raises the HTTP guard) |
| `EMBEDDINGS_ENABLED`, `EMBEDDING_MODEL`, `EMBEDDING_CACHE_DIR` | `true`, `BAAI/bge-small-en-v1.5`, — | dense retrieval |
| `LOG_LEVEL`, `LOG_FILE` | `INFO`, — | logging |

## 4. Tracing a request through the code

1. Find the `request_id` in the response, and grep the logs for it
   ([LOGGING_AND_DEBUGGING.md](LOGGING_AND_DEBUGGING.md)).
2. `plan` shows the interpreter's decision; `llm call` shows each LLM call; `precedence resolved` shows
   the document decisions; `chat handled` shows the outcome.
3. To step through a request in a debugger, reproduce it in a test with `chat_factory`. It runs the real
   orchestrator with a scripted LLM, so you can put breakpoints in the graph's node methods (`Orchestrator._node_*`) or edge functions
   (`_route_plan`, `_route_after_reads`).

## 5. How to…

### Add a supported document format
1. Write `extract_<fmt>(path) -> list[Block]` in `app/ingestion/extractors.py`. Emit `Block("heading",
   text)`, `Block("text", text)` or `Block("table", rows=[header, ...])`; drop the metadata table with
   `is_metadata_table`.
2. Add the suffix branch in `pipeline.extract_document` (set `paginated=True` if you can give pages).
3. Add a fixture file in `tests/fixtures.py::build_corpus` and assertions in `tests/unit/test_ingestion.py`.

### Change retrieval behaviour
- Scoring and cut-offs: `HybridIndex` (`K1`, `B`, `RRF_K`, `LEXICAL_RATIO`, `DENSE_FLOOR`,
  `DENSE_MARGIN`).
- Context selection: `RagEngine.select_sources`, `MAX_CHUNKS`, `MAX_CHUNKS_PER_DOC`.
- Chunk size: `chunker.MAX_CHUNK_TOKENS`.
- Keep the eligibility filter **before** `search`; never filter afterwards.
  `test_restricted_documents_never_reach_the_llm` will catch a mistake here.

### Add a new tool
1. Implement it in `app/tools/leave_tools.py`: take `ToolContext`, check permission first (add the rule to
   `app/security/access.py`), and return `_ok` / `_err`. Wrap writes in `write_transaction` and
   re-validate inside it.
2. Expose it to the interpreter: add the name to the `reads` tool enum in `ROUTER_SCHEMA`, describe it in
   `ROUTER_SYSTEM`, and add it to `Plan.from_json`'s accepted names.
3. Route it: `_prepare_reads` (permission pre-check) and `_run_read`; or, for a write, a `_propose_*`
   method returning `ChatResult(proposal=...)` plus a branch in `_execute_pending`.
4. Add an answer template in `responses.py` and tests in `tests/unit/test_tools.py` and
   `tests/integration/test_chat_api.py`.

The five required tool names and arguments are fixed by the assessment contract; don't rename them.

### Add an authorisation rule
Put the rule in `app/security/access.py`, call it both in the tool and in the orchestrator's pre-check,
and add rows to `test_balance_permissions`-style parametrised tests. Never put a rule only in a prompt.

### Add a test
Unit tests take `conn`, `kb` and `settings` from `tests/conftest.py`. Chat tests use
`chat_factory({"substring": plan(...)})` and `chat.send(emp, message, session=...)`; route rules are
checked automatically.

## 6. Common mistakes and tips

- **Sending corpus text outside `corpus_blocks`.** It would bypass the budget and the logs. Only pass
  document-derived text through `complete_json(corpus_blocks=...)`.
- **Putting corpus facts in prompts or code.** The assessment forbids hand-written corpus facts; prompts
  must stay generic.
- **Logging user text.** Log IDs and counts only; the scrubber is a safety net, not a licence.
- **Comparing dates as strings without canonical form.** Always go through `_parse_date` / `_canonical`.
- **Forgetting the pending-proposal semantics.** Any message consumes the proposal (`take_pending` runs
  first).
- **Python 3.11 locally.** The Docker image is 3.11. The `.venv` was created with 3.11; the system
  `python3` on the dev machine is 3.14.
- **`data/veltrix.db` gets modified** by local runs; reset it from the assessment zip when needed.
