# Testing guide

## 1. Overview

- **Framework:** pytest (`requirements-dev.txt`), configured in `pytest.ini` (`testpaths = tests`,
  `-p no:cacheprovider`, DeprecationWarnings ignored).
- **Size:** 206 collected tests, from 136 test functions (several are parametrised).
- **No API key needed:** every LLM call goes to a scripted fake (`tests/fakes.py`).
- **No dependence on `./data`:** tests build their own synthetic corpus and database
  (`tests/fixtures.py`). One test also ingests the real corpus when `./data` exists.

```
tests/
├── conftest.py            # session corpus + KB, per-test DB copy, make_settings, TODAY = 2026-10-16
├── fakes.py               # FakeProvider, ScriptedRouter, plan(), fake_extract, fake_compose
├── fixtures.py            # build_corpus (15 docs: md/docx/html/pdf/scanned pdf), build_database (9 employees)
├── unit/
│   ├── test_working_days.py      # 12 functions
│   ├── test_precedence.py        # 19
│   ├── test_tools.py             # 18
│   ├── test_ingestion.py         # 14
│   └── test_guards_and_pii.py    # 13
├── integration/
│   ├── conftest.py               # chat_factory fixture, Chat helper, check_contract
│   └── test_chat_api.py          # 39
└── security/
    ├── test_access_and_pii.py    # 4
    ├── test_token_budget.py      # 4
    └── test_review_regressions.py  # 13 (fixes from the adversarial review)
```

## 2. Fixtures and mocks

| Fixture / helper | Scope | What it provides |
|---|---|---|
| `corpus_root` | session | A temp folder with `build_corpus` + `build_database` + `verify_and_migrate` |
| `kb` | session | `run_ingestion` over the fixture corpus (no embeddings; OCR if Tesseract is installed) |
| `db_path`, `conn` | function | **A fresh copy of the fixture DB per test**, so mutations never leak between tests |
| `settings` | function | `make_settings`: `llm_provider="fake"`, `APP_TODAY=2026-10-16`, embeddings off |
| `chat_factory` | function | Calls `main.initialise` with a fake LLM and the shared `kb`, returns a `Chat` wrapper over `TestClient`; every response passes through `check_contract` (route rules) |
| `plan(...)` | helper | Builds a full `ROUTER_SCHEMA`-shaped plan |
| `ScriptedRouter` | helper | Maps message substrings to plans (stands in for LLM call 1) |
| `fake_extract` / `fake_compose` | helpers | Deterministic stand-ins for calls 2 and 3, driven by regexes over the source text |
| `FakeProvider.calls` | spy | Records every system and user prompt, used to assert what an LLM would have seen |

**Fixture people** (synthetic): `E100` hr; `E200` manager of `E201`, `E202`; `E203` reports to `E300`;
`E300` manager of `E301`, `E203`; `E410` manager of `E400` (DE).

**Fixture documents:**
- the Section 5.2 volunteer-day example (`VOL-V1/V2/FAQ/DE/V3`)
- calendars for IN, DE and US, including a restricted-holiday table
- restricted `MGR-HB` and `HR-COMP` with secret marker phrases
- an injection wiki
- a scanned memo
- a conflicting expense policy and handbook

## 3. What each file covers

| File | Covers |
|---|---|
| `unit/test_working_days.py` | Inclusive counting, weekends, holidays, ranges spanning both, end before start, calendar choice (region, recency, leave year), restricted holidays stay working days |
| `unit/test_precedence.py` | All three worked-example cases; authority, scope, recency; unresolved conflict; agreement isn't conflict; per-band values; wording vs negation; upcoming changes; eligibility per role, region and date |
| `unit/test_tools.py` | Permission matrix (10 cases); no phone in results; team-leave rules and overlap edges; apply success, dry run, every rejection code; concurrency (no overdraw, no double booking); cancel rules and balance restoration |
| `unit/test_ingestion.py` | Every format; OCR; heading locators; header, footer and title removal; metadata tables; DOCX tables; holiday extraction; date parsing; table split with repeated header; malformed manifest; bad files; real corpus (skipped if `./data` or Tesseract is missing) |
| `unit/test_guards_and_pii.py` | Confirm / reject phrases; manipulation and bulk regexes (including false-positive checks); leave-type detection; date table; masking and scrubbing |
| `integration/test_chat_api.py` | HTTP codes; health readiness; session ownership; RAG precedence end to end; historical; conflict; scanned memo by scope; `no_answer`; off-topic; injection; manipulation before the LLM; identity claims; LLM outage; every role's reads; bulk; team leave; personal data; hybrid; the whole confirmation workflow (propose, confirm, change, discard, "no", skip attempt, cross-session, revalidation); cancel flows; modified corpus; UI page |
| `security/test_access_and_pii.py` | Restricted text never in any prompt (per role); no corpus text in system prompts; no names, emails or phones in the log file; no phone in responses |
| `security/test_token_budget.py` | Client refuses over budget without sending; cl100k counting; selection under 3,000 on a large corpus; every call logged with `corpus_tokens` |
| `security/test_review_regressions.py` | Strict ISO dates; no hidden proposal after a timeout; deep JSON → 400/401; lone surrogate → 400; schema-invalid LLM output; apply for someone else; "No, make it Friday"; leave type from the same flow only; `unanswered` counted as corpus; no name→ID oracle; stuck LLM call cut off |

