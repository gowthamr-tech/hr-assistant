# Orchestration and tools

The orchestration layer is **code-driven**: the LLM proposes a plan, and code checks and executes it. The
model never calls a tool itself. There is no function-calling loop.

## 1. Orchestration

### 1.1 Request classification: `Orchestrator._interpret`

LLM call 1 uses `ROUTER_SYSTEM` and `ROUTER_SCHEMA` (`app/llm/prompts.py`) and returns a `Plan`:

| Field | Values / meaning |
|---|---|
| `category` | `hr_question`, `leave_data`, `leave_action`, `greeting`, `off_topic`, `manipulation`, `bulk_request`, `personal_data_request` |
| `needs_documents`, `search_query`, `reference_date` | inputs for RAG |
| `reads[]` | `{tool, target, leave_type, status, start_date, end_date}`; `tool` is one of the three read tools |
| `action` | `{type: none / apply_leave / cancel_leave, target, leave_type, start_date, end_date, dates_explicit_year, request_id, match_start_date, match_end_date}` |
| `date_problem` | `""`, `missing`, `ambiguous`, `past` |
| `needs_clarification`, `clarification_question` | the question text is **not** shown to the user (see below) |

**What the interpreter sees:**
- the requester's ID, role and region (no name)
- `guards.date_context(today)`: today, tomorrow, this week, next week, "next <weekday>", and the next 42
  days
- the pending proposal
- the last 6 turns, with names of employees the requester may see mapped to IDs and all others replaced
  by `[name]` (`NameRegistry.replace_names`)

### 1.2 Route selection: `Orchestrator._dispatch`

Checked in this order:

1. A pending proposal exists and `is_confirmation` → `_execute_pending`. A bare `is_rejection` →
   `clarify` (`PROPOSAL_DISCARDED`). Anything else: the proposal is already discarded, so continue.
2. Regex guards → `refuse`: `is_manipulation` (`MANIPULATION`), `is_bulk_request` (`BULK`),
   `is_personal_data_request` (`PERSONAL`). These run before any LLM call.
3. Interpreter categories: `manipulation`, `bulk_request`, `personal_data_request`, `off_topic` →
   `refuse`; `greeting` → `clarify` (`GREETING`).
