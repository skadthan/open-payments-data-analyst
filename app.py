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
    "Welcome to the **Open Payments Data Analyst**.\n\n"
    "I can answer questions about CMS Open Payments — pharmaceutical and "
    "medical device industry payments to U.S. physicians and teaching "
    "hospitals from **2021 through 2024** (about 55 million records).\n\n"
    "Try asking:\n"
    "- *Top 10 companies by total payment amount across all years*\n"
    "- *Which medical specialties received the most general payments in 2024?*\n"
    "- *How many physicians have ownership interests across all years?*"
)


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


def _build_response_elements(result: dict[str, Any]) -> list:
    """Assemble the table + (optional) chart for a successful result."""
    elements: list = []
    df = result.get("data")
    if df is None or df.empty:
        return elements

    capped = df.head(int(CONFIG["ui"]["max_display_rows"]))
    elements.append(cl.Dataframe(name="Results", data=capped, display="inline"))

    if CONFIG["ui"].get("show_charts", True):
        fig = _auto_chart(capped)
        if fig is not None:
            elements.append(
                cl.Plotly(name="Chart", figure=fig, display="inline")
            )

    return elements


# --- Chainlit handlers -----------------------------------------------------

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

    await cl.Message(content=GREETING).send()


@cl.on_message
async def on_message(message: cl.Message) -> None:
    """Handle one user turn end-to-end."""
    agent: SQLAgent | None = cl.user_session.get("agent")
    if agent is None:
        await cl.ErrorMessage(
            content="Agent not initialized. Refresh the page to retry."
        ).send()
        return

    chat_history: list[tuple[str, str]] = cl.user_session.get("chat_history") or []

    # Step 1: SQL generation + execution (visible as a collapsible step).
    async with cl.Step(name="Generating SQL", type="tool") as step:
        result = await cl.make_async(agent.run_query)(
            message.content, chat_history
        )
        if result.get("sql"):
            step.output = f"```sql\n{result['sql']}\n```"
        if result.get("error"):
            prev = step.output or ""
            step.output = (
                f"{prev}\n\n**Error after {result['attempts']} attempt(s):** "
                f"{result['error']}"
            )

    # Step 2: error path — show a friendly error and stop.
    if result.get("error"):
        await cl.ErrorMessage(
            content=(
                "Sorry, I couldn't answer that. The agent retried "
                f"{result['attempts']} time(s) and the last DuckDB error was:\n\n"
                f"```\n{result['error']}\n```"
            )
        ).send()
        return

    # Step 3: success path — answer + table + (optional) chart.
    elements = _build_response_elements(result)
    answer = result.get("answer") or "(no summary available)"
    await cl.Message(content=answer, elements=elements).send()

    # Step 4: update conversation history (last 5 turns).
    chat_history.append((message.content, result.get("answer") or ""))
    cl.user_session.set("chat_history", chat_history[-5:])


@cl.on_chat_end
async def on_chat_end() -> None:
    """Close the per-session DuckDB connection cleanly."""
    agent: SQLAgent | None = cl.user_session.get("agent")
    if agent is not None:
        agent.close()
