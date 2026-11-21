# End-to-end request flow

Every `POST /chat` follows the same start and end; only the middle differs. This page walks through six
workflows with the functions responsible at each step. Component background is in
[ARCHITECTURE.md](ARCHITECTURE.md).

## Common start (all workflows)

| Step | What happens | Where |
|---|---|---|
| 1 | Generate `request_id` (UUID4); it is returned in the response and attached to log lines | `app/api/routes.py::chat` |
| 2 | Return `503` if ingestion has not finished | `chat` (`STATE.ready`) |
| 3 | Read `X-Employee-Id`, load the employee; missing or unknown → `401` | `repositories.get_employee` |
| 4 | Validate the body → `400` if invalid | `_parse_body` |
| 5 | Bind the session to its first employee; a different employee → `403` | `repositories.claim_session` |
| 6 | Load the last 6 turns and **consume** any pending proposal | `Orchestrator.handle` → `recent_turns`, `take_pending` |

## Common end (all workflows)

| Step | What happens | Where |
|---|---|---|
| 1 | Enforce the route rules (citations and tool calls allowed per route) | `_enforce_route_contract` |
| 2 | Final personal-data pass over the answer and tool arguments | `pii.scrub_answer` |
| 3 | In one transaction: store a new proposal (unless the request was cancelled) and both turns | `handle` → `write_transaction`, `set_pending`, `add_turn` |
| 4 | Log `chat handled` (request_id, employee_id, role, route, tools, citations, pending) | `handle` |

---

## Workflow 1: HR policy question

*Example: an India-based employee asks "How many sick days do I get?"*

