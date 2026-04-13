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

_FROM_TABLE_RE = re.compile(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)


def _first_from_table(sql: str | None) -> str | None:
    """Return the first `FROM <table>` identifier in a SQL string, or None."""
    if not sql:
        return None
    m = _FROM_TABLE_RE.search(sql)
    return m.group(1) if m else None

import chainlit as cl
import pandas as pd
import plotly.express as px
import yaml

from agent import SQLAgent, PROVIDER_DEFAULTS, get_ollama_models, create_llm
from rag import DocumentRAG, classify_question, build_rag_prompt


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
    "**2018 through 2024** (about 80+ million records across general "
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
        "message": "Compare total general payments by year from 2018 to 2024",
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


def _write_full_xlsx(df: pd.DataFrame, question: str) -> Path | None:
    """Write the full result to a temp .xlsx file and return the path.

    Returns None if openpyxl is not installed — callers should treat a
    None return as "no Excel export this session" and fall back to CSV
    only. Adding openpyxl to requirements.txt and reinstalling enables
    this without any code change.
    """
    try:
        import openpyxl  # noqa: F401 — imported for availability check
    except ImportError:
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"openpayments-{_slugify(question)}-{stamp}.xlsx"
    path = Path(tempfile.gettempdir()) / name
    df.to_excel(path, index=False, engine="openpyxl")
    return path


