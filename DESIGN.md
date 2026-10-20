# Design

## 1. Ingestion: tables and the scanned document

The manifest is the only source of metadata. Each file is extracted into ordered blocks (heading, text,
table) by format (`app/ingestion/extractors.py`):

- **PDF** (PyMuPDF): tables come from `page.find_tables()`, and text blocks inside a table are skipped so
  nothing is read twice. Headings are bold spans larger than the body font. The title and the running
  header and footer (small font in the page margins) are dropped. A page with almost no text and an image
  is rendered at 300 dpi and OCR'd with Tesseract. OCR lines that are mostly symbols (signatures) are
  discarded.
- **DOCX**: the body is walked in document order, so each table stays under its heading. Heading styles
  become headings. Merged cells are de-duplicated.
- **HTML / Markdown**: `h2`–`h6` and `##` become headings, and tables keep their rows.

Every table is kept as rows and rendered as Markdown with its header, so a value is never separated from
its column. The per-document metadata table ("Document ID | …") is dropped. Holiday dates are extracted
automatically from `calendar` documents (`holidays.py`): every row whose cell parses as a date is a holiday,
except in tables or sections titled *restricted* or *optional*. They are stored in `assistant_holidays`.

## 2. Chunking and retrieval

One chunk is one section, split only above 380 tokens. A split table repeats its header, and page-located
chunks never straddle pages. The locator is therefore exact: the section heading, or `p. N` when a PDF has
no heading. Sections are short in this corpus (30–200 tokens), so a whole section fits the budget, and
answers depend on whole rules (a sick-leave paragraph, an accrual table), not on sentence fragments.

Retrieval is hybrid: BM25 plus `BAAI/bge-small-en-v1.5` on CPU (fastembed/ONNX), fused with reciprocal
rank fusion. A relative cut-off removes chunks that only share words like "days". Selection is diversity
first: the best chunk of each matching document, then further chunks. That matters because precedence
only works if the competing documents (v1, v2, FAQ, addendum) are all in front of the extractor. Sources
are added greedily while the exact cl100k count of the rendered sources stays ≤ 3,000.

## 3. Section 5 rules

The LLM extracts, and code decides (`app/orchestration/rag.py`, `app/policy/`).

1. **Eligibility** (`eligibility.py`): readable access level, region or GLOBAL, `effective_date ≤ reference
   date`. Only these documents, plus readable future documents for the requester's region, can be retrieved.
2. **Extraction** (LLM call 2): for each source, `{rule, value, statement, source}` claims, with one shared
   rule key per quantity across sources.
3. **Precedence** (`precedence.resolve_rule`): authority, then scope, then recency, each narrowing the
   survivors of the previous test. If the surviving documents state different value sets, the result is
   an unresolved conflict and every side is cited. Several values from one document (per service band)
   are complementary, not a conflict.
4. **Upcoming** (`find_upcoming`): a future claim is reported if, on its own effective date, it would win
   against the current claims and changes the value.
5. **Composition** (LLM call 3) sees only the resolved facts, labelled `current`, `conflict` or `upcoming`.
   Losing values never reach it. Citations are the `(doc_id, locator)` of the facts it says it used. No
   claims means `no_answer`; the extractor's `unanswered` field drives partial answers.

The reference date is today unless the interpreter finds a past date in the question.

## 4. Where access control is enforced

- `app/api/routes.py::chat`: requester loaded from `X-Employee-Id` (401), session bound to its first
  employee in `repositories.claim_session` (403).
- `app/security/access.py`: `can_read_access_level`, `can_view_leave_of`, `can_use_team_leave`. These are the
  only definitions of the role table.
- `app/policy/eligibility.py::eligible_docs` and `app/retrieval/index.py::HybridIndex.search(allowed_doc_ids)`:
  restricted chunks are never scored, so they can never be selected, sent or cited.
