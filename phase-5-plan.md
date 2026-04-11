# Phase 5 — Demo Polish (Pre-Client Walkthrough)

## Context

Phases 0–4 are complete: ingestion, agent, chat UI, and a 15/15 passing
test suite. The next milestone is a **live demo to a prospective client**.
If the client likes it, a follow-up phase will handle production readiness
(multi-user, auth, deployment, cost-vs-hosted-API tradeoffs).

This phase is deliberately **not** production work. The question is:
_"What does a smart, skeptical client notice in a 15-minute walkthrough
that would undermine confidence — and which of those can we fix in a day
or two?"_

The exploration in Phase 4 cleanup identified eight "future enhancements"
from `project-roadmap-plan.md` section "Phase 5". Most are post-demo work
(RAG, dashboards, multi-provider LLM, voice). This plan picks only the
subset that makes the demo itself more convincing.

## Scope (5 items)

Ordered by demo visibility × implementation cost.

### 5.1 — CSV result export (highest demo value)

**Problem:** After any successful query the client sees a table in the
chat, but there is no way to take the data with them. Every serious
analytics tool lets the user download results; not having this is the
single most visible gap.

**Change:** In `app.py::_build_response_elements`, attach a `cl.File`
element pointing at a temp-file CSV of the **full** result (not capped
to `max_display_rows`). Filename should be derived from the query
(slugified timestamp fallback).

**Files:** `app.py` only. No config changes. No agent changes.

### 5.2 — Row-truncation notice

**Problem:** `_build_response_elements` caps the inline table at
`config.ui.max_display_rows` (1000). If a query returns 12,000 rows,
the client sees 1000 with no indication the rest exist. They may
silently miss data.

**Change:** When `len(df) > max_display_rows`, add a short visible line
to the assistant message: _"Showing the first 1,000 of 12,345 rows —
use the CSV download for the full result."_ This cross-sells 5.1.

**Files:** `app.py::_build_response_elements` and `on_message`.

### 5.3 — Clickable starter prompts

**Problem:** The greeting lists three example questions as plain
markdown (`app.py:47-50`). A client has to copy-paste or retype them.
Chainlit has first-class support for clickable starter buttons via
`cl.Starter` / `@cl.set_starters`.

**Change:** Convert the three examples into `cl.Starter` objects so
the client clicks once to run each. Keeps the greeting as prose, adds
the buttons below.

**Files:** `app.py` — new `@cl.set_starters` handler.

### 5.4 — Data freshness check at startup

**Problem:** The app does not warn the user if CSVs in `Datasets/` have
been updated after the DuckDB file was built. During a demo, a stale
database would be embarrassing.

**Change:** In `on_chat_start`, after agent init, compare the
newest `.csv` mtime in `CONFIG.data.source_dir` against the mtime of
`CONFIG.data.duckdb_path`. If CSVs are newer, send an informational
(non-blocking) `cl.Message` telling the user to run
`python ingest.py --rebuild`.

**Files:** `app.py::on_chat_start` only.

### 5.5 — Friendlier error presentation

**Problem:** When the self-correction loop exhausts retries, `app.py`
shows the raw DuckDB error (`app.py:190-197`). A client seeing a line
like `Binder Error: Referenced column "Payment_Amount" not found in
FROM clause!` will not feel confident in the tool.

**Change:** Keep the raw error available, but lead with a plain-English
explanation: _"I tried to answer that 4 times but could not generate a
working query. Often this means the question is ambiguous — try
rephrasing it or asking about a specific year, company, or specialty."_
The technical details stay inside the existing collapsible SQL step, not
the top-level error message.

**Files:** `app.py::on_message` error branch.

---

## Explicitly out of scope for Phase 5

These come back on the table **only if the client green-lights the
project** after the demo:

- Multi-user authentication / sessions (Chainlit has it built in)
- Deployment to a shared host (nginx + TLS + systemd)
- Alternative LLM providers (Anthropic / OpenAI hosted APIs — likely the
  right answer for 5–10 concurrent users instead of scaling local Ollama)
- RAG over CMS methodology docs
- Dashboard mode with pinned auto-refreshing charts
- Saved/pinned queries beyond the starter buttons in 5.3
- Voice input via Whisper
- Smart column selection (two-pass LLM schema narrowing)
- Scheduled reports

---

## Acceptance criteria

- [ ] 5.1 — Every successful query exposes a downloadable CSV containing
      the **full** result set (not the capped view)
- [ ] 5.2 — Truncated results display an explicit row-count notice
- [ ] 5.3 — The landing screen shows three clickable starter buttons
      that run real queries end-to-end
- [ ] 5.4 — Stale-data warning appears when any CSV is newer than the
      DuckDB file; otherwise the app starts silently
- [ ] 5.5 — Exhausted-retry errors lead with plain English; the DuckDB
      detail is secondary
- [ ] Manual smoke-test the five items together in one session: open
      the app, click a starter, download the CSV, see a truncation
      notice on a large result, and deliberately trigger an error

## Verification

Run `python run.py`, then:

1. Click each of the three starter buttons — all three should return
   answers and expose CSV downloads.
2. Ask _"show me every general payment in 2024"_ (≫ 1,000 rows).
   Expect the truncation notice and a large CSV.
3. Rebuild `data/openpayments.duckdb` as a no-op and touch a file in
   `Datasets/` to make it newer — restart the app and confirm the
   stale-data banner.
4. Ask a deliberately broken question like _"show me the vibes"_ and
   confirm the error message is plain English.

## Rollout

Each of 5.1 – 5.5 lands as its own commit with a smoke test. If any one
of them reveals a deeper issue, that item is deferred and the demo
proceeds without it rather than holding up the rest.
