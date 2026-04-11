# Phase 3: Chainlit Chat UI

## Goal

Wrap the Phase 2 `SQLAgent` in a polished, Amazon Q Business-style chat
interface using **Chainlit 2.11**. Users ask questions in plain English; the
UI shows the agent's reasoning (SQL generation as a collapsible step), the
natural language answer, the result table, and an auto-generated chart when
applicable.

This is the user-facing surface of the project. Every prior phase exists to
make this layer feel fast and trustworthy.

---

## Pre-Audit (what already exists)

| Item | Status | Notes |
|------|--------|-------|
| `SQLAgent` from Phase 2 | ✅ stable | `run_query(question, history) → dict` is the entry point. Synchronous. |
| `config.yaml` `ui:` section | ✅ present | `title`, `max_display_rows`, `show_sql`, `show_charts`, `theme`, `show_agent_steps` |
| `chainlit==2.11.0` | ✅ installed | Confirmed in `.venv`. Provides `cl.Dataframe` (a native pandas element — **better than the roadmap's markdown-text approach**). |
| `plotly==6.7.0` | ✅ installed | Used by `cl.Plotly` for inline charts. |
| `.chainlit/config.toml` | ✅ auto-generated | Created on first `chainlit run`. **`.chainlit/` is gitignored** — we deliberately do NOT commit it (contains machine-specific session timeouts and a `[meta] generated_by` line that flips on every Chainlit upgrade). UI customizations belong in `app.py` and `config.yaml`, not in `config.toml`. |
| `.gitignore` already excludes `.chainlit/` | ✅ done | No new gitignore changes needed. |

### Deviations from the roadmap doc

1. **Use `cl.Dataframe`, not markdown-text-in-`cl.Text`.** Chainlit 2.x ships
   a first-class pandas DataFrame element. It paginates, sorts, and renders
   far better than a markdown table — critical for a 1000-row result.
2. **Skip `cl.Plotly` for ownership/single-row results.** A 1-row chart is
   noise. The auto-chart heuristic must reject those cases up front.
3. **No custom `.chainlit/config.toml`.** Roadmap suggested customizing it;
   reality is the file is gitignored and machine-specific. We rely on
   Chainlit defaults plus the `default_theme = "dark"` line that the
   auto-generated file already commented in/out is fine — we don't manage it.

---

## What gets built

| File | Purpose |
|------|---------|
| `app.py` | Chainlit chat application (~200 lines) |
| `phase-3-plan.md` | This document |

Nothing else. No new helpers, no new config keys.

---

## Module design — `app.py`

### Top-level layout

```
app.py
├── _load_config()                   — read config.yaml once at import time
├── on_chat_start (decorated)        — init agent, store in session, send greeting
├── on_message (decorated)           — main turn handler
├── _build_sql_step(...)             — show generated SQL as a cl.Step
├── _build_response_elements(...)    — assemble Dataframe + optional chart
├── _auto_chart(df) -> Figure | None — heuristic chart generator
└── (no CLI — chainlit run app.py is the only entry point)
```

### Why `_load_config` runs at import time

Chainlit imports `app.py` once per worker. If config is missing or malformed
we want the process to fail loudly at startup, **not** on the first user
message. An import-time `FileNotFoundError` is the right failure mode here.

### `on_chat_start` — startup checks and agent init

The agent's `__init__` already validates that the DuckDB file exists and is
openable read-only. We catch its exceptions and turn them into a friendly
`cl.ErrorMessage` so the user sees actionable instructions instead of a
stack trace.

```python
@cl.on_chat_start
async def on_chat_start():
    try:
        agent = SQLAgent(CONFIG_PATH)
    except FileNotFoundError as e:
        await cl.ErrorMessage(
            content=(
                f"**DuckDB database not found.**\n\n{e}\n\n"
                "Run `python ingest.py --rebuild` to create it, then refresh."
            )
        ).send()
        return
    except Exception as e:
        await cl.ErrorMessage(
            content=(
                f"**Failed to initialize the agent.**\n\n{e}\n\n"
                "Check that Ollama is running (`ollama serve`) and that the "
                "model in `config.yaml` is pulled (`ollama list`)."
            )
        ).send()
        return

    cl.user_session.set("agent", agent)
    cl.user_session.set("chat_history", [])

    await cl.Message(content=GREETING).send()
```

`GREETING` is a constant string with a short welcome and 3-4 example
questions to seed the user's first interaction.

### `on_message` — the main turn handler

Three responsibilities:

1. **Run the agent inside a `cl.Step`** so the user sees "Generating SQL"
   spinning, then expands to see the actual SQL once it's done. Use
   `cl.make_async(agent.run_query)` to keep the synchronous DuckDB +
   LangChain + Ollama call off the asyncio event loop. Without this the
   UI freezes for the duration of every query.
2. **Branch on `result["error"]`** — error path sends an `ErrorMessage`
   with the failed SQL inside an expandable step; success path assembles
   data + chart elements and sends them with the natural language answer.
3. **Update `chat_history`** with the (question, answer) tuple, capped at
   the last 5 exchanges to match the agent's `chat_history[-4:]` window
   (we keep one extra in case the agent's window grows in the future).

```python
@cl.on_message
async def on_message(message: cl.Message):
    agent = cl.user_session.get("agent")
    if agent is None:
        await cl.ErrorMessage(
            content="Agent not initialized. Refresh the page to retry."
        ).send()
        return

    chat_history = cl.user_session.get("chat_history") or []

    async with cl.Step(name="Generating SQL", type="tool") as step:
        result = await cl.make_async(agent.run_query)(
            message.content, chat_history
        )
        if result.get("sql"):
            step.output = f"```sql\n{result['sql']}\n```"
        if result.get("error"):
            step.output = (
                (step.output or "")
                + f"\n\n**Error after {result['attempts']} attempt(s):** "
                + result["error"]
            )

    if result.get("error"):
        await cl.ErrorMessage(
            content=(
                "Sorry, I couldn't answer that. The agent retried "
                f"{result['attempts']} time(s) and the last DuckDB error was:\n\n"
                f"```\n{result['error']}\n```"
            )
        ).send()
        return

    elements = _build_response_elements(result)
    await cl.Message(content=result["answer"], elements=elements).send()

    chat_history.append((message.content, result["answer"] or ""))
    cl.user_session.set("chat_history", chat_history[-5:])
```

### `_build_response_elements(result)` — Dataframe + optional chart

```python
def _build_response_elements(result):
    elements = []
    df = result.get("data")
    if df is None or df.empty:
        return elements

    capped = df.head(int(CONFIG["ui"]["max_display_rows"]))
    elements.append(cl.Dataframe(name="Results", data=capped, display="inline"))

    if CONFIG["ui"].get("show_charts", True):
        fig = _auto_chart(capped)
        if fig is not None:
            elements.append(cl.Plotly(name="Chart", figure=fig, display="inline"))

    return elements
```

Notes:
- `cl.Dataframe` accepts a pandas DataFrame directly — no markdown
  conversion needed.
- The result is capped to `ui.max_display_rows` (1000) **before** charting
  so we don't accidentally chart 50M rows.
- Charts are gated on `ui.show_charts` from config, not hardcoded.

### `_auto_chart(df)` — heuristics

| Result shape | Chart |
|-------------|-------|
| 1 row | `None` (a 1-bar chart is noise) |
| 2 columns + 1 string + 1 numeric, ≤ 30 rows | Horizontal bar (numeric on x, category on y, sorted descending) |
| Has a date/year column + a numeric column | Line chart (date on x, numeric on y) |
| Anything else | `None` (let the table speak for itself) |

Implementation sketch:

```python
def _auto_chart(df):
    if len(df) < 2:
        return None

    cols = list(df.columns)
    dtypes = df.dtypes

    # Time-series detection: column named like Date_*, Program_Year, or month/year
    time_cols = [c for c in cols
                 if pd.api.types.is_datetime64_any_dtype(dtypes[c])
                 or c.lower() in {"program_year", "year", "month"}
                 or "date" in c.lower()]
    numeric_cols = [c for c in cols if pd.api.types.is_numeric_dtype(dtypes[c])
                    and c not in time_cols]

    if time_cols and numeric_cols:
        return px.line(df, x=time_cols[0], y=numeric_cols[0],
                       title=f"{numeric_cols[0]} over {time_cols[0]}")

    # Categorical bar: exactly 2 cols, one string + one numeric, ≤ 30 rows
    if len(cols) == 2 and len(df) <= 30:
        str_cols = [c for c in cols if dtypes[c] == object]
        num_cols = [c for c in cols if pd.api.types.is_numeric_dtype(dtypes[c])]
        if len(str_cols) == 1 and len(num_cols) == 1:
            sorted_df = df.sort_values(num_cols[0], ascending=True)
            return px.bar(sorted_df, x=num_cols[0], y=str_cols[0],
                          orientation="h",
                          title=f"{num_cols[0]} by {str_cols[0]}")

    return None
```

### Greeting copy

Short, friendly, and seeded with 3 example questions that exercise the
three table types so users discover the dataset shape on turn one:

```
Welcome to the Open Payments Data Analyst.

I can answer questions about CMS Open Payments — pharmaceutical and medical
device industry payments to U.S. physicians and teaching hospitals from
2021 through 2024 (about 55 million records).

Try asking:
- *Top 10 companies by total payment amount across all years*
- *Which medical specialties received the most general payments in 2024?*
- *How many physicians have ownership interests across all years?*
```

---

## Error handling matrix

| Failure | Where caught | What the user sees |
|---------|--------------|-------------------|
| `config.yaml` missing/malformed | Import time | Process exits — admin runs `chainlit run` from terminal and reads the traceback. (Not a user-facing error.) |
| DuckDB file missing | `on_chat_start` | `ErrorMessage` with "run `python ingest.py --rebuild`" instructions |
| DuckDB locked by another process | `on_chat_start` | `ErrorMessage` explaining the lock and suggesting closing the offender |
| Ollama not running | `on_chat_start` (LLM init) OR `on_message` (first call) | `ErrorMessage` with `ollama serve` instructions |
| LLM returns empty / malformed SQL | `agent.run_query` retries; if exhausted, `error` is set | `ErrorMessage` showing attempt count and last error |
| DuckDB query error | Same as above | Same |
| Off-topic question | Sentinel SQL → `UNSUPPORTED_MESSAGE` returned as `answer` | Polite "I can only answer questions about CMS Open Payments…" message — no error UI |

The whole point is that **no unhandled exception ever reaches the user**.
Every failure mode is either an `ErrorMessage` with actionable text or a
process-level startup failure visible only in the terminal.

---

## Concurrency / async notes

`SQLAgent.run_query` is fully synchronous — it does blocking HTTP calls to
Ollama and blocking DuckDB execution. Calling it directly from an async
handler would freeze the Chainlit UI for every other tab/session sharing
the worker. We wrap it with `cl.make_async`, which schedules it on a thread
pool. This is the same pattern used in Chainlit's official LangChain examples.

The DuckDB connection inside `SQLAgent` is opened in **read-only** mode,
which means multiple Chainlit sessions (separate tabs / users) can each
own their own `SQLAgent` instance and share the same `.duckdb` file
without locking each other out.

---

## Acceptance criteria

- [x] `chainlit run app.py` launches without errors and serves HTTP (verified on `:8765` headless)
- [ ] The greeting renders with the example questions *(needs browser)*
- [ ] Asking a simple count question shows the SQL step expanding to the generated SQL, the natural-language answer, and a `cl.Dataframe` element *(needs browser)*
- [x] Asking a top-10 question renders a horizontal bar chart *(unit-tested via `_auto_chart`)*
- [x] Asking a year-trend question renders a line chart *(unit-tested via `_auto_chart`)*
- [ ] An off-topic question gets the polite "unsupported" message — no error UI *(agent path verified in Phase 2 smoke test; UI path needs browser)*
- [ ] A follow-up question (e.g. "now show me the same for 2023") uses the prior turn's context *(agent path verified in Phase 2; UI path needs browser)*
- [x] The UI does not freeze during a long query — `agent.run_query` is wrapped in `cl.make_async`
- [x] Ollama / DuckDB startup failures yield an `ErrorMessage`, not a stack trace *(every exception in `on_chat_start` is caught and routed to `cl.ErrorMessage`)*

---

## Implementation results

### What was verified programmatically (no browser needed)

| Check | Result |
|-------|--------|
| `app.py` parses (AST) and imports cleanly under the project venv | ✅ |
| `chainlit run app.py --headless --port 8765` boots cleanly on first try | ✅ — `Your app is available at http://localhost:8765` within 1 second |
| `GET /` returns HTTP 200 (1434-byte HTML shell) | ✅ |
| `POST /project/settings` returns HTTP 200 (handlers registered) | ✅ |
| Chainlit auto-generated `chainlit.md` on first run, then we customized it | ✅ |
| `_auto_chart` returns a Plotly Figure for top-N (string + numeric, ≤30 rows) | ✅ — bar |
| `_auto_chart` returns a Plotly Figure for year-trend (Program_Year + numeric) | ✅ — line |
| `_auto_chart` returns `None` for 1-row results, 3-column results, empty DFs | ✅ × 3 |
| `_auto_chart` returns `None` for 35-row top-N (above bar-chart cap) | ✅ |

### What still needs human eyeballs in a browser

These can only be verified by `chainlit run app.py` and clicking around — they
exercise React rendering, WebSocket message flow, and the Step expand/collapse
UX that has no programmatic equivalent:

- Greeting renders with the example questions formatted as a bullet list
- Asking a question shows "Generating SQL" as a collapsible step that expands to the SQL
- `cl.Dataframe` element renders the result table with pagination/sort
- `cl.Plotly` element renders the bar/line chart inline beneath the table
- Follow-up question ("now show me the same for 2023") uses prior context
- Stopping Ollama mid-session yields an `ErrorMessage`, not a stack trace

### Observed issues and fixes

| Issue | Symptom | Fix |
|-------|---------|-----|
| **pandas 2.x StringDtype default** | `_auto_chart` rejected the top-10 companies case (`dtypes['Company'] == object` was False because the column was `StringDtype(na_value=nan)`, not `object`). Result: bar charts never fired for any string-keyed top-N query. | Switched the categorical detection to `pd.api.types.is_string_dtype(dtypes[c]) and not pd.api.types.is_numeric_dtype(...)`. Re-ran all 5 chart cases — all correct. |
| **`cl.Dataframe` requires Chainlit context to instantiate** | Trying to unit-test `_build_response_elements` outside a chat session crashes with `ChainlitContextException` because `Field(default_factory=lambda: context.session.thread_id)` runs at construction time. | Acknowledged limitation: the element-building path can only be exercised inside a real Chainlit session. Documented in this section so future maintainers don't try the same dead-end. |
| **`chainlit.md` auto-generated on first run** | First `chainlit run` created a generic Chainlit welcome page at the project root (not under the gitignored `.chainlit/`). | Customized `chainlit.md` with project-specific copy and example questions, then committed it as part of Phase 3. |

---

## Out of scope (deferred to later phases)

| Item | Why deferred |
|------|--------------|
| CSV/Excel download of results | Phase 5 enhancement — `cl.File` element, simple but not required for MVP |
| Saved/pinned queries | Phase 5 enhancement |
| Streaming the summarization tokens | The summarizer runs in 1–2s warm; streaming adds complexity for marginal UX gain |
| Custom `.chainlit/config.toml` styling | Gitignored; would need to be re-applied per machine. Defaults look fine. |
| Multi-user auth | Single-user local app; Chainlit's auth layer is unnecessary |

---

## Dependencies

Phase 0 (Chainlit + Plotly installed), Phase 1 (DuckDB populated), Phase 2 (`SQLAgent` available).