- `app/tools/leave_tools.py`: every read tool calls `can_view_leave_of` with the header identity. Write tools
  have no employee argument and act on `ctx.requester`. `_validate_cancel` treats "someone else's request"
  exactly like "no such request".
- `app/orchestration/orchestrator.py::_prepare_reads`: the same check runs before any tool call, so a refusal
  carries no tool call. It also refuses more than one other employee per message (bulk).
  `_resolve_target` only matches names among employees the requester may see.
- The LLM's plan is treated as untrusted input: it can only pick among these code paths, never widen them.

## 5. Personal data

- **Logs**: call sites log IDs, counts and statuses only, never messages, prompts or tool results.
  `PiiScrubFilter` (`logging_setup.py`) also replaces any employee name loaded from the DB with the
  employee ID, masks emails and removes phone numbers in every record, including uvicorn's. Exceptions are
  logged by type only.
- **Answers**: tool answers are templates. Names appear only for employees the requester may see. Emails
  go out as `mask_email`, and phones are never selected into a result. `scrub_answer` runs a final pass on
  every answer and argument.
- **Prompts**: the interpreter gets the requester's ID, role and region (not their name), plus history
  passed through `scrub_all`. The composer gets tool data with names and emails removed
  (`_llm_safe_tool_data`). The extractor gets region and level only.

## 6. Failure modes found and fixed

| Found | Fix |
|---|---|
| PDF metadata table not recognised after cell flattening, so "Effective date 1 January 2026" became an India holiday | match on the normalised first cell, and skip metadata-key rows when parsing holidays |
| Retrieval sent chunks that only matched "days / year" | relative BM25 / dense cut-offs |
| One document's per-band values reported as a conflict | conflicts need two documents with different value sets |
| Only the first changed band was reported as upcoming | report every winning claim of the future document |
| "Version 1 → Version 2" tables could leak superseded values | extractor instruction: extract only the value the document itself sets |
| PII log filter cleared uvicorn's argument tuple, so every access log line crashed | scrub arguments one by one |
| The interpreter could guess a leave type | code checks the user's own words, otherwise `clarify` |
| Check-then-write races (overdraw, double booking) | `BEGIN IMMEDIATE` and revalidation inside the transaction; concurrency tests |
| *Adversarial review:* `fromisoformat` accepts `20261020`, which slipped past the string-compared overlap SQL | strict `YYYY-MM-DD`; canonical dates stored |
| *Review:* a timed-out or crashed request could leave a hidden proposal for the next "yes" | proposal and turn committed together after the response is built; timeout sets a cancel flag and clears it |
| *Review:* extractor `unanswered` text bypassed the budget | sent as a counted, capped corpus block |
| *Review:* interpreter history mapped any name to an ID | only names the requester may see are mapped; others become `[name]` |
| *Review:* "apply leave for Ravi" proposed leave for the requester | action `target` plus a code check for other IDs and names, then refuse |
| *Review:* schema-invalid LLM JSON or deeply nested bodies caused 500s | schema validation becomes `LLMError` (`clarify`); body parse errors return 400 |
| *Review:* BM25 statistics included restricted chunks | IDF computed per request over allowed chunks only |

Still open: rule keys come from an LLM, and live-model latency has not been measured (README, Known
limitations).

## 7. At 10,000 documents

What breaks first is **retrieval and ingestion cost**. The BM25 and cosine loops are pure Python over all
chunks per query, and start-up re-extracts and re-embeds everything, with OCR running serially. Fixes:
persist chunks and vectors keyed by file hash, re-embed only changed files, and OCR in a worker pool.
Move to a vector store with metadata filters (Qdrant or pgvector) and push eligibility (access level,
region, effective date) into the filter, so access control stays *before* scoring. Next, **precedence
recall** degrades: with many versions per topic, top-k may miss the competing document. Group documents
into policy families at ingestion (same topic, different versions and regions), and always retrieve the
eligible members of a matched family. Then cap per-family sources to stay within 3,000 tokens, with a
per-rule extraction call when a family is large.