4. `leave_action` → `_propose_apply` or `_propose_cancel`.
5. `leave_data` with no reads and `needs_clarification` → `clarify` with the fixed
   `CLARIFY_EMPLOYEE_OR_PERIOD` text (the model's own wording could echo names or IDs).
6. Everything else → `_answer` (reads and/or documents → `tool`, `rag`, `hybrid` or `no_answer`).

`_enforce_route_contract` makes the final result follow the route rules, e.g. `rag` without citations
becomes `no_answer`.

### 1.3 Argument validation and authorisation

| Check | Where |
|---|---|
| The plan matches the schema (types and enums) | `llm/client.py::schema_violation` → `LLMError` → `clarify` |
| Read targets resolve only to visible employees | `_resolve_target` |
| Permission before calling a read tool | `_prepare_reads` → `can_view_leave_of`, `can_use_team_leave` |
| At most one other employee per message | `_prepare_reads` |
| Dates are strict `YYYY-MM-DD` | `leave_tools._parse_date` (orchestrator `_iso` reuses it) |
| The leave type is in the user's own words | `guards.leave_type_stated` + `_current_leave_thread` |
| Writes only for the requester | `_names_someone_else` (plan target, other IDs or known names in the message) → `SELF_ONLY` |
| Permission inside every tool | each tool in `leave_tools.py` (§3) |

### 1.4 Confirmation

- A proposal is a `ChatResult.proposal = (tool_name, args)`. `handle` stores it in
  `assistant_pending_actions` **in the same transaction as the turn, after the response is built**. It is
  skipped if the HTTP layer has already timed out (`cancelled` event).
- At most one proposal per session (the session ID is the primary key).
- It is consumed by `take_pending` at the start of the next turn, whatever the message is.
- Execution re-validates against the current data; "don't ask me to confirm" changes nothing, because
  every write goes through a proposal.

### 1.5 Response assembly

- Tool-only answers come from templates in `responses.py` (`read_answer`, `apply_proposal`,
  `apply_done`, `cancel_proposal`, `cancel_done`, `rejection`).
- Document and hybrid answers come from `RagEngine.compose`.
- `ToolResult.as_call()` produces each `tool_calls` entry.
- `scrub_answer` runs last over the answer and the arguments.

### 1.6 Error handling

`LLMError` → `clarify` (`LLM_UNAVAILABLE`). Any other exception in a turn is logged as `request failed`
(type only) → `clarify`. A database error during execution → tool status `error` with "nothing was
changed". Details: [END_TO_END_FLOW.md §6](END_TO_END_FLOW.md#workflow-6-failure-handling).

## 2. ToolContext and ToolResult

```python
@dataclass
class ToolContext:            # app/tools/leave_tools.py
    conn: sqlite3.Connection
    requester: Employee       # loaded from the DB for X-Employee-Id
    today: date               # APP_TODAY or the system date
    docs: dict[str, DocMeta]
    holidays_by_doc: dict[str, list[date]]
    holiday_names: dict[tuple[str, date], str]

@dataclass
class ToolResult:
    name: str; args: dict; status: str  # "success" | "error"
    data: dict; error: str; error_code: str
```

## 3. The five tools

All five are implemented in `app/tools/leave_tools.py` and covered by `tests/unit/test_tools.py`.

### 3.1 `get_leave_balance(ctx, employee_id: str, leave_type: Optional[str] = None)`

| | |
|---|---|
| Purpose | The current remaining balance of one employee (no future accrual). |
| Validation | `leave_type` ∈ {`annual`, `sick`} if given (`invalid_leave_type`). |
| Authorisation | `can_view_leave_of(requester, target)`: self, the target's manager (`target.manager_id == requester.employee_id`, role `manager`), or any `hr`. Otherwise, and for unknown IDs: `forbidden` with `EMPLOYEE_REFUSAL`. |
| DB | `get_employee`, `get_balances` (SELECT only). |
| Returns | `data = {employee: {employee_id, full_name, email (masked)}, balances: {type: days}, is_self}`; no phone. |
| Args echoed | `{"employee_id": ...}` plus `leave_type` if given. |
| Status | Complete. |

### 3.2 `list_leave_requests(ctx, employee_id: str, status: Optional[str] = None)`

| | |
|---|---|
| Purpose | One employee's leave requests, optionally filtered by status. |
| Validation | `status` ∈ {`pending`, `approved`, `cancelled`} (`invalid_status`). |
| Authorisation | Same as `get_leave_balance`. |
| DB | `list_requests` (SELECT, ordered by start date). |
| Returns | `data = {employee, is_self, requests: [{request_id, leave_type, start_date, end_date, working_days, status}]}`. |
| Status | Complete. |

### 3.3 `get_team_leave(ctx, start_date: str, end_date: str)`

| | |
|---|---|
| Purpose | Pending and approved leave of the requester's direct reports overlapping the range. |
| Validation | Strict ISO dates (`invalid_date`); end ≥ start (`invalid_range`). |
| Authorisation | `can_use_team_leave`: role `manager` only (`forbidden` otherwise). Only rows whose `manager_id` = the requester. |
| DB | `get_direct_reports`, `active_requests_overlapping` (`start_date <= end AND end_date >= start`). |
| Returns | `data = {team_size, leave: [{employee_id, full_name, email (masked), request fields…}]}`. |
| Status | Complete. |

### 3.4 `apply_leave(ctx, leave_type: str, start_date: str, end_date: str, dry_run: bool = False)`

| | |
|---|---|
| Purpose | Create a pending leave request **for the requester**; there is no employee argument. |
| Validation (`_validate_apply`, in order) | type ∈ {annual, sick} → strict ISO dates → both in 2026 (`outside_year`) → start ≥ today (`start_in_past`) → end ≥ start (`invalid_range`) → working days > 0 (`zero_working_days`) → no date overlap with own pending/approved requests (`overlap`) → balance row exists (`no_balance`) → days ≤ balance (`insufficient_balance`). |
| Working days | `count_working_days`: Monday–Friday days in the range that aren't holidays from `choose_calendars` (readable, region or GLOBAL, effective ≤ max(today, start), listing dates in the leave year; then scope, then recency). |
| DB | `dry_run=True`: reads only. Otherwise one `write_transaction`: re-validate, `insert_request` (UUID4 ID, status `pending`, `working_days`), `adjust_balance(-days)`. |
| Returns | `data = {working_days, excluded_holidays, balance_before, balance_after[, request_id, status]}`. |
| Args echoed | `{leave_type, start_date, end_date}` in canonical ISO form. |
| Status | Complete. |

### 3.5 `cancel_leave(ctx, request_id: str, dry_run: bool = False)`

| | |
|---|---|
| Purpose | Cancel one of the requester's own requests. |
| Validation (`_validate_cancel`) | Request exists **and** belongs to the requester (else `not_found`, `REQUEST_NOT_FOUND`, the same text for both cases); status ∈ {pending, approved} (`not_cancellable`); `start_date` > today (`already_started`). |
| DB | One `write_transaction`: re-validate, `set_request_status` (a conditional UPDATE from pending/approved), `adjust_balance(+working_days)`. A lost race gives `conflict`. |
| Returns | `data = {request, restored_days, balance_after}`. |
| Status | Complete. |

## 4. Adding a tool

See [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md#how-to-add-a-new-tool).
