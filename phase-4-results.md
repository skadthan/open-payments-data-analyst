# Phase 4 — Integration & Testing Results

**Date:** 2026-04-10
**Status:** ✅ Complete — all acceptance criteria met

This document records the results of the Phase 4 test suite defined in
`project-roadmap-plan.md`. All testing was done manually through the
Chainlit chat UI (`python run.py`) against the full CMS Open Payments
dataset (2021–2024) loaded into DuckDB.

---

## Test Query Suite — 15 / 15 Passed

| # | Category | Query | Result |
|---|----------|-------|--------|
| 1 | Simple count | "How many general payment records are in 2024?" | ✅ Pass |
| 2 | Simple count | "How many research payment records exist across all years?" | ✅ Pass |
| 3 | Top-N aggregation | "Top 10 companies by total payment amount across all years" | ✅ Pass — ranked list + horizontal bar chart |
| 4 | Filtered aggregation | "Consulting fee payments over $100,000 in New York in 2023" | ✅ Pass |
| 5 | Cross-year comparison | "Compare total payments by year from 2021 to 2024" | ✅ Pass — 4-row result |
| 6 | Specialty analysis | "Which medical specialties received the most payments in 2024?" | ✅ Pass |
| 7 | Research-specific | "Top 5 therapeutic areas by research funding in 2024" | ✅ Pass — uses `research_payments_2024` |
| 8 | Ownership | "How many physicians have ownership interests?" | ✅ Pass — uses `ownership_payments` |
| 9 | Geographic | "Show total payments by state in 2024, top 20" | ✅ Pass |
| 10 | Product analysis | "What are the top drugs by associated payment amount in 2024?" | ✅ Pass |
| 11 | Payment type breakdown | "Break down payment forms (cash, in-kind, etc.) for 2024" | ✅ Pass |
| 12 | Time series | "Monthly payment trends for 2024" | ✅ Pass |
| 13 | Follow-up | "Now show me the same for 2023" (after #12) | ✅ Pass — conversation context preserved |
| 14 | Unanswerable | "What is the weather in Baltimore?" | ✅ Pass — graceful decline via unsupported sentinel |
| 15 | Ambiguous | "Show me the biggest payments" | ✅ Pass — reasonable default (top by amount) |

**Score: 15 / 15** (target was 12 / 15).

---

## Acceptance Criteria

- [x] 12+ of 15 test queries produce correct answers (**15/15**)
- [x] No unhandled exceptions during any test scenario
- [x] Ingestion completes in under 30 minutes
- [x] Query response time under 30 seconds for all test queries
- [x] Application runs stably for 30+ minute interactive session

---

## Bugs Found and Fixed During Phase 4

Three issues surfaced during integration testing and were fixed in
commits `2c9fa6d` and `5eefe21`:

### 1. Blank screen on first browser load (Python 3.14 + nest_asyncio)
**Symptom:** `chainlit run app.py` booted cleanly and `/` returned HTTP 200,
but every static frontend asset (`/favicon`, `/logo`, JS/CSS bundles)
500-errored with `anyio.NoEventLoopError`. The browser showed a blank page.

**Root cause:** `chainlit/cli/__init__.py` calls `nest_asyncio.apply()` at
module import time. On Python 3.14 that monkey-patch breaks
`asyncio.current_task()`, cascading through sniffio → anyio → starlette's
`FileResponse.__call__`.

**Fix:** Added `run.py`, a thin launcher that pre-imports `nest_asyncio`,
replaces `apply` with a no-op, then hands off to `chainlit.cli`. Users
launch with `python run.py` instead of `chainlit run app.py`.

Full bisect path documented in `phase-3-plan.md` "Observed issues and fixes".

### 2. Case-sensitive name filters returned zero rows
**Symptom:** Asking "how many payments does the provider named Madan
Bangalore have by each year?" returned `The query returned no rows.` The
generated SQL used `Covered_Recipient_First_Name = 'Madan' AND
Covered_Recipient_Last_Name = 'Bangalore'` — exact-case equality — but
the stored data uses different casing.

**Fix:** Added a new system-prompt rule requiring `ILIKE` for all
name/string-equality filters on physician, company, drug, hospital,
city, and specialty columns. Users no longer have to guess stored casing.

### 3. Topic guard rejected legitimate follow-ups
**Symptom:** After the empty-result for Madan Bangalore, the user asked
"can you use upper or lower case, maybe the names are case sensitive?"
The agent replied with the unsupported-question message because rule
#8 evaluated the follow-up in isolation and decided it wasn't about
Open Payments data.

**Fix:** Softened the topic-guard rule so it only fires the unsupported
sentinel when chat history shows no prior on-topic exchange. Refinement
follow-ups ("try case-insensitive", "what about 2023?", "show me the
chart") now pass through as on-topic.

### 4. Empty-result summary was a dead end
**Symptom:** When a query legitimately returned zero rows, the summary
was the flat string "The query returned no rows." — no hint about why
or what to try next.

**Fix:** Replaced the empty-result branch in `_summarize` with a
multi-line message listing concrete next steps: double-check spelling,
try a partial match, broaden filters, confirm the entity exists in
the 2021–2024 dataset.

---

## Related Commits

| Commit | Description |
|--------|-------------|
| `2c9fa6d` | Fix Python 3.14 + nest_asyncio blank-screen bug (added `run.py`) |
| `5eefe21` | Chat UX — case-insensitive names, softer topic guard, actionable empty results |
| `8169282` | Add smoke test script and chat reference artifacts |

---

## Phase 4 Status: Complete

All 15 test queries pass, all acceptance criteria met, and all bugs
discovered during integration testing are fixed and committed. The MVP
is ready for Phase 5 (post-MVP enhancements) or deployment.
