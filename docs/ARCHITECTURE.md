# Architecture

This page explains the components of the HR Assistant and how they fit together. For the
step-by-step path of individual requests, see [END_TO_END_FLOW.md](END_TO_END_FLOW.md).

## 1. The big picture

The service is a single FastAPI process with one chat endpoint It does two kinds of work:

- **Answering policy questions** from 30 HR documents (retrieval-augmented generation, "RAG").
- **Reading and changing leave data** in SQLite through five tools.

The central design decision is **"the LLM reads and writes; code decides"**. The LLM is used for three
language tasks: understanding the message, extracting facts from document excerpts, and writing the final
answer. Everything that must be correct or secure is in plain Python:

- who the requester is
- which documents they may read
- which document wins a conflict
- whether a leave request is valid
- what the database contains

<img src="images/architecture-overall.png" alt="architecture-overall" width="100%">

<details><summary>Diagram source (Mermaid)</summary>

```mermaid
flowchart TB
    client([HTTP client / test UI]) -->|POST /chat + X-Employee-Id| routes
    subgraph API["app/api/routes.py"]
        routes["chat"] --> parse["_parse_body"]
        routes --> ident["repositories.get_employee<br/>repositories.claim_session"]
    end
    routes --> orch
    subgraph ORCH["app/orchestration"]
        orch["Orchestrator.handle"] --> guards["guards.py<br/>regex checks"]
        orch --> interp["_interpret<br/>LLM call 1"]
        orch --> rag["RagEngine<br/>rag.py"]
        orch --> resp["responses.py<br/>templates"]
    end
    orch --> tools["app/tools/leave_tools.py<br/>5 tools"]
    tools --> db[(SQLite<br/>veltrix.db)]
    orch --> db
    rag --> elig["app/policy/eligibility.py"]
    rag --> index["app/retrieval/index.py<br/>HybridIndex"]
    rag --> prec["app/policy/precedence.py"]
    rag --> llm["app/llm/client.py<br/>LLMClient"]
    interp --> llm
    llm --> provider{{Anthropic / OpenAI /<br/>Gemini / Ollama}}
    subgraph START["startup: app/main.py::initialise"]
        ingest["app/ingestion/pipeline.py<br/>run_ingestion"] --> index
        ingest --> holidays[(assistant_holidays)]
    end
```

</details>


## 2. Components

### 2.1 HTTP layer: `app/api/routes.py`

| | |
|---|---|
| **Purpose** | Implements `POST /chat`, `GET /health` and the optional test UI at `GET /`. |
| **Input** | Raw HTTP request: `X-Employee-Id` header and a JSON body. |
| **Processing** | `_parse_body` validates the body (size ≤ 64 KB, `session_id` ≤ 200 chars, `message` ≤ 4,000 chars, no lone surrogates). `repositories.get_employee` resolves the requester. `repositories.claim_session` binds the session. The orchestrator then runs in a worker thread (`_HANDLER_POOL`), guarded by `asyncio.wait_for(asyncio.shield(...))`. |
| **Output** | The JSON response from Section 9 of the task, or `400` / `401` / `403` / `503` / `500` with `{"detail": ...}`. |
| **Dependencies** | `app/state.py::STATE` (readiness flag, orchestrator), `app/db`. |
| **Failure scenarios** | Ingestion not finished → `503`. Handler exceeds `_handler_timeout()` (29 s by default) → HTTP 200 `clarify` answer, the request's cancel flag is set and any pending proposal for the session is deleted. Unexpected exception → `500 {"detail": "internal error"}`. |

The header is checked before the body, so a request with both problems gets `401`. Details:
[API_REFERENCE.md](API_REFERENCE.md).

### 2.2 Startup and readiness: `app/main.py`, `app/state.py`