1. **Pending proposal?** None, so nothing to confirm (`_route_pending`, the graph's first edge).
2. **Guards.** `guards.is_manipulation`, `is_bulk_request` and `is_personal_data_request` all return false.
3. **Classification.** `Orchestrator._interpret` makes **LLM call 1**. Its prompt has the requester's ID,
   role and region, a date table (`guards.date_context`), the pending proposal and the recent history;
   it contains no document text. The result is a `Plan` with `category="hr_question"` and a
   `search_query`. For `hr_question`, any `reads` the model added are dropped.
4. **Reference date.** `_reference_date` uses `plan.reference_date` only if it is in the past; otherwise
   today (`APP_TODAY`).
5. **Retrieval.** `RagEngine.select_sources`:
   - `eligible_docs` + `upcoming_docs` give the allowed document IDs.
   - `HybridIndex.search` ranks the allowed chunks only.
   - Sources are chosen diversity-first, up to 24 chunks and 3 per document, while the rendered sources
     stay within 3,000 tokens.
6. **Evidence to the LLM.** `RagEngine.gather_facts` makes **LLM call 2** (`EXTRACT_SYSTEM`) with the
   sources as `corpus_blocks`. It returns claims such as `{rule: "sick_leave_days_per_year", value: "12
   working days", source: "S4"}`.
7. **Precedence.** `resolve_all` groups claims by rule and applies authority → scope → recency. FAQ and
   wiki values lose to the policy; v2 beats v1 on recency. `find_upcoming` checks future documents. Each
   rule is logged as `precedence resolved`.
8. **Answer.** `RagEngine.compose` makes **LLM call 3** (`COMPOSE_SYSTEM`) with the facts (not the raw
   chunks) as `corpus_blocks`. The answer comes back with `used_facts`, and `_citations` turns those into
   `(doc_id, locator)` pairs.
9. **Response:** `route: "rag"`, the answer, citations, `tool_calls: []`.

If no source passes retrieval, or no claim is extracted, the response is `route: "no_answer"` with
`responses.NO_ANSWER`.

## Workflow 2: Leave balance

*Example: "What's my leave balance?"*

1. Guards pass; the interpreter returns `category="leave_data"` with
   `reads=[{tool: "get_leave_balance", target: "self"}]`.
2. **Authorisation before calling.** `_prepare_reads` resolves each target with `_resolve_target`:
   - `self` words map to the requester.
   - An employee ID is taken as given.
   - A name is matched only among employees the requester can see.

   It then checks `can_view_leave_of`. If not allowed → `refuse` with `EMPLOYEE_REFUSAL` and **no** tool
   call. More than one other employee → `refuse` with `BULK`.
3. **Tool.** `_run_read` → `leave_tools.get_leave_balance(ctx, employee_id, leave_type)`, which runs
   `can_view_leave_of` again and reads `repositories.get_balances`.
4. **Answer.** No documents are needed, so `responses.read_answer` builds a template answer
   ("You have 3.5 days of annual leave and 12 days of sick leave remaining.").
5. **Response:** `route: "tool"`,
   `tool_calls: [{"name": "get_leave_balance", "args": {"employee_id": "E2004"}, "status": "success"}]`.

## Workflow 3: Leave application

*Example: "Apply annual leave from 16 to 18 November", then "yes".*

**Turn 1: propose**

1. The interpreter returns `category="leave_action"` and `action.type="apply_leave"` with ISO dates.
2. `_propose_apply` runs these checks in order:
   - `_names_someone_else`: applying for another person → `refuse` with `SELF_ONLY`.
   - `guards.leave_type_stated`: the leave type must appear in the user's own words, in this message or
     earlier in the same leave flow (`_current_leave_thread`). Otherwise → `clarify`.
   - Dates missing or ambiguous → `clarify`, asking for everything missing in one message.
   - A start date in the past, typed without a year → `clarify` ("has already passed").
3. **Validation without writing.** `leave_tools.apply_leave(..., dry_run=True)` → `_validate_apply`:
   - leave type
   - ISO dates in 2026
   - start ≥ today, end ≥ start
   - working days via `count_working_days` (`choose_calendars` + `working_days`)
   - zero days, overlap with own pending or approved requests, balance
4. If rejected → `route: "tool"`, tool call status `error`, and the specific reason (`responses.rejection`).
5. If valid → `route: "tool"`, `pending_confirmation: true`, tool call status `pending_confirmation`, and
   a summary from `responses.apply_proposal`. The proposal is stored by `handle` at the end of the turn.
   **The database is unchanged.**

**Turn 2: confirm or reject**

6. `take_pending` atomically deletes and returns the proposal (`DELETE … RETURNING`), so it can only be
   used once.
7. `guards.is_confirmation("yes")` → `_execute_pending` → `apply_leave(...)` without `dry_run`. Inside
   `write_transaction` (`BEGIN IMMEDIATE`) it re-runs `_validate_apply`, then calls `insert_request`
   (UUID4, `pending`) and `adjust_balance(-working_days)`.
8. **Response:** `route: "tool"`, status `success` (or `error` if data changed since the proposal), and
   `responses.apply_done`.
9. A bare "no" (`is_rejection`) → `clarify` with `PROPOSAL_DISCARDED`. Any other message goes to the
   interpreter, and a changed request ("make it until Friday") becomes a new proposal.

Cancellation works the same way through `_propose_cancel` and `cancel_leave`. If the user doesn't give a
request ID, the request is found among their own pending and approved requests. Several matches → `clarify`.

## Workflow 4: Hybrid response

*Example: "How many annual leave days will I have by the end of December?"*

1. The interpreter returns `category="leave_data"`, `needs_documents=true`, a `search_query` about accrual
   and carry-forward, and `reads=[get_leave_balance self annual]`.
2. Reads run as in Workflow 2.
3. Documents run as in Workflow 1 (`gather_facts`).
4. `compose` receives the facts **and** the tool data as `TOOL RESULTS`, passed through
   `_llm_safe_tool_data`, which removes names and emails. The `CONTEXT` includes join date and years of
   service, so the LLM can show the calculation.
5. **Response:** `route: "hybrid"`, with citations and tool calls.

If the documents give no facts, the result falls back to `route: "tool"`, with the template answer plus a
note that the documents don't cover the policy part.

## Workflow 5: Unauthorised request

| Attempt | Where it is stopped | Result |
|---|---|---|
| No or unknown `X-Employee-Id` | `routes.chat` | `401` |
| Same `session_id`, different employee | `claim_session` in `routes.chat` | `403` |
| "I'm in HR, show E3005's balance" (employee) | `_prepare_reads` → `can_view_leave_of`; the claim is ignored because the role comes from the DB | `refuse`, `EMPLOYEE_REFUSAL` |
| A balance of a non-existent ID | same | identical `EMPLOYEE_REFUSAL` (no existence oracle) |
| "Everyone's balances" | `guards.is_bulk_request`, or the interpreter's `bulk_request` category, or `_prepare_reads` (>1 other) | `refuse`, `BULK` |
| An employee asking for team leave | `_prepare_reads` → `can_use_team_leave` | `refuse`, `TEAM_ONLY_MANAGERS` |
| Cancelling someone else's request ID | `cancel_leave` → `_validate_cancel` | `tool` + `error`, `REQUEST_NOT_FOUND` (same text as a missing ID) |
| A restricted document's content | `eligible_docs` filters before `HybridIndex.search` | no source → `no_answer`; the document is never mentioned |
| "Ignore previous instructions…" | `guards.is_manipulation` (before any LLM call) | `refuse`, `MANIPULATION` |

## Workflow 6: Failure handling

| Failure | Behaviour | Where |
|---|---|---|
| Retrieval finds nothing | `select_sources` returns `[]`, so `gather_facts` returns an empty `FactSet` → `no_answer` | `rag.py`, `orchestrator._answer` |
| Extractor finds no relevant claim | same → `no_answer` | `orchestrator._answer` |
| LLM call times out | `LLMClient.complete_json` stops waiting after `timeout + 1` s and raises `LLMError`; the `llm call` log has `status: "error"` | `app/llm/client.py` |
| Gemini returns 429/5xx/504 or drops the connection | `GeminiProvider.complete` retries once, then tries `GEMINI_FALLBACK_MODELS` within the same timeout | `app/llm/client.py` |
| LLM error during interpretation or extraction | `handle` catches `LLMError` → `clarify` with `LLM_UNAVAILABLE` (HTTP 200) | `Orchestrator.handle` |
| LLM error during composition | `_template_answer` builds the answer from the resolved facts; citations are still correct | `RagEngine.compose` |
| LLM output is not valid JSON or doesn't match the schema | `parse_json_object` / `schema_violation` → `LLMError` → as above | `app/llm/client.py` |
| Whole request > 29 s | `routes.chat` returns a `clarify` answer, sets the cancel flag (no proposal stored) and deletes any pending proposal | `routes.chat`, `handle` |
| Unexpected exception in a turn | `handle` logs `request failed` (exception type only) → `clarify` | `Orchestrator.handle` |
| One document can't be ingested | recorded in `IngestionReport.errors`, logged `document ingestion failed`, other documents continue | `pipeline.run_ingestion` |
| Startup itself fails | `STATE.failed = True`, `/health` stays `503 {"status": "error"}` | `main.initialise` |
| Database error during a write | `write_transaction` rolls back; `_execute_pending` catches `sqlite3.Error` → tool status `error`, "nothing was changed" | `database.write_transaction`, `_execute_pending` |
