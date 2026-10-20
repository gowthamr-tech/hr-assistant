# Security and privacy

Every control below is enforced in **code**. Prompts also tell the LLM to behave (e.g. "ignore identity
claims"), but no control on this page depends on the LLM obeying.

## 1. Controls and their enforcement points

### 1.1 Employee identity
`routes.chat` → `repositories.get_employee(conn, X-Employee-Id)`. That database row is the only source of
`role`, `region` and `manager_id`. Nothing from the message changes it. The interpreter sees the role and
region from the database, and its plan cannot widen what code allows.

### 1.2 Role-based document access
`security/access.py::readable_access_levels`:

| Role | May read |
|---|---|
| employee | `all` |
| manager | `all`, `managers` |
| hr | `all`, `managers`, `hr` |

Enforced in `policy/eligibility.py::eligible_docs` / `upcoming_docs`, **before** scoring in
`HybridIndex.search(queries, allowed_doc_ids)`. Restricted chunks are never scored, selected, sent to an
LLM or cited, so a restricted document's title and ID never appear in any prompt or answer.

### 1.3 Session ownership
`repositories.claim_session` (`INSERT OR IGNORE` + SELECT): the first employee owns the `session_id`. A
different `X-Employee-Id` → `403`. Pending proposals are stored with `employee_id`, and `take_pending`
returns nothing if it doesn't match.

### 1.4 Leave-data permissions
`security/access.py::can_view_leave_of` and `can_use_team_leave`, checked twice: in
`orchestrator._prepare_reads` (so a refusal has no tool call) and again inside each read tool.

| Role | View | Team leave | Apply / cancel |
|---|---|---|---|
| employee | self | no | self |
| manager | self + direct reports (`manager_id`) | direct reports only | self |
| hr | any one employee at a time | no (role `manager` only) | self |

"One at a time" means more than one other employee in one message → `refuse` (`_prepare_reads`).

### 1.5 Unauthorised access and existence oracles
- Forbidden and non-existent employees get the identical `EMPLOYEE_REFUSAL`, and no tool call.
- Name lookups only search employees the requester can see (`_resolve_target`).
- A foreign request ID and a missing one get the identical `REQUEST_NOT_FOUND`.
- The interpreter's history maps only *visible* names to IDs (`NameRegistry.replace_names(text,
  visible_ids)`), and its free-text clarification is not shown to users.

### 1.6 Prompt injection
- **From documents:** document text only enters the extract and compose calls, inside `<documents>`,
  labelled as data. The interpreter (the only call that can lead to a tool) never sees document text.
  The composer sees only resolved facts. A document therefore can't trigger a tool.
- **From users:** `guards.is_manipulation` refuses before any LLM call (system-prompt requests, "ignore
  instructions", "developer mode", "bypass permissions"…). Identity claims are ignored by construction.
  "Don't ask me to confirm" has no effect, because every write goes through a proposal.

### 1.7 Model-generated tool arguments
These are treated as untrusted input:
- `schema_violation` checks types and enums.
- Strict ISO date parsing (`_parse_date`, which rejects `20261020` and `2026-W43-2`).
- Write tools take no employee argument.
- Read tools re-check permission against `ctx.requester`.
- `_names_someone_else` refuses writes "for" another person.
- `leave_type_stated` discards a leave type the user didn't say.

### 1.8 Personal information (`full_name`, `email`, `phone`)
- **Answers:** templates show names only for visible employees, and emails via `mask_email`
  (`r***@domain`). Phone numbers are never put into results. `scrub_answer` masks employee emails and
  removes phone-like strings in every answer and tool argument (role mailboxes such as `hr@` stay).
- **LLM prompts:** the interpreter gets the ID, role and region (no name); the extractor gets the region
  and level; the composer gets tool data stripped of names and emails (`_llm_safe_tool_data`). Text the
  user types reaches the interpreter as written.
- **Logs:** see §1.12.

### 1.9 Secret management
- API keys are read from the environment (`config.load_settings`, provider env vars).
- `.env` is in `.gitignore` and `.dockerignore`; `.env.example` contains no values.
- Keys are never logged; logs record only the provider and model names.

### 1.10 Database query safety
All SQL is in `app/db/repositories.py` with `?` parameters. The only dynamic SQL is the number of
placeholders in `IN (...)` lists, built from a count, never from input.

### 1.11 Transaction safety
- `database.write_transaction` uses `BEGIN IMMEDIATE`, so check-then-write can't interleave between
  requests.
- `apply_leave` and `cancel_leave` re-validate inside the transaction.
- `set_request_status` is conditional, and `take_pending` uses `DELETE … RETURNING`.
- Turn and proposal storage share one transaction.

### 1.12 Error responses and logging privacy
- Error bodies are fixed strings (`{"detail": …}`), and exceptions inside a turn become a `clarify`
  answer.
- Logs carry IDs, counts and statuses; messages, prompts and tool results are never logged.
- `logging_setup.PiiScrubFilter` replaces known employee names with IDs, masks emails and removes phones
  in every record, including uvicorn's.
- `JsonFormatter` logs only the exception **type**.

## 2. Threat table

| Threat | Existing protection | Implementation | Evidence / test | Remaining gap |
|---|---|---|---|---|
| Impersonation via the message ("I'm HR") | Role from the DB only | `routes.chat`, `can_view_leave_of` | `test_identity_claims_are_ignored` | none known |
| Session hijack | Session bound to first employee | `claim_session` | `test_session_belongs_to_first_employee` | Session IDs can be squatted by guessing; there are no auth tokens (out of scope) |
| Restricted text reaching the LLM | Filter before scoring | `eligible_docs`, `HybridIndex.search` | `test_restricted_documents_never_reach_the_llm` (spies on every call) | none known |
| Restricted document revealed | No source → `no_answer`; nothing named | same | same | none known |
| Viewing others' leave (IDOR) | Pre-check + in-tool check | `_prepare_reads`, tools | `test_balance_permissions`, `test_forbidden_and_unknown_employees_get_identical_refusal` | none known |
| Existence oracle | Identical refusals; visible-only name mapping | `EMPLOYEE_REFUSAL`, `REQUEST_NOT_FOUND`, `replace_names` | `test_not_found_message_is_identical_for_foreign_and_missing`, `test_interpreter_never_learns_ids_of_invisible_people` | Response timing isn't equalised |
| Bulk enumeration | Regex + category + count check | `guards.is_bulk_request`, `_prepare_reads` | `test_bulk_requests_refused_for_every_role`, `test_hr_one_employee_at_a_time` | A user can still ask one by one (allowed by the spec) |
| Document prompt injection | Interpreter never sees documents; facts-only composer | `rag.py`, `prompts.py` | `test_prompt_injection_in_documents_is_not_followed` (fake LLM) | Injected text could still be extracted as a "fact" by a non-compliant LLM; it would then lose on authority (it's in a wiki) |
| User prompt injection | Regex guard + manipulation category | `guards.is_manipulation` | `test_user_manipulation_refused_before_llm` | Regex can be paraphrased around; then the interpreter must classify it |
| Writes without confirmation | Proposal + consume-once | `handle`, `take_pending` | `test_cannot_skip_confirmation`, `test_confirmation_in_another_session_does_nothing` | Proposals never expire (only the next message discards them) |
| Hidden proposal after a timeout | Cancel flag + delete on timeout | `routes.chat`, `handle` | `test_timed_out_request_leaves_no_hidden_proposal` | none known |
| Overdraw / double booking | `BEGIN IMMEDIATE` + revalidation | `apply_leave` | `test_concurrent_applications_cannot_overdraw`, `test_concurrent_overlapping_applications` | Single SQLite file; not designed for multiple hosts |
| Lenient date forms bypassing overlap SQL | Strict regex | `_parse_date` | `test_only_canonical_iso_dates_accepted` | none known |
| Applying for someone else | No employee arg; refuse if another person is named | `_names_someone_else` | `test_cannot_apply_for_someone_else` | May refuse a legitimate message that mentions a colleague ("X will cover for me") |
| PII in logs | Scrub filter + ID-only logging | `PiiScrubFilter` | `test_logs_contain_no_personal_data` | Names typed by the user that aren't employee names aren't scrubbed (by design) |
| PII in answers | Templates + `scrub_answer` | `responses.py`, `pii.py` | `test_answers_and_tool_args_never_show_phone_numbers` | none known |
| Malformed body / DoS by nesting | Size limit, catch-all parse | `_parse_body` | `test_deeply_nested_body_is_400_and_unknown_header_401` | No rate limiting |
| LLM output abuse (wrong types) | Schema validation | `schema_violation` | `test_schema_invalid_llm_output_degrades_to_clarify` | none known |
| Stuck provider call | Hard wall-clock timeout | `LLMClient.complete_json` | `test_stuck_llm_call_cannot_stall_the_request` | The abandoned thread keeps running until the SDK returns |
| Secret leakage | env-only keys, gitignored `.env` | `config.py`, `.gitignore` | manual check | Keys pasted into chats or terminals must be rotated by their owner |