def _write_session_pdf(session_log: list[dict[str, Any]]) -> Path | None:
    """Render the full session Q/A log to a temp PDF and return the path.

    Returns None if reportlab is not installed OR the session log is
    empty. The demo affordance is "take this analysis home" — each
    entry includes the question, generated SQL, and plain-English
    answer. Designed for a compliance-minded audience, so we lean
    toward auditability (SQL visible, timestamp per Q).
    """
    if not session_log:
        return None
    try:
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.lib.enums import TA_LEFT
        from reportlab.platypus import (
            SimpleDocTemplate,
            Paragraph,
            Spacer,
            Preformatted,
            PageBreak,
        )
    except ImportError:
        return None

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = Path(tempfile.gettempdir()) / f"openpayments-session-{stamp}.pdf"

    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    body = styles["BodyText"]
    meta = ParagraphStyle(
        "meta",
        parent=body,
        fontSize=8,
        textColor="#666666",
        spaceAfter=8,
        alignment=TA_LEFT,
    )
    mono = ParagraphStyle(
        "mono",
        parent=body,
        fontName="Courier",
        fontSize=8,
        leading=10,
        leftIndent=12,
        textColor="#222222",
    )

    doc = SimpleDocTemplate(
        str(path),
        pagesize=LETTER,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
        title="CMS Open Payments — Session Report",
    )

    story: list = []
    story.append(Paragraph("CMS Open Payments — Session Report", h1))
    story.append(Paragraph(
        f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · "
        f"{len(session_log)} question(s)",
        meta,
    ))
    story.append(Spacer(1, 0.15 * inch))

    def _escape(s: str) -> str:
        return (
            (s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    for i, entry in enumerate(session_log, start=1):
        story.append(Paragraph(f"Q{i}. {_escape(entry.get('question', ''))}", h2))
        ts = entry.get("timestamp")
        if ts:
            story.append(Paragraph(f"Asked at {ts}", meta))
        sql = entry.get("sql") or ""
        if sql:
            story.append(Paragraph("<b>Generated SQL:</b>", body))
            story.append(Preformatted(sql, mono))
            story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph("<b>Answer:</b>", body))
        story.append(Paragraph(_escape(entry.get("answer", "")), body))
        story.append(Spacer(1, 0.18 * inch))

    doc.build(story)
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
    question = result.get("question") or "query"
    csv_path = _write_full_csv(df, question)
    elements.append(
        cl.File(
            name=f"Download CSV ({len(df):,} rows)",
            path=str(csv_path),
            mime="text/csv",
            display="inline",
        )
    )

    # R3.4 — Excel export alongside CSV. Silently skipped if openpyxl
    # is not installed; analysts in a healthcare/compliance setting
    # expect an .xlsx download.
    xlsx_path = _write_full_xlsx(df, question)
    if xlsx_path is not None:
        elements.append(
            cl.File(
                name=f"Download Excel ({len(df):,} rows)",
                path=str(xlsx_path),
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                display="inline",
            )
        )

    return elements


# --- Model settings helpers ------------------------------------------------

# Display labels for the "Model" dropdown.  Keys are "provider/model" so
# the provider can be derived when the user picks one — no dynamic rebuild.
_PROVIDER_LABELS = {
    "ollama": "Ollama (Local)",
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google AI",
    "deepseek": "DeepSeek",
}


def _build_all_model_items() -> dict[str, str]:
    """Build a combined ``{label: value}`` dict for the model Select widget.

    Chainlit's ``Select(items=...)`` maps ``{label: value}`` — the *label*
    is shown in the UI and the *value* is returned in settings.  We want
    the returned value to be ``provider/model`` so the handler can parse it.
    """
    items: dict[str, str] = {}
    for provider, preset in PROVIDER_DEFAULTS.items():
        label_prefix = _PROVIDER_LABELS.get(provider, provider)
        if provider == "ollama":
            models = get_ollama_models(
                CONFIG["model"].get("base_url", "http://localhost:11434")
            )
            if not models:
                models = [CONFIG["model"]["name"]]
        else:
            models = preset.get("models", [])
        for m in models:
            label = f"{label_prefix} — {m}"
            value = f"{provider}/{m}"
            items[label] = value
    return items


def _build_settings_widgets() -> list:
    """Build the ChatSettings input widgets (called once at session start)."""
    model_items = _build_all_model_items()

    # Default selection: current Ollama model from config.
    default_provider = CONFIG["model"].get("provider", "ollama")
    default_model = CONFIG["model"]["name"]
    default_value = f"{default_provider}/{default_model}"
    if default_value not in model_items.values():
        default_value = next(iter(model_items.values()))

    return [
        cl.input_widget.Select(
            id="model",
            label="AI Model",
            items=model_items,
            initial_value=default_value,
            description="Pick a provider and model. Cloud models require an API key below.",
        ),
        cl.input_widget.TextInput(
            id="api_key",
            label="API Key (required for cloud models)",
            initial="",
            placeholder="sk-... / anthropic-... (session only, never saved to disk)",
        ),
        cl.input_widget.Slider(
            id="temperature",
            label="Temperature",
            min=0.0,
            max=1.0,
            step=0.1,
            initial=float(CONFIG["model"]["temperature"]),
        ),
    ]


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
    cl.user_session.set("session_log", [])
    cl.user_session.set("corrections", [])

    # Initialize RAG (optional — SQL pipeline works without it).
    rag_instance = None
    if CONFIG.get("rag", {}).get("enabled", False):
        try:
            rag_instance = DocumentRAG(CONFIG)
            if not rag_instance.is_available():
                rag_instance = None
        except Exception:
            rag_instance = None
    cl.user_session.set("rag", rag_instance)

    # Send settings panel (gear icon in the UI).
    settings = cl.ChatSettings(inputs=_build_settings_widgets())
    await settings.send()

    stale_warning = _check_data_freshness()
    if stale_warning:
        await cl.Message(content=stale_warning).send()


@cl.on_settings_update
async def on_settings_update(settings: dict) -> None:
    """Re-initialize LLM clients when the user changes model/key."""
    agent: SQLAgent | None = cl.user_session.get("agent")
    if agent is None:
        return

    # Model value is "provider/model" (e.g. "openai/gpt-4o").
    model_key = settings.get("model", "")
    if "/" not in model_key:
        await cl.ErrorMessage(content="**Invalid model selection.**").send()
        return
    new_provider, new_model = model_key.split("/", 1)

    api_key = (settings.get("api_key") or "").strip() or None
    temperature = float(settings.get("temperature", 0.1))

    # Validate: cloud providers need an API key.
    needs_key = PROVIDER_DEFAULTS.get(new_provider, {}).get("needs_api_key", False)
    if needs_key and not api_key:
        provider_label = _PROVIDER_LABELS.get(new_provider, new_provider)
        await cl.ErrorMessage(
            content=f"**API key required.** Enter your {provider_label} API key in the settings panel, then confirm again."
        ).send()
        return

    try:
        agent.swap_llm(
            provider=new_provider,
            model=new_model,
            api_key=api_key,
            temperature=temperature,
        )
    except Exception as e:
        await cl.ErrorMessage(
            content=f"**Failed to switch model.**\n\n{e}"
        ).send()
        return

    provider_label = _PROVIDER_LABELS.get(new_provider, new_provider)
    await cl.Message(
        content=f"Switched to **{provider_label}** / `{new_model}` (temperature {temperature})"
    ).send()


async def _answer_rag_question(question: str, agent: SQLAgent, rag_instance: DocumentRAG) -> str | None:
    """Handle a pure-RAG question. Returns the answer text, or None on failure."""
    async with cl.Step(name="Searching CMS documentation", type="tool") as step:
        chunks = await cl.make_async(rag_instance.query)(question)
        if not chunks:
            step.output = "No relevant documentation found."
            return None
        sources = set()
        for c in chunks:
            sources.add(f"{c['source_file']} (p.{c['page_number']})")
        step.output = f"Found {len(chunks)} relevant excerpts from: {', '.join(sorted(sources))}"

    prompt = build_rag_prompt(question, chunks)
    msg = cl.Message(content="")
    parts: list[str] = []
    try:
        async for chunk_text in agent.stream_rag_answer(prompt):
            parts.append(chunk_text)
            await msg.stream_token(chunk_text)
    except Exception as e:
        fallback = f"(RAG answer unavailable — LLM call failed: {e})"
        parts = [fallback]
        msg.content = fallback

    # Source attribution footer.
    source_lines = []
    for c in chunks[:3]:  # Top 3 sources.
        source_lines.append(f"- {c['source_file']}, page {c['page_number']}")
    footer = "\n\n---\n📚 **Sources:**\n" + "\n".join(source_lines)
    msg.content += footer
    parts.append(footer)
    await msg.send()
    return "".join(parts).strip()


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

    # Query routing: classify as sql, rag, or hybrid.
    rag_instance: DocumentRAG | None = cl.user_session.get("rag")
    route = classify_question(question, rag_available=rag_instance is not None)

    if route == "rag" and rag_instance is not None:
        answer = await _answer_rag_question(question, agent, rag_instance)
        if answer:
            chat_history.append((question, answer))
            cl.user_session.set("chat_history", chat_history[-5:])
            session_log: list[dict[str, Any]] = cl.user_session.get("session_log") or []
            session_log.append({
                "question": question,
                "sql": None,
                "answer": answer,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            cl.user_session.set("session_log", session_log)
            return
        # If RAG returned nothing, fall through to SQL path.

    # For hybrid route, retrieve RAG context and inject it into the agent's
    # system prompt temporarily so the LLM can use domain knowledge.
    rag_context_injected = False
    if route == "hybrid" and rag_instance is not None:
        try:
            chunks = rag_instance.query(question, top_k=3)
            if chunks:
                context_parts = []
                for c in chunks:
                    text_snippet = c["text"][:500]
                    context_parts.append(
                        f"[{c['source_file']}, p.{c['page_number']}]: {text_snippet}"
                    )
                rag_supplement = (
                    "\n\nRelevant CMS documentation for context:\n"
                    + "\n---\n".join(context_parts)
                )
                agent._original_system_prompt = agent.system_prompt
                agent.system_prompt = agent.system_prompt + rag_supplement
                rag_context_injected = True
        except Exception:
            pass  # Hybrid context is optional; SQL still works without it.

    # Step 1: SQL generation + execution (visible as a collapsible step).
    async with cl.Step(name="Generating SQL", type="tool") as step:
        prep = await cl.make_async(agent.prepare)(question, chat_history)

        # R3.2 — visible self-correction. When the retry loop kicked in
        # (attempts > 1), show each failed attempt + its DuckDB error,
        # then the final (successful or last-tried) SQL. When it didn't,
        # fall back to the original single-block render.
        history: list[tuple[str | None, str]] = prep.get("attempt_history") or []
        if history:
            chunks: list[str] = []
            for i, (bad_sql, err) in enumerate(history, start=1):
                first_line = (err or "").splitlines()[0][:200] if err else "empty response"
                if bad_sql:
                    chunks.append(
                        f"**Attempt {i} — failed:** {first_line}\n"
                        f"```sql\n{bad_sql}\n```"
                    )
                else:
                    chunks.append(f"**Attempt {i} — failed:** {first_line}")
            if prep.get("sql") and not prep.get("error"):
                chunks.append(
                    f"**Attempt {len(history) + 1} — succeeded ✓**\n"
                    f"```sql\n{prep['sql']}\n```"
                )
            elif prep.get("sql"):
                chunks.append(
                    f"**Last SQL tried:**\n```sql\n{prep['sql']}\n```"
                )
            step.output = "\n\n".join(chunks)
        elif prep.get("sql"):
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
                "the CMS Open Payments dataset (2018–2024).\n\n"
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
        # R3.8 — Export-to-PDF action alongside Show/Hide SQL. Attached to
        # every streamed assistant answer; clicking it builds a PDF from
        # the full session log accumulated so far.
        actions: list[cl.Action] = list(sql_actions)
        actions.append(
            cl.Action(
                name="export_pdf",
                payload={},
                label="📄 Export session to PDF",
                tooltip="Download a PDF of every Q/A in this session so far",
            )
        )
        # Feedback buttons — thumbs up/down to rate and improve the AI.
        actions.append(
            cl.Action(
                name="feedback_up",
                payload={"question": question},
                label="👍",
                tooltip="This answer was helpful",
            )
        )
        actions.append(
            cl.Action(
                name="feedback_down",
                payload={"question": question, "sql": prep.get("sql", "")},
                label="👎",
                tooltip="This answer was wrong — click to tell me what to fix",
            )
        )
        msg = cl.Message(content="", elements=elements, actions=actions)
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

        # R3.1 + R3.6 — performance and provenance footer, appended to
        # every successful streamed summary. One block so it renders as
        # a single muted section under the narrative answer.
        elapsed = prep.get("elapsed_sec") or 0.0
        table = _first_from_table(prep.get("sql"))
        footer_parts = [f"⚡ Answered in **{elapsed:.1f}s**"]
        if table:
            footer_parts.append(
                f"Source: `{table}` · **{len(df):,}** rows matched"
            )
        footer = "\n\n---\n" + " · ".join(footer_parts)
        msg.content += footer
        parts.append(footer)

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

    # Step 4: update conversation history (last 5 turns) and the
    # untrimmed session log used by the Export-to-PDF action.
    chat_history.append((question, answer_for_history))
    cl.user_session.set("chat_history", chat_history[-5:])

    session_log: list[dict[str, Any]] = cl.user_session.get("session_log") or []
    session_log.append({
        "question": question,
        "sql": prep.get("sql"),
        "answer": answer_for_history,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    cl.user_session.set("session_log", session_log)

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

    # Restore original system prompt if hybrid RAG context was injected.
    if rag_context_injected:
        agent.system_prompt = agent._original_system_prompt


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


@cl.action_callback("export_pdf")
async def on_export_pdf(action: cl.Action) -> None:
    """Build a PDF of the whole session so far and attach it as a download."""
    session_log: list[dict[str, Any]] = cl.user_session.get("session_log") or []
    if not session_log:
        await cl.Message(
            content="Nothing to export yet — ask at least one question first.",
        ).send()
        return
    pdf_path = _write_session_pdf(session_log)
    if pdf_path is None:
        await cl.ErrorMessage(
            content=(
                "PDF export unavailable — `reportlab` is not installed. "
                "Run `pip install reportlab` (already listed in "
                "requirements.txt) and try again."
            )
        ).send()
        return
    await cl.Message(
        content=f"Session report ready — **{len(session_log)}** question(s) included.",
        elements=[
            cl.File(
                name=f"openpayments-session-{datetime.now().strftime('%Y%m%d-%H%M%S')}.pdf",
                path=str(pdf_path),
                mime="application/pdf",
                display="inline",
            )
        ],
    ).send()


@cl.action_callback("feedback_up")
async def on_feedback_up(action: cl.Action) -> None:
    """Acknowledge positive feedback."""
    try:
        await action.remove()
    except Exception:
        pass
    await cl.Message(content="Thanks for the feedback!").send()


@cl.action_callback("feedback_down")
async def on_feedback_down(action: cl.Action) -> None:
    """Prompt user for correction text, then inject it into the agent."""
    try:
        await action.remove()
    except Exception:
        pass

    # Ask the user what was wrong.
    res = await cl.AskUserMessage(
        content="What was wrong with this answer? Your feedback will improve future responses in this session.",
        timeout=120,
    ).send()

    if res is None or not res.get("output", "").strip():
        await cl.Message(content="No feedback received — skipped.").send()
        return

    correction = res["output"].strip()
    question = (action.payload or {}).get("question", "")
    sql = (action.payload or {}).get("sql", "")

    # Build a contextual correction for the agent.
    correction_entry = correction
    if question:
        correction_entry = f'For the question "{question}": {correction}'

    corrections: list[str] = cl.user_session.get("corrections") or []
    corrections.append(correction_entry)
    cl.user_session.set("corrections", corrections)

    agent: SQLAgent | None = cl.user_session.get("agent")
    if agent:
        agent.add_correction(correction_entry)

    await cl.Message(
        content=f"Got it — I'll apply this correction to future queries in this session:\n> {correction}"
    ).send()


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
