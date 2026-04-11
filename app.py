"""
Open Payments Data Analyst — Chainlit chat UI.

Wraps the Phase 2 SQLAgent in an Amazon Q Business-style chat surface.
Each turn:
  1. Shows "Generating SQL" as a collapsible cl.Step
  2. Renders the natural-language answer as the main message
  3. Attaches the result table (cl.Dataframe) and an auto-chart (cl.Plotly)
     when the result shape is suitable

Run with:
    chainlit run app.py

See phase-3-plan.md for design rationale.
"""
from __future__ import annotations

import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import chainlit as cl
import pandas as pd
import plotly.express as px
import yaml

from agent import SQLAgent


# --- Config (loaded once at import time) -----------------------------------

CONFIG_PATH = "config.yaml"


def _load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


CONFIG: dict = _load_config(CONFIG_PATH)


GREETING = (
    "Welcome to the **CMS Open Payments Data Analyst**.\n\n"
    "I can answer natural-language questions about the CMS Open Payments "
    "program — financial relationships between pharmaceutical and medical "
    "device manufacturers and U.S. physicians and teaching hospitals from "
    "**2021 through 2024** (about 55 million records across general "
    "payments, research payments, and ownership interests).\n\n"
    "Pick one of the starter prompts below, or type your own question."
)


# Click-to-run example prompts shown on the landing screen.
# Chainlit renders these as buttons above the composer before the
# first user message. They disappear once the chat starts.
STARTERS: list[dict[str, str]] = [
    {
        "label": "Top 10 companies",
        "message": "Top 10 companies by total payment amount across all years",
    },
    {
        "label": "Specialties in 2024",
        "message": "Which medical specialties received the most general payments in 2024?",
    },
    {
        "label": "Physician ownership",
        "message": "How many physicians have ownership interests across all years?",
    },
    {
        "label": "Yearly trend",
        "message": "Compare total general payments by year from 2021 to 2024",
    },
]


# --- Auto-chart heuristic --------------------------------------------------

def _auto_chart(df: pd.DataFrame):
    """Pick a chart type for the result, or return None if nothing fits."""
    if df is None or len(df) < 2:
        return None

    cols = list(df.columns)
    dtypes = df.dtypes

    # Time-series detection: datetime columns, or columns named like
    # Program_Year / Date_of_Payment / month / year.
    time_cols = [
        c for c in cols
        if pd.api.types.is_datetime64_any_dtype(dtypes[c])
        or c.lower() in {"program_year", "year", "month"}
        or "date" in c.lower()
    ]
    numeric_cols = [
        c for c in cols
        if pd.api.types.is_numeric_dtype(dtypes[c]) and c not in time_cols
    ]

    if time_cols and numeric_cols:
        x, y = time_cols[0], numeric_cols[0]
        sorted_df = df.sort_values(x)
        return px.line(sorted_df, x=x, y=y, title=f"{y} over {x}", markers=True)

    # Categorical bar: exactly 2 columns (1 string + 1 numeric), <= 30 rows.
    # Note: pandas 2.x uses StringDtype() for string columns, not object —
    # use is_string_dtype which handles both.
    if len(cols) == 2 and len(df) <= 30:
        str_cols = [c for c in cols if pd.api.types.is_string_dtype(dtypes[c])
                    and not pd.api.types.is_numeric_dtype(dtypes[c])]
        num_cols = [c for c in cols if pd.api.types.is_numeric_dtype(dtypes[c])]
        if len(str_cols) == 1 and len(num_cols) == 1:
            sorted_df = df.sort_values(num_cols[0], ascending=True)
            return px.bar(
                sorted_df,
                x=num_cols[0],
                y=str_cols[0],
                orientation="h",
                title=f"{num_cols[0]} by {str_cols[0]}",
            )

    return None


def _check_data_freshness() -> str | None:
    """Return a warning string if any CSV under source_dir is newer than the DuckDB file.

    Returns None when the data is fresh (or when source_dir is missing,
    which is a non-issue for a read-only demo host).
    """
    try:
        src = Path(CONFIG["data"]["source_dir"])
        db = Path(CONFIG["data"]["duckdb_path"])
        if not src.exists() or not db.exists():
            return None
        db_mtime = db.stat().st_mtime
        newest_csv = max(
            (p.stat().st_mtime for p in src.rglob("*.csv")),
            default=0.0,
        )
        if newest_csv > db_mtime:
            return (
                "⚠️ **Data may be stale.** One or more CSV files in "
                f"`{src}` are newer than the DuckDB database. Run "
                "`python ingest.py --rebuild` and refresh this page "
                "to query the latest data."
            )
    except Exception:  # noqa: BLE001 — freshness check must never crash startup
        return None
    return None


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 40) -> str:
    """Make a filesystem-safe slug from a free-text question."""
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s[:max_len] or "query"


