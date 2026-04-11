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

---

## Round 2 — Additional demo polish (A / B / C / D)

After Phase 5.1–5.5 landed and were pushed, four additional improvements
were added to reshape the demo feel from "ask-wait-dump" to
"conversational analyst with CMS-native styling":

### A — Streaming the summary response

**Problem.** `agent.py::_summarize` was blocking — the UI sat silent
for 2–5 s per answer, then dumped the whole paragraph at once.

**Change.** Split `SQLAgent.run_query` into:
- `prepare(question, chat_history)` — runs the SQL generation + execution
  + retry loop and returns `{sql, data, error, attempts, canned_answer}`.
- `stream_summary(question, sql, df)` — async generator yielding text
  chunks via `llm_summary.astream(...)`.

`run_query` is kept as a back-compat wrapper so `smoke-test-agent.py`
and the CLI REPL still work.

`app.py::on_message` now calls `prepare` first, then either renders a
canned answer (unsupported / empty-df) or streams tokens into
`cl.Message.stream_token(...)`. The truncation notice is appended after
streaming finishes.

### B — LLM-generated follow-up suggestions

**Problem.** The conversation stalled after each answer. A live demo
needs to flow through 3–4 questions without the user typing.

**Change.** Added `SQLAgent.suggest_followups(question, answer)` which
makes a third LLM call with a structured prompt ("propose 3 follow-up
questions, one per line, max 70 chars"). Results render as
`cl.Action` buttons on a fresh `cl.Message("You might also ask:")`.
A `@cl.action_callback("followup")` handler echoes the clicked
question as a user message and re-enters the same pipeline via a
shared `_answer_question` helper.

Gated on `config.yaml` → `ui.show_followups: true` so the feature can
be toggled off in under a second if the extra LLM call slows rehearsal.
Wrapped in a broad `try/except` — follow-up generation failures never
touch the main answer.

### C — Show-SQL action

**Problem.** Selecting text inside a collapsed "Generating SQL" step
to copy the SQL is fiddly for technical clients.

**Change.** Attaches `cl.Action(name="show_sql", payload={"sql": ...})`
to the summary message. The callback sends a fresh, top-level
assistant message containing only the SQL in a fenced code block,
which is easy to select and copy. Gated on `config.yaml` →
`ui.show_copy_sql: true`.

### D — CMS Open Payments branding

**Problem.** The demo showed the generic Chainlit default name, logo,
and theme. A CMS/healthcare client would clock the mismatch immediately.

**Change.**
- `.chainlit/config.toml` `[UI]`: `name = "CMS Open Payments Data Analyst"`,
  `description = "..."`, `logo_file_url = "/public/openpayments-logo.png"`,
  `default_avatar_file_url = "/public/openpayments-avatar.png"`,
  `custom_css = "/public/branding.css"`.
- New `.chainlit/public/branding.css` overriding primary color to USWDS
  gov blue `#005ea2` with accent `#0050d8`, tightening the font stack
  toward Source Sans Pro, and restyling the header/starter buttons.
- New `.chainlit/public/openpayments-logo.png` and
  `openpayments-avatar.png` — placeholder text wordmarks generated
  locally with PIL in the CMS blue palette. The user can overwrite
  these files with the real CMS assets at any time — no code change
  needed.
- `chainlit.md` and `app.py::GREETING` rewritten to match CMS-aligned
  copy ("CMS Open Payments program", "financial relationships between
  pharmaceutical and medical device manufacturers and U.S. physicians
  and teaching hospitals").

**Asset-sourcing note.** The authoritative source was
`https://openpayments.system.cms.gov/login`, but outbound WebFetch
failed (SSL + timeout) from this environment. Path 2 (locally-generated
placeholder + USWDS colors) was taken for the initial commit so the
feature is self-contained.

### Round 2 acceptance criteria

- [ ] A — summary text appears character-by-character, not all-at-once
- [ ] B — three follow-up buttons render after each answer; clicking
      one re-enters the pipeline and produces a new streamed answer
- [ ] C — "📋 Show SQL" action renders a copyable SQL message on click
- [ ] D — tab shows "CMS Open Payments Data Analyst", header logo is
      the new image, primary color is gov blue
- [ ] No regressions on Phase 5.1–5.5 (CSV download, truncation notice,
      starters, stale-data warning, friendly errors) or on the Phase 4
      15-query suite

---

## Round 3 — Additional demo polish ideas (NOT YET IMPLEMENTED)

Captured during the pre-demo review. These are ranked by demo impact
divided by implementation effort. Nothing in this section has been
coded — it's a todo list for the next session.

### 🏆 High ROI — strongly recommended (~90 min total)

#### R3.1 — Query performance footer

**Problem.** Clients underestimate how fast DuckDB is on local hardware.
The speed of the demo is the most under-sold feature of the app.

**Change.** Append a one-line footer to every streamed summary:
`⚡ Answered in 1.2s from 15.4M rows scanned`

`prep["elapsed_sec"]` is already populated in `SQLAgent.prepare`.
Scanned-row count can come cheaply from a DuckDB `EXPLAIN ANALYZE` or
from `COUNT(*)` on the source table, whichever is simpler. If
scanned-row count proves awkward, ship just the elapsed time — it
already tells the story.

**Files:** `app.py::_answer_question` — append the footer after the
streaming loop and before the truncation notice.

**Demo payoff.** Reframes client mental model: "local 55M-row query in
under 2 seconds" is a different conversation from "Amazon Q Business
but local."

#### R3.2 — Visible self-correction

**Problem.** The SQL self-correction loop in `SQLAgent.prepare` (up to
3 retries) already works — it's why the 15/15 test suite passes. But
it's **invisible** to the user. The most impressive feature of the
agent is silent.

**Change.** When `prep["attempts"] > 1`, make the retries visible. Two
reasonable approaches:

- **Option A (minimal):** add one extra line to the "Generating SQL"
  step output: `⚠️ First attempt failed — column name mismatch.
  Retried and succeeded.`
- **Option B (richer):** render each failed attempt as its own
  collapsible `cl.Step` inside the main step, showing the bad SQL and
  the DuckDB error, then the corrected SQL.

Option A is 10 lines; Option B is ~40 lines and more visually
impressive. The plan recommends **Option A first**, upgrade to B if
time allows.

**Files:** `agent.py::prepare` — capture the per-attempt error history
in the prep dict (currently only the *last* error is stored).
`app.py::_answer_question` — render the history into the existing
`cl.Step` output.

**Demo payoff.** This is **the** single best demo moment. It answers
the unspoken client objection — "what if the AI gets the SQL wrong?" —
before they voice it. Clients will see a failure, watch the agent fix
itself, and decide they trust it.

#### R3.3 — Live record counts in the greeting

**Problem.** The greeting hard-codes "55 million records" as marketing
copy. A skeptical client notices static numbers don't move.

**Change.** In `on_chat_start`, run four quick `COUNT(*)` queries
against `all_general_payments`, `all_research_payments`,
`all_ownership_payments`, and `all_removed_deleted`. Interpolate the
results into the greeting:

> Currently loaded: **15,432,109** general, **3,612,458** research,
> **8,014** ownership, and **1,204,556** removed/deleted payment
> records across calendar years 2021–2024.

DuckDB answers each of these in under 50 ms.

**Files:** `app.py::on_chat_start` — add a `_load_stats(agent)` helper
and format the greeting with its output.

**Demo payoff.** The greeting feels *connected to real data*, not
marketing copy. Clients register this subconsciously as "the app is
showing me its actual state."

### ✨ Nice polish — fast wins (~15 min each)

#### R3.4 — Excel (.xlsx) download alongside CSV

**Change.** In `app.py::_build_response_elements`, add a second
`cl.File` pointing at an `.xlsx` file written via
`df.to_excel(path, index=False, engine="openpyxl")`. Requires adding
`openpyxl` to `requirements.txt`.

**Demo payoff.** Analysts live in Excel. "Download CSV" is fine;
"Download Excel" is *expected* in a healthcare/compliance setting.

#### R3.5 — Dollar formatting guidance in the summary prompt

**Change.** Add a rule to `SUMMARIZE_PROMPT_TEMPLATE` in `agent.py`:

> When referencing dollar amounts, format them as `$1.2M`, `$834K`, or
> `$1,234,567` — never as raw decimals like `1234567.0`. Format percentages
> as `42.3%`, not `0.423`.

**Demo payoff.** Prevents the LLM from embarrassing itself with
`2345678.0` inside a sentence. Costs nothing; makes every answer look
professional.

#### R3.6 — Data provenance footer

**Change.** Append a one-line source citation to every streamed
summary (alongside R3.1 if both are shipped):

> Source: `all_general_payments`, 15,432,109 rows matched, 2024.

The table name is already in `prep["sql"]` (first `FROM` clause); the
row count is `len(prep["data"])`; the year can be parsed from the SQL
or from the question itself.

**Demo payoff.** Builds trust with a compliance-minded audience.
Makes every answer feel auditable.

### 🤔 Bigger lifts — consider only if time allows (~1–2 hours each)

#### R3.7 — Drill-down on chart clicks

**Change.** Wire Plotly click events through Chainlit so that clicking
a bar in "Top 10 companies" auto-runs a follow-up query scoped to that
company. Requires `cl.on_plotly_click` or a custom JS bridge — Chainlit
support for this is fiddly and version-dependent.

**Risk.** May not work cleanly on all Chainlit releases. Demo-killer
if it breaks live.

#### R3.8 — Conversation-to-PDF export

**Change.** "Download this analysis as a report" button at the end of
a session. Collect every Q/A pair from `cl.user_session`, render to a
simple PDF via ReportLab or weasyprint, attach via `cl.File`.

**Demo payoff.** Clients want to "take the demo home" — a single PDF
of the session they just watched is a concrete artifact that outlasts
the meeting.

### ⏭ Explicitly out of scope for Round 3

These are interesting but not worth the effort for a 15-minute demo:

- Named entity linking (click "Pfizer" → drill-down)
- Comparison mode ("X vs Y" shortcut)
- Query history sidebar
- Rotating starter prompts
- Hover-to-explain for specific numbers in the summary
- Auto-generated conversation titles

### Round 3 recommended order

If any Round 3 items ship before the demo, do them in this order:

1. **R3.2** (visible self-correction) — highest demo payoff
2. **R3.1** (performance footer) — reframes the speed story
3. **R3.3** (live counts in greeting) — fixes the marketing-copy feel
4. **R3.5** (dollar formatting) — cheapest, ship with anything above
5. **R3.6** (provenance footer) — ships alongside R3.1
6. **R3.4** (Excel export) — if `openpyxl` is already installed
7. **R3.8** (PDF export) — only if R3.1–R3.6 leave time
8. **R3.7** (chart drill-down) — skip unless risk of live failure is acceptable