There are no end-to-end tests against a real LLM. Live checks were done manually (see
[TEST_SCENARIOS.md](TEST_SCENARIOS.md)).

## 4. Running tests

```bash
make test                                          # whole suite
.venv/bin/python -m pytest -q                      # same, without make
.venv/bin/python -m pytest tests/unit/test_tools.py -q                        # one file
.venv/bin/python -m pytest "tests/unit/test_tools.py::test_cancel_rejections" -q   # one test (all params)
.venv/bin/python -m pytest -k "confirm" -q         # by name
docker compose run --rm --no-deps --entrypoint "" assistant python -m pytest -q   # inside the image
```

`REAL_DATA_DIR` points `test_real_corpus_ingests_completely` at another data folder (default `data`).

## 5. Diagnosing a failure

1. Re-run with `-x -vv` to stop at the first failure and see full asserts.
2. For integration tests, print `chat.provider.calls` to see exactly what each fake LLM call received.
3. Check whether the `ScriptedRouter` key matches the message: the first matching substring wins, and
   the default is `off_topic`.
4. OCR-dependent tests skip without `tesseract` on PATH; install it (`brew install tesseract` /
   `apt-get install tesseract-ocr`).
5. `check_contract` failures mean a route rule was broken: read the route and the field counts in the
   assertion.

## 6. Not covered by automated tests

- Real LLM behaviour: rule-key consistency, answer wording, classification accuracy.
- Provider adapters against live APIs (Anthropic, OpenAI and Gemini request shapes).
- `docker compose up` startup time and the entrypoint's uid switching.
- Dense retrieval quality (tests run with embeddings disabled).
- Latency under the 30 s limit with real providers.
- Concurrent requests through HTTP (concurrency is tested at the tool level).

## 7. Proposed test-case matrix (not implemented)

| Area | Proposed case | Type |
|---|---|---|
| API validation | `Content-Type` other than JSON with a valid body (accepted today; decide if intended) | integration |
| Ingestion | Encrypted or password-protected PDF; DOCX with nested tables; HTML with `<th>`-only header columns | unit |
| RAG accuracy | Golden set of ~30 questions over the real corpus with expected values and doc_ids, run against a live model | live eval |
| Precedence | Two region-specific documents with different region sets (`["IN","US"]` vs `["IN"]`) on the same date | unit |
| Citations | Locator is the heading for a chunk continued across a PDF page break | unit |
| Authorisation | Manager whose report is also an HR employee; HR viewing a manager | unit |
| Prompt injection | Live-model run where the injected wiki text is the only source on a topic | live eval |
| Leave application | Leave spanning a holiday and the last day of 2026; balance with fractions (e.g. 0.5 left) | unit |
| Leave cancellation | Cancel an `approved` request starting tomorrow; cancel when the balance row is missing | unit |
| Confirmation | Confirmation message with trailing emoji or other languages | unit |
| Concurrency | 20 parallel `/chat` confirmations over HTTP for the same employee | integration |
| Token limits | Single section larger than 3,000 tokens; facts larger than 3,000 tokens | unit |
| Personal data | Answer composed by a live model never contains another employee's name | live eval |
| Docker | `docker compose up` on a clean machine reaches `/health` 200 in under 10 min (CI job) | e2e |
| Modified corpus | Changed values in a scanned PDF (OCR path) are reflected in answers | integration |
