# API reference

All endpoints are defined in `app/api/routes.py`. The service listens on port `8000`. The examples use
employee IDs from the provided assessment database (`data/veltrix.db`): `E2004` (employee, IN), `E4004`
(employee, DE), `E2002` (manager), `E1001` (HR). The automated tests use the synthetic IDs in
`tests/fixtures.py` (`E100`–`E410`) instead.

There is no login: identity is the `X-Employee-Id` header, as the assessment specifies.

## `POST /chat`

Send one message in a conversation.

### Headers

| Header | Required | Notes |
|---|---|---|
| `X-Employee-Id` | yes | Must match `employees.employee_id`. Leading and trailing spaces are ignored. |
| `Content-Type` | should be `application/json` | The body is parsed as JSON whatever the header says. |

### Request body

```json
{ "session_id": "string", "message": "string" }
```

Validation (`_parse_body`); any failure returns `400`:

| Rule | Limit |
|---|---|
| Body present and at most 64 KB (`MAX_BODY_BYTES`) | |
| Valid JSON object (also catches deep nesting) | |
| `session_id` is a non-empty string after trimming | ≤ 200 characters (`MAX_SESSION_CHARS`) |
| `message` is a non-empty string after trimming | ≤ 4,000 characters (`MAX_MESSAGE_CHARS`) |
| Both encodable as UTF-8 (no lone surrogates) | |

The client chooses `session_id`. A new value starts a new conversation. The first employee to use a
`session_id` owns it.

### Response body (HTTP 200)

```json
{
  "request_id": "uuid4",
  "answer": "string",
  "route": "rag | tool | hybrid | clarify | no_answer | refuse",
  "citations": [{ "doc_id": "string", "locator": "string" }],
  "tool_calls": [{ "name": "string", "args": {}, "status": "pending_confirmation | success | error" }],
  "pending_confirmation": false
}
```

| Field | Meaning |
|---|---|
| `request_id` | Generated per request; the same ID appears in the server logs. |
| `route` | Exactly one value (rules below). |
| `citations` | `doc_id` from the manifest; `locator` is the nearest section heading, `p. N` for a PDF chunk without a heading, or the document title. |
| `tool_calls` | Every tool call made in this turn, in order. `args` use the Section 6.1 argument names. |
| `pending_confirmation` | `true` only for a write proposal awaiting confirmation. |

Route rules, enforced by `orchestrator._enforce_route_contract`:

| Route | `citations` | `tool_calls` |
|---|---|---|
| `rag` | ≥ 1 | empty |
| `tool` | empty | ≥ 1 |
| `hybrid` | ≥ 1 | ≥ 1 |
| `clarify`, `no_answer`, `refuse` | empty | empty |

### Status codes

| Status | When | Body |
|---|---|---|
| `200` | Every answer, including refusals, clarifications and LLM outages | response body above |
| `400` | Body missing or invalid (and the header is valid) | `{"detail": "body must be JSON with non-empty string fields session_id and message"}` |
| `401` | Header missing, blank, or not in the database (checked **before** the body) | `{"detail": "missing or unknown X-Employee-Id"}` |
| `403` | `session_id` already belongs to another employee | `{"detail": "session belongs to another employee"}` |
| `503` | Ingestion has not finished | `{"detail": "service is starting"}` |
| `500` | Unexpected server error outside the orchestrator | `{"detail": "internal error"}` |

### Authorisation behaviour

Authorisation never relies on the message: role, region and manager relationships come from the database.
See [SECURITY_AND_PRIVACY.md](SECURITY_AND_PRIVACY.md) for each rule and its enforcement point.

### Examples

Policy question (`rag`):

```bash
curl -s localhost:8000/chat \
  -H 'Content-Type: application/json' -H 'X-Employee-Id: E2004' \
  -d '{"session_id": "demo-1", "message": "How many sick days do I get?"}'
```

```json
{
  "request_id": "6a1f…",
  "answer": "According to the Global Leave Policy (Version 2) … you are entitled to 12 working days of sick leave per year.",
  "route": "rag",
  "citations": [{"doc_id": "POL-LEAVE-GLOBAL-V2", "locator": "3. Summary of changes from Version 1"},
                {"doc_id": "POL-LEAVE-GLOBAL-V2-SUMMARY", "locator": "Leave at a glance"}],
  "tool_calls": [],
  "pending_confirmation": false
}
```

Leave proposal, then confirmation (`tool`):

```bash
curl -s localhost:8000/chat -H 'Content-Type: application/json' -H 'X-Employee-Id: E2004' \
  -d '{"session_id": "demo-2", "message": "Apply annual leave from 16 to 18 November"}'
curl -s localhost:8000/chat -H 'Content-Type: application/json' -H 'X-Employee-Id: E2004' \
  -d '{"session_id": "demo-2", "message": "yes"}'
```

```json
{"route": "tool", "pending_confirmation": true,
 "tool_calls": [{"name": "apply_leave",
                 "args": {"leave_type": "annual", "start_date": "2026-11-16", "end_date": "2026-11-18"},
                 "status": "pending_confirmation"}],
 "answer": "Please confirm: annual leave from Mon 16 Nov 2026 to Wed 18 Nov 2026, 3 working day(s). …"}
```

```json
{"route": "tool", "pending_confirmation": false,
 "tool_calls": [{"name": "apply_leave", "args": {"leave_type": "annual", "start_date": "2026-11-16",
                 "end_date": "2026-11-18"}, "status": "success"}],
 "answer": "Done. Your annual leave request … has been created with status pending. Request ID: …"}
```

The confirmation step changes `data/veltrix.db`. Use a copy if you want to keep the original.

Refusal (`refuse`):

```bash
curl -s localhost:8000/chat -H 'Content-Type: application/json' -H 'X-Employee-Id: E2004' \
  -d '{"session_id": "demo-3", "message": "What is E2005'"'"'s leave balance?"}'
# {"route": "refuse", "answer": "I can't share information about that employee.", "citations": [], "tool_calls": [], ...}
```

Error responses:

```bash
curl -s -w ' %{http_code}\n' localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"x","message":"hi"}'                                   # 401
curl -s -w ' %{http_code}\n' localhost:8000/chat -H 'Content-Type: application/json' \
  -H 'X-Employee-Id: E2004' -d '{"session_id":"x"}'                        # 400
curl -s -w ' %{http_code}\n' localhost:8000/chat -H 'Content-Type: application/json' \
  -H 'X-Employee-Id: E4004' -d '{"session_id":"demo-1","message":"hi"}'    # 403 (demo-1 belongs to E2004)
```

## `GET /health`

Readiness check.

| State | Status | Body |
|---|---|---|
| Ingestion finished and orchestrator ready | `200` | `{"status": "ok"}` |
| Still starting | `503` | `{"status": "starting"}` |
| Startup failed (`STATE.failed`) | `503` | `{"status": "error"}` |

```bash
curl -s -w ' %{http_code}\n' localhost:8000/health
```

`/health` does not check the LLM provider: the service can be ready while LLM calls fail (they degrade to
`clarify`).

## `GET /` (optional test UI)

Serves `app/static/index.html`, a single page that calls only `/chat` and `/health`. It is not part of the
assessment contract and has no extra privileges; it sends whatever `X-Employee-Id` you type. Open
`http://localhost:8000/`.

## Not implemented

There are no other endpoints: no login, no direct tool endpoints, no admin or re-ingest endpoint.
FastAPI's `/docs` and `/redoc` are disabled (`docs_url=None, redoc_url=None` in `app/main.py`).