| | |
|---|---|
| **Purpose** | Build everything the service needs, then flip `/health` to `200`. |
| **Input** | `Settings` from `app/config.py::get_settings()` (environment variables). |
| **Processing** | `lifespan` starts `initialise()` in a background thread named `ingestion`. `initialise` calls `verify_and_migrate`, loads employee names and emails into the PII scrubber (`NAMES.load`, `load_known_emails`), runs `run_ingestion`, builds the LLM client with `build_llm_client`, and creates the `Orchestrator`. |
| **Output** | `STATE.ready = True`, or `STATE.failed = True` if anything raised. |
| **Failure scenarios** | Missing assessment tables or a broken manifest → `STATE.failed`, so `/health` stays `503 {"status": "error"}`. An LLM client that can't be built is **not** fatal: the service starts, and chat requests degrade to a `clarify` answer. |

### 2.3 Ingestion: `app/ingestion/`

| | |
|---|---|
| **Purpose** | Turn the corpus into searchable chunks and a holiday table, from scratch at every start. |
| **Input** | `MANIFEST_PATH` (the only source of metadata) and the files in `CORPUS_DIR`. |
| **Processing** | `pipeline.load_manifest` validates each entry into a `DocMeta`. `pipeline.extract_document` calls the format extractor: `extract_pdf` (PyMuPDF; Tesseract OCR for image-only pages), `extract_docx`, `extract_html`, `extract_markdown`, `extract_plain_text`. `chunker.chunk_document` builds one chunk per section. For `calendar` documents, `holidays.extract_holidays` reads the dates. |
| **Output** | A `KnowledgeBase` (docs, chunks, `HybridIndex`, `holidays_by_doc`, `holiday_names`, `IngestionReport`); rows in `assistant_holidays`. |
| **Dependencies** | PyMuPDF, python-docx, BeautifulSoup, pytesseract + the Tesseract binary, tiktoken, fastembed (optional). |
| **Failure scenarios** | A malformed manifest entry is skipped and logged. A missing, corrupt, empty or out-of-folder file is recorded in `report.errors`, and ingestion continues. If the embedding model is unavailable, retrieval falls back to BM25 only. |

Details: [RAG_PIPELINE.md](RAG_PIPELINE.md).

### 2.4 Retrieval: `app/retrieval/index.py`

| | |
|---|---|
| **Purpose** | Score chunks against a query, **only within the documents the requester is allowed to use**. |
| **Input** | Queries plus `allowed_doc_ids`. |
| **Processing** | `HybridIndex.search` computes BM25 (statistics over the allowed chunks only, via `_stats`) and cosine similarity with `FastEmbedder` (BAAI/bge-small-en-v1.5), then fuses the two rankings with reciprocal rank fusion. Relative cut-offs drop weak matches. |
| **Output** | A ranked list of `ScoredChunk`. |
| **Failure scenarios** | Embedding failure → lexical-only, with a warning. No allowed documents → empty list, which leads to `no_answer`. |

### 2.5 Policy rules: `app/policy/`

| Module | Responsibility |
|---|---|
| `eligibility.py` | `eligible_docs` (role, region and date filter) and `upcoming_docs` (readable and for the region, but not yet effective). |
| `precedence.py` | `resolve_rule` applies authority → scope → recency; `disagree` / `same_value` decide whether values conflict; `find_upcoming` finds upcoming changes. |
| `calendar.py` | `choose_calendars` picks the applicable holiday calendar; `working_days` counts Monday–Friday days that aren't holidays. |

These are pure functions with no I/O, which is why they are unit-tested directly
(`tests/unit/test_precedence.py`, `tests/unit/test_working_days.py`).

### 2.6 LLM access: `app/llm/`