def _write_full_csv(df: pd.DataFrame, question: str) -> Path:
    """Write the full (uncapped) result to a temp CSV and return the path."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"openpayments-{_slugify(question)}-{stamp}.csv"
    path = Path(tempfile.gettempdir()) / name
    df.to_csv(path, index=False)
    return path


def _build_response_elements(result: dict[str, Any]) -> list:
    """Assemble the table + (optional) chart + CSV download for a successful result."""
    elements: list = []
    df = result.get("data")
    if df is None or df.empty:
        return elements

    max_rows = int(CONFIG["ui"]["max_display_rows"])
    capped = df.head(max_rows)
    elements.append(cl.Dataframe(name="Results", data=capped, display="inline"))

    if CONFIG["ui"].get("show_charts", True):
        fig = _auto_chart(capped)
        if fig is not None:
            elements.append(
                cl.Plotly(name="Chart", figure=fig, display="inline")
            )

    # Full-result CSV download. Always attached, even when the inline
    # table is not truncated, so the demo has a consistent export affordance.
    csv_path = _write_full_csv(df, result.get("question") or "query")
    elements.append(
        cl.File(
            name=f"Download CSV ({len(df):,} rows)",
            path=str(csv_path),
            mime="text/csv",
            display="inline",
        )
    )

    return elements


# --- Chainlit handlers -----------------------------------------------------

@cl.set_starters
async def set_starters() -> list[cl.Starter]:
    """Clickable example prompts rendered on the landing screen."""
    return [cl.Starter(label=s["label"], message=s["message"]) for s in STARTERS]


@cl.on_chat_start
async def on_chat_start() -> None:
    """Initialize the agent for this session and send the greeting."""
    try:
        agent = SQLAgent(CONFIG_PATH)
    except FileNotFoundError as e:
        await cl.ErrorMessage(
            content=(
                f"**DuckDB database not found.**\n\n{e}\n\n"
                "Run `python ingest.py --rebuild` to create it, then refresh "
                "this page."
            )
        ).send()
        return
    except IOError as e:
        await cl.ErrorMessage(
            content=(
                f"**DuckDB file is locked.**\n\n{e}\n\n"
                "Close any other process holding the database (the `duckdb.exe` "
                "CLI is the usual culprit) and refresh this page."
            )
        ).send()
        return
    except Exception as e:  # noqa: BLE001 — startup catch-all is intentional
        await cl.ErrorMessage(
            content=(
                f"**Failed to initialize the agent.**\n\n{e}\n\n"
                "Check that Ollama is running (`ollama serve`) and that the "
                f"model `{CONFIG['model']['name']}` is pulled (`ollama list`)."
            )
        ).send()
        return

    cl.user_session.set("agent", agent)
    cl.user_session.set("chat_history", [])

    # Intentionally no greeting bubble: sending any message here collapses
    # the Chainlit starter landing (big logo + starter buttons) into chat
    # mode. The landing IS the greeting. The description text lives in
    # chainlit.md (accessible via the Readme button). The bottom-left
    # brand watermark in public/branding.css keeps the logo visible
    # during the chat itself.
    stale_warning = _check_data_freshness()
    if stale_warning:
        await cl.Message(content=stale_warning).send()


async def _answer_question(question: str) -> None:
    """Run a single user question end-to-end (used by on_message and
    the follow-up action callback in Phase 5.B)."""
    agent: SQLAgent | None = cl.user_session.get("agent")
    if agent is None:
        await cl.ErrorMessage(
            content="Agent not initialized. Refresh the page to retry."
        ).send()
        return

    chat_history: list[tuple[str, str]] = cl.user_session.get("chat_history") or []

    # Step 1: SQL generation + execution (visible as a collapsible step).
    async with cl.Step(name="Generating SQL", type="tool") as step:
        prep = await cl.make_async(agent.prepare)(question, chat_history)
        if prep.get("sql"):
            step.output = f"```sql\n{prep['sql']}\n```"
        if prep.get("error"):
            prev = step.output or ""
            step.output = (
                f"{prev}\n\n**Error after {prep['attempts']} attempt(s):** "
                f"{prep['error']}"
            )

    # Step 2: error path — plain-English message, raw detail stays in the step.
    if prep.get("error"):
        await cl.ErrorMessage(
            content=(
                "I couldn't answer that after "
                f"{prep['attempts']} attempt(s). This usually means the "
                "question is ambiguous or references data that isn't in "
                "the CMS Open Payments dataset (2021–2024).\n\n"
                "**Things to try:**\n"
                "- Rephrase the question more specifically (name a year, "
                "  company, specialty, or state)\n"
                "- Check the spelling of any names, drugs, or companies\n"
                "- Break a complex question into two simpler ones\n\n"
                "The full technical error is in the *Generating SQL* step above."
            )
        ).send()
        return

    # Step 3: success path — build elements once, then either render
    # the canned text (unsupported / empty df) or stream the summary.
    elements = _build_response_elements(prep)
    canned = prep.get("canned_answer")

    if canned is not None:
        # Unsupported question or empty-result case: fixed text, no streaming,
        # no elements beyond whatever _build_response_elements returned
        # (which will be [] for empty df anyway).
        await cl.Message(content=canned, elements=elements).send()
        answer_for_history = canned
    else:
        # Streamed summary: create an empty message, stream tokens,
        # append the truncation notice if needed, then send() to finalize.
        df = prep["data"]
        sql_actions: list[cl.Action] = []
        if CONFIG["ui"].get("show_copy_sql", True) and prep.get("sql"):
            sql_actions.append(
                cl.Action(
                    name="show_sql",
                    payload={"sql": prep["sql"]},
                    label="📋 Show/Hide SQL",
                    tooltip="Toggle a copyable block of the generated SQL",
                )
            )
        msg = cl.Message(content="", elements=elements, actions=sql_actions)
        parts: list[str] = []
        try:
            async for chunk in agent.stream_summary(
                prep["question"], prep["sql"], df
            ):
                parts.append(chunk)
                await msg.stream_token(chunk)
        except Exception as e:  # noqa: BLE001 — streaming must degrade gracefully
            fallback = (
                f"(Summary unavailable — summarization LLM call failed: {e}) "
                f"The query returned {len(df):,} row(s)."
            )
            parts = [fallback]
            msg.content = fallback

        # Row-truncation notice appended after the streamed summary.
        max_rows = int(CONFIG["ui"]["max_display_rows"])
        if len(df) > max_rows:
            notice = (
                f"\n\n> ℹ️ Showing the first **{max_rows:,}** of "
                f"**{len(df):,}** rows. Use the CSV download below for "
                "the full result."
            )
            msg.content += notice
            parts.append(notice)

        await msg.send()
        answer_for_history = "".join(parts).strip() or "(no summary)"

    # Step 4: update conversation history (last 5 turns).
    chat_history.append((question, answer_for_history))
    cl.user_session.set("chat_history", chat_history[-5:])

    # Step 5: LLM-generated follow-up suggestion buttons. Skipped for
    # canned answers (no data to drill into) and when disabled in config.
    # Wrapped in a broad try/except — follow-ups must never break the
    # main answer.
    if canned is None and CONFIG["ui"].get("show_followups", True):
        try:
            suggestions = await cl.make_async(agent.suggest_followups)(
                question, answer_for_history
            )
        except Exception:  # noqa: BLE001
            suggestions = []
        if suggestions:
            actions = [
                cl.Action(
                    name="followup",
                    payload={"question": s},
                    label=s,
                    tooltip="Click to ask this follow-up question",
                )
                for s in suggestions
            ]
            await cl.Message(
                content="**You might also ask:**",
                actions=actions,
            ).send()


@cl.action_callback("show_sql")
async def on_show_sql(action: cl.Action) -> None:
    """Toggle the generated SQL: first click reveals a copyable SQL
    message, second click removes it. Keyed per-action so each answer's
    Show/Hide SQL button operates independently."""
    sql = (action.payload or {}).get("sql", "").strip()
    if not sql:
        return
    toggles: dict = cl.user_session.get("sql_toggles") or {}
    existing: cl.Message | None = toggles.get(action.id)
    if existing is not None:
        try:
            await existing.remove()
        except Exception:  # noqa: BLE001 — best-effort removal
            pass
        toggles.pop(action.id, None)
    else:
        msg = cl.Message(content=f"```sql\n{sql}\n```", author="SQL")
        await msg.send()
        toggles[action.id] = msg
    cl.user_session.set("sql_toggles", toggles)


@cl.action_callback("followup")
async def on_followup_action(action: cl.Action) -> None:
    """Handle a click on one of the LLM-generated follow-up buttons."""
    try:
        await action.remove()  # prevent double-click / re-fire
    except Exception:  # noqa: BLE001
        pass
    question = (action.payload or {}).get("question", "").strip()
    if not question:
        return
    # Echo the user's choice as a chat message so the transcript reads
    # naturally, then delegate to the same pipeline on_message uses.
    await cl.Message(content=question, author="User", type="user_message").send()
    await _answer_question(question)


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """Thin wrapper — delegates to `_answer_question` so the
    follow-up action callback in Phase 5.B can reuse the same pipeline."""
    await _answer_question(message.content)


@cl.on_chat_end
async def on_chat_end() -> None:
    """Close the per-session DuckDB connection cleanly."""
    agent: SQLAgent | None = cl.user_session.get("agent")
    if agent is not None:
        agent.close()