| | |
|---|---|
| **Purpose** | One interface (`LLMClient.complete_json`) over four providers, with the corpus-token budget enforced. |
| **Input** | System prompt, user text, a JSON schema, and `corpus_blocks` (the only channel for document-derived text). |
| **Processing** | Counts the tokens of `corpus_blocks` with tiktoken `cl100k_base` (`tokens.count_tokens`). If they exceed 3,000 it raises `CorpusBudgetExceeded` without sending. Otherwise it wraps the blocks in `<documents>…</documents>`, calls the provider in `_LLM_POOL` with a hard wall-clock limit, parses the JSON (`parse_json_object`) and validates it against the schema (`schema_violation`). |
| **Output** | A `dict` matching the schema, or `LLMError`. |
| **Providers** | `AnthropicProvider` (structured output via `output_config.format`, `effort`, optional refusal fallback); `OpenAIProvider` (also used for `ollama` through `OLLAMA_BASE_URL`); `GeminiProvider` (AI Studio key, Vertex express key, or Vertex with gcloud credentials; retries plus fallback models). |
| **Failure scenarios** | Timeout, refusal, truncated output, invalid JSON or a schema mismatch all raise `LLMError`, and callers degrade (see [END_TO_END_FLOW.md §6](END_TO_END_FLOW.md#workflow-6-failure-handling)). |

### 2.7 Orchestration: `app/orchestration/`

| Module | Responsibility |
|---|---|
| `orchestrator.py` | `Orchestrator.handle` runs one turn through a LangGraph `StateGraph` (`build_graph`). Nodes call the agents; edge functions (`_route_pending`, `_route_plan`, `_route_after_reads`) pick the path; `route_contract` (`_enforce_route_contract`) keeps the route rules. Persistence stays in `handle`. |
| `rag.py` | `RagEngine.select_sources`, `gather_facts`, `compose`. |
| `guards.py` | Regex checks: `is_manipulation`, `is_bulk_request`, `is_personal_data_request`, `is_confirmation`, `is_rejection`, `leave_type_stated`; plus `date_context` for date resolution. |
| `responses.py` | Fixed messages (`EMPLOYEE_REFUSAL` lives in `security/access.py`) and templates for tool answers. |

Details: [AGENT_AND_TOOLS.md](AGENT_AND_TOOLS.md).

### 2.8 Tools: `app/tools/leave_tools.py`

`get_leave_balance`, `list_leave_requests`, `get_team_leave`, `apply_leave`, `cancel_leave`. Each one takes
a `ToolContext` (connection, requester, today, documents and holidays) and returns a `ToolResult`. Their
permission checks are inside the tool. Details: [AGENT_AND_TOOLS.md](AGENT_AND_TOOLS.md#3-the-five-tools).

### 2.9 Data access: `app/db/`

`database.py` handles connections, `write_transaction` (`BEGIN IMMEDIATE`) and the added `assistant_*`
tables. `repositories.py` holds every SQL statement, all parameterised. Details:
[DATABASE_AND_DATA_MODEL.md](DATABASE_AND_DATA_MODEL.md).

### 2.10 Security and logging: `app/security/`, `app/logging_setup.py`

`access.py` holds the role table. `pii.py` does email masking and name/email/phone scrubbing.
`logging_setup.py` provides JSON logs with `PiiScrubFilter`. Details:
[SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md) and [LOGGING_AND_DEBUGGING.md](LOGGING_AND_DEBUGGING.md).

## 3. Diagrams

### 3.1 Document ingestion and indexing

<img src="images/architecture-ingestion.png" alt="architecture-ingestion" width="100%">

<details><summary>Diagram source (Mermaid)</summary>

```mermaid
flowchart LR
    A["manifest.json"] -->|load_manifest| B["DocMeta list"]
    B --> C{file suffix}
    C -->|.pdf| D["extract_pdf"]
    C -->|.docx| E["extract_docx"]
    C -->|.html/.htm| F["extract_html"]
    C -->|.md| G["extract_markdown"]
    C -->|.txt| H["extract_plain_text"]
    D -->|page with no text + image| OCR["tesseract_ocr 300 dpi"]
    D --> I[Blocks: heading / text / table]
    E --> I[Blocks: heading / text / table]
    F --> I[Blocks: heading / text / table]
    G --> I[Blocks: heading / text / table]
    H --> I[Blocks: heading / text / table]
    OCR --> I[Blocks: heading / text / table]
    I --> J["chunk_document<br/>one chunk per section"]
    I -->|doc_type == calendar| K["extract_holidays"]
    J --> L["HybridIndex<br/>BM25 + bge-small vectors"]
    K --> M[(assistant_holidays)]
    K --> N["holidays_by_doc in memory"]
```

</details>


### 3.2 RAG retrieval and response generation

<img src="images/architecture-rag.png" alt="architecture-rag" width="560">

<details><summary>Diagram source (Mermaid)</summary>

```mermaid
flowchart TD
    Q["question + search_query"] --> E1["eligible_docs + upcoming_docs<br/>role , region , date"]
    E1 --> S["HybridIndex.search<br/>allowed_doc_ids only"]
    S --> SEL["select_sources<br/>diversity first, <= 3,000 tokens"]
    SEL -->|no sources| NA["no_answer"]
    SEL --> X["LLM call 2: extract claims<br/>EXTRACT_SCHEMA"]
    X --> R["resolve_all<br/>authority then scope then recency"]
    X --> U["find_upcoming"]
    R --> F[Facts: current / conflict / upcoming]
    U --> F[Facts: current / conflict / upcoming]
    F -->|no facts| NA
    F --> C["LLM call 3: compose<br/>COMPOSE_SCHEMA"]
    C -->|LLMError| T["_template_answer"]
    C --> CIT[_citations from used facts]
    T --> CIT[_citations from used facts]
```

</details>


### 3.3 Agentic tool execution

<img src="images/architecture-tools.png" alt="architecture-tools" width="560">

<details><summary>Diagram source (Mermaid)</summary>

```mermaid
flowchart TD
    M["message"] --> G{guards}
    G -->|manipulation / bulk / personal| RF["refuse"]
    G --> P["LLM call 1: interpreter then Plan"]
    P -->|reads| PR["_prepare_reads<br/>_resolve_target + can_view_leave_of"]
    PR -->|not allowed| RF2["refuse: EMPLOYEE_REFUSAL"]
    PR -->|several other employees| RF3["refuse: BULK"]
    PR --> RT["_run_read then tool"]
    RT --> TOOL["get_leave_balance / list_leave_requests / get_team_leave<br/>permission re-checked inside"]
    TOOL --> D{needs documents?}
    D -->|no| TR["route tool + template answer"]
    D -->|yes| H["RAG + compose with tool data then hybrid"]
```

</details>


The LLM never calls a tool directly. It returns a plan (`ROUTER_SCHEMA`), code checks the plan, and code
calls the tool. Document text never reaches the interpreter, so a document can't trigger a tool.

### 3.4 Leave application and confirmation

<img src="images/architecture-leave-confirmation.png" alt="architecture-leave-confirmation" width="100%">

<details><summary>Diagram source (Mermaid)</summary>

```mermaid
sequenceDiagram
    participant U as User
    participant O as Orchestrator
    participant T as apply_leave
    participant DB as SQLite
    U->>O: "Apply annual leave 16-18 Nov"
    O->>O: interpreter plan + leave_type_stated + date checks
    O->>T: apply_leave(..., dry_run=True)
    T->>DB: read balance, overlaps (no writes)
    T-->>O: success (working_days, balance_after) or error
    O->>DB: set_pending + add_turn (one transaction)
    O-->>U: route tool, pending_confirmation true
    U->>O: "yes"
    O->>DB: take_pending (DELETE ... RETURNING)
    O->>T: apply_leave(...)
    T->>DB: BEGIN IMMEDIATE, re-validate, INSERT request, UPDATE balance, COMMIT
    T-->>O: success or error
    O-->>U: route tool, status success/error
```

</details>


## 4. How the parts interact during one request

1. FastAPI receives the request on the event loop; `chat` hands the work to a thread from `_HANDLER_POOL`.
2. That thread opens **one SQLite connection** (`database.connection`), shared by identity lookup, session
   handling, tools and turn storage.
3. `Orchestrator.handle` makes 0–3 LLM calls through `LLMClient`. Each call runs on another thread
   (`_LLM_POOL`) so it can be abandoned on timeout.
4. The `KnowledgeBase` (index, chunks, holidays) is read-only shared memory, built once at startup.
5. The turn ends with one write transaction that stores the turn history and, for proposals, the pending
   action.
