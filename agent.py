"""
Open Payments LLM Agent — Text-to-SQL core.

Translates natural-language questions into DuckDB SQL, executes them
against the Phase 1 data layer, retries on SQL errors, and summarizes
the results with a second LLM call. All LLM calls go to a local Ollama
server (zero cost).

Public API:
    from agent import SQLAgent
    agent = SQLAgent("config.yaml")
    result = agent.run_query("Top 10 companies by total payments in 2024", chat_history=[])
    # result = {
    #     "question":    str,
    #     "sql":         str | None,
    #     "data":        pandas.DataFrame | None,
    #     "answer":      str | None,
    #     "error":       str | None,
    #     "attempts":    int,
    #     "elapsed_sec": float,
    # }

CLI smoke test:
    python agent.py

See phase-2-plan.md for the full design rationale.
"""
from __future__ import annotations

import re
import sys
import time
import warnings
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yaml

# Silence the Python 3.14 pydantic v1 deprecation warning emitted at
# `from langchain_ollama import ChatOllama`. It's cosmetic; does not
# affect runtime behavior.
warnings.filterwarnings(
    "ignore",
    message=r".*Pydantic V1.*",
    category=UserWarning,
)

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_ollama import ChatOllama


# --- Key columns (hand-curated for prompt injection) -----------------------

# We can't inline all 252 research columns into the prompt. These are the
# analytically useful ones per table — types are looked up from
# _schema_metadata at SchemaManager init time.
KEY_COLUMNS: dict[str, list[str]] = {
    "general_payments": [
        "Program_Year",
        "Date_of_Payment",
        "Total_Amount_of_Payment_USDollars",
        "Number_of_Payments_Included_in_Total_Amount",
        "Nature_of_Payment_or_Transfer_of_Value",
        "Form_of_Payment_or_Transfer_of_Value",
        "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name",
        "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_State",
        "Covered_Recipient_Type",
        "Covered_Recipient_First_Name",
        "Covered_Recipient_Last_Name",
        "Covered_Recipient_NPI",
        "Covered_Recipient_Specialty_1",
        "Recipient_City",
        "Recipient_State",
        "Recipient_Country",
        "Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1",
        "Product_Category_or_Therapeutic_Area_1",
        "Physician_Ownership_Indicator",
        "Teaching_Hospital_Name",
        "Teaching_Hospital_CCN",
    ],
    "research_payments": [
        "Program_Year",
        "Total_Amount_of_Payment_USDollars",
        "Name_of_Study",
        "Context_of_Research",
        "Preclinical_Research_Indicator",
        "ClinicalTrials_Gov_Identifier",
        "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name",
        "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_State",
        "Principal_Investigator_1_First_Name",
        "Principal_Investigator_1_Last_Name",
        "Principal_Investigator_1_NPI",
        "Principal_Investigator_1_Specialty_1",
        "Principal_Investigator_1_State",
        "Product_Category_or_Therapeutic_Area_1",
        "Expenditure_Category1",
        "Recipient_City",
        "Recipient_State",
    ],
    "ownership_payments": [
        "Program_Year",
        "Physician_First_Name",
        "Physician_Last_Name",
        "Physician_NPI",
        "Physician_Specialty",
        "Recipient_State",
        "Recipient_City",
        "Total_Amount_Invested_USDollars",
        "Value_of_Interest",
        "Terms_of_Interest",
        "Interest_Held_by_Physician_or_an_Immediate_Family_Member",
        "Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name",
    ],
    "removed_deleted": [
        "Change_Type",
        "Program_Year",
        "Payment_Type",
        "Record_ID",
    ],
}


# --- Prompt templates ------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """\
You are a DuckDB SQL analyst for the CMS Open Payments dataset (calendar years 2021-2024).
Your job is to translate the user's question into ONE DuckDB SQL query.

Schema (key columns only; more columns exist but rarely matter):
{compact_schema}

Per-year tables (replace YYYY with 2021, 2022, 2023, or 2024):
  general_payments_YYYY
  research_payments_YYYY
  ownership_payments_YYYY
  removed_deleted_YYYY

UNION ALL views spanning every year:
  all_general_payments, all_research_payments, all_ownership_payments, all_removed_deleted

Rules:
1. Output ONLY the SQL query. No prose, no explanation, no markdown code fences.
2. Use DuckDB syntax (not PostgreSQL or MySQL). Use DATE_TRUNC('month', Date_of_Payment) for monthly aggregation.
3. Column names are Snake_Case and case-sensitive. Copy them EXACTLY as shown in the schema.
4. For single-year questions, query the per-year table (e.g. general_payments_2024).
   For cross-year questions, query the matching all_* view.
5. Always add `LIMIT 100` unless the user explicitly asks for all rows or a specific larger limit.
6. Monetary amounts: Total_Amount_of_Payment_USDollars (general/research), Total_Amount_Invested_USDollars (ownership).
7. The paying company is Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name.
8. Name and string filters: NEVER use case-sensitive `=` on name columns (first name, last name, company name, drug name, hospital name, city, specialty). Always use `ILIKE` so the user does not have to guess the stored casing. For exact-name lookups, use `ILIKE 'value'`; for partial matches, use `ILIKE '%value%'`. This applies to BOTH single-column filters and multi-column filters (e.g. first name AND last name).
9. If the question is unrelated to CMS Open Payments AND the chat history shows no prior on-topic exchange, respond with exactly: SELECT 'unsupported' AS note;
   However, if the chat history shows the user is refining, retrying, or follow-up-asking about a prior on-topic question (e.g. "try case-insensitive", "what about 2023?", "show me the chart"), treat the new question as on-topic and answer it.
"""

SUMMARIZE_PROMPT_TEMPLATE = """\
You are answering a user's question about CMS Open Payments data.

Question: {question}

The analyst ran this SQL:
{sql}

And got these results (first 20 rows as CSV):
{csv_preview}

Total rows in the full result: {total_rows}

Write a concise 2-4 sentence answer in plain English. Reference specific numbers
from the data when relevant. Do not make up values. Do not restate the SQL.
"""

FOLLOWUP_PROMPT_TEMPLATE = """\
You just answered a question about CMS Open Payments data.

Original question: {question}

Answer you gave: {answer}

Suggest exactly 3 short follow-up questions a CMS Open Payments analyst
would naturally ask next, given that answer. Rules:
- One question per line.
- No numbering, no bullets, no quotes, no prose before or after.
- Each question max 70 characters.
- Questions must be about the same CMS Open Payments dataset (2021-2024).
- Prefer drill-downs (by year, state, specialty, product) over brand-new topics.
"""

EMPTY_RESULT_MESSAGE = (
    "No matching records were found for that query. A few things to try:\n"
    "- Double-check the spelling of any names, companies, or drugs.\n"
    "- Try a partial match (e.g. just the last name) — I search case-insensitively.\n"
    "- Broaden the year range or remove other filters.\n"
    "- Confirm the entity is in the CMS Open Payments dataset (2021-2024)."
)

UNSUPPORTED_MESSAGE = (
    "I can only answer questions about the CMS Open Payments dataset "
    "(pharmaceutical and medical device payments to physicians, 2021-2024). "
    "Please rephrase your question to focus on that data."
)


# --- SQL extraction --------------------------------------------------------

_SQL_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_sql(text: str) -> str:
    """Strip markdown code fences if present and return the SQL body."""
    if not text:
        return ""
    m = _SQL_FENCE_RE.search(text)
    if m:
        return m.group(1).strip()
    return text.strip()


# --- SchemaManager ---------------------------------------------------------

class SchemaManager:
    """Builds the compact schema string used in the system prompt.

    Types are read from the **actual** DuckDB views via information_schema
    (not from _schema_metadata) because the data dictionaries and the
    Parquet-inferred types occasionally disagree — e.g. `Program_Year` is
    BIGINT in Parquet but was declared 'integer' → BIGINT in the dictionary,
    yet `Physician_Ownership_Indicator` ended up BOOLEAN in Parquet but
    'string' → VARCHAR in the dictionary. The LLM's SQL runs against the
    views, so the view types are the ones that matter.
    """

    # One representative per-year view per table_type is enough —
    # schemas are identical across years.
    _REP_VIEW = {
        "general_payments": "general_payments_2024",
        "research_payments": "research_payments_2024",
        "ownership_payments": "ownership_payments_2024",
        "removed_deleted": "removed_deleted_2024",
    }

    def __init__(self, con: duckdb.DuckDBPyConnection) -> None:
        self._types: dict[tuple[str, str], str] = {}
        for table_type, view_name in self._REP_VIEW.items():
            rows = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = ?",
                [view_name],
            ).fetchall()
            for column_name, data_type in rows:
                self._types[(table_type, column_name)] = data_type or "?"

    def compact_schema(self) -> str:
        lines: list[str] = []
        for table_type, cols in KEY_COLUMNS.items():
            lines.append(f"  {table_type}:")
            for col in cols:
                dtype = self._types.get((table_type, col), "?")
                lines.append(f"    - {col} [{dtype}]")
        return "\n".join(lines)


# --- SQLAgent --------------------------------------------------------------

class SQLAgent:
    """Main agent. One instance per chat session."""

    def __init__(self, config_path: str = "config.yaml") -> None:
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        self.cfg = cfg
        self.max_retries: int = int(cfg["model"]["max_retries"])
        self.timeout: int = int(cfg["model"]["timeout"])

        duckdb_path = Path(cfg["data"]["duckdb_path"]).resolve()
        if not duckdb_path.exists():
            raise FileNotFoundError(
                f"DuckDB file not found at {duckdb_path}. "
                "Run `python ingest.py --rebuild` first."
            )
        try:
            self.con = duckdb.connect(str(duckdb_path), read_only=True)
        except duckdb.IOException as e:
            raise IOError(
                f"Could not open {duckdb_path} in read-only mode: {e}. "
                "Another process (e.g. `duckdb.exe` CLI) may be holding "
                "an exclusive lock. Close it and try again."
            ) from e

        self.schema = SchemaManager(self.con)
        self.system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            compact_schema=self.schema.compact_schema()
        )

        self.llm_sql = ChatOllama(
            model=cfg["model"]["name"],
            base_url=cfg["model"]["base_url"],
            temperature=float(cfg["model"]["temperature"]),
        )
        self.llm_summary = ChatOllama(
            model=cfg["model"]["name"],
            base_url=cfg["model"]["base_url"],
            temperature=float(cfg["model"]["summarization_temperature"]),
        )

    # --- LLM call construction -------------------------------------------

    def _build_messages(
        self,
        question: str,
        error_context: str | None,
        chat_history: list[tuple[str, str]],
    ) -> list:
        msgs: list = [SystemMessage(content=self.system_prompt)]

        # Last 4 exchanges for conversational context.
        for prev_q, prev_a in chat_history[-4:]:
            msgs.append(HumanMessage(content=prev_q))
            msgs.append(AIMessage(content=prev_a))

        if error_context:
            user_content = (
                f"{question}\n\n"
                f"(Retry context — your previous attempt failed:\n{error_context})"
            )
        else:
            user_content = question
        msgs.append(HumanMessage(content=user_content))
        return msgs

    def _generate_sql(
        self,
        question: str,
        error_context: str | None,
        chat_history: list[tuple[str, str]],
    ) -> str:
        messages = self._build_messages(question, error_context, chat_history)
        response = self.llm_sql.invoke(messages)
        return extract_sql(response.content)

    def _summary_prompt(self, question: str, sql: str, df: pd.DataFrame) -> str:
        preview = df.head(20).to_csv(index=False)
        return SUMMARIZE_PROMPT_TEMPLATE.format(
            question=question,
            sql=sql,
            csv_preview=preview,
            total_rows=len(df),
        )

    def _summarize(self, question: str, sql: str, df: pd.DataFrame) -> str:
        """Blocking summary (used by run_query back-compat wrapper)."""
        if df.empty:
            return EMPTY_RESULT_MESSAGE
        prompt = self._summary_prompt(question, sql, df)
        response = self.llm_summary.invoke([HumanMessage(content=prompt)])
        return response.content.strip()

    async def stream_summary(
        self,
        question: str,
        sql: str,
        df: pd.DataFrame,
    ):
        """Async generator yielding summary text chunks.

        For empty/canned cases the caller should use `prep["canned_answer"]`
        directly instead of streaming — this method is only for the
        success path with a non-empty DataFrame.
        """
        prompt = self._summary_prompt(question, sql, df)
        async for chunk in self.llm_summary.astream([HumanMessage(content=prompt)]):
            text = getattr(chunk, "content", "") or ""
            if text:
                yield text

    def suggest_followups(
        self,
        question: str,
        answer: str,
        max_suggestions: int = 3,
    ) -> list[str]:
        """Generate up to `max_suggestions` follow-up questions.

        Failures are swallowed — the caller gets an empty list rather than
        an exception, because follow-ups must never break the main answer.
        """
        try:
            prompt = FOLLOWUP_PROMPT_TEMPLATE.format(
                question=question,
                answer=answer,
            )
            response = self.llm_summary.invoke([HumanMessage(content=prompt)])
            raw = (response.content or "").strip()
            lines = [line.strip(" -•\"'\t") for line in raw.splitlines()]
            lines = [line for line in lines if line and len(line) <= 120]
            return lines[:max_suggestions]
        except Exception:
            return []

    # --- Main entry points -----------------------------------------------

    def prepare(
        self,
        question: str,
        chat_history: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Generate + execute SQL, but do NOT run the summary LLM call.

        Returns a dict with:
          - question, sql, data, error, attempts, elapsed_sec
          - canned_answer: str | None — set for unsupported / empty-df
            cases where the caller should skip streaming and use this
            fixed text instead
        On success with non-empty data, canned_answer is None and the
        caller should stream the summary via `stream_summary(...)`.
        """
        if chat_history is None:
            chat_history = []

        start = time.perf_counter()
        error_context: str | None = None
        last_sql: str | None = None
        last_err: str | None = None
        total_attempts = self.max_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                sql = self._generate_sql(question, error_context, chat_history)
            except Exception as e:
                return self._error_result(
                    question, None, f"LLM call failed: {e}", attempt, start
                )

            last_sql = sql

            if not sql:
                error_context = "You returned an empty response. Return valid DuckDB SQL."
                last_err = "empty LLM response"
                continue

            if sql.strip().lower().startswith("select 'unsupported'"):
                return {
                    "question": question,
                    "sql": sql,
                    "data": pd.DataFrame(),
                    "canned_answer": UNSUPPORTED_MESSAGE,
                    "error": None,
                    "attempts": attempt,
                    "elapsed_sec": time.perf_counter() - start,
                }

            try:
                df = self.con.execute(sql).fetchdf()
            except Exception as e:
                last_err = str(e)
                error_context = (
                    f"Your previous SQL failed with this DuckDB error:\n{e}\n\n"
                    f"Previous SQL was:\n{sql}\n\n"
                    "Fix the query and return only the corrected SQL."
                )
                continue

            canned = EMPTY_RESULT_MESSAGE if df.empty else None
            return {
                "question": question,
                "sql": sql,
                "data": df,
                "canned_answer": canned,
                "error": None,
                "attempts": attempt,
                "elapsed_sec": time.perf_counter() - start,
            }

        return self._error_result(
            question, last_sql, last_err or "unknown error", total_attempts, start
        )

    def run_query(
        self,
        question: str,
        chat_history: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """Blocking end-to-end: prepare + summarize. Back-compat wrapper.

        Used by the CLI REPL and `smoke-test-agent.py`. The Chainlit app
        uses `prepare` + `stream_summary` directly so the summary can
        stream token-by-token.
        """
        prep = self.prepare(question, chat_history)
        if prep.get("error"):
            prep["answer"] = None
            return prep
        if prep.get("canned_answer") is not None:
            prep["answer"] = prep.pop("canned_answer")
            return prep
        try:
            answer = self._summarize(question, prep["sql"], prep["data"])
        except Exception as e:
            answer = (
                f"(Summary unavailable — summarization LLM call failed: {e}) "
                f"The query returned {len(prep['data'])} row(s)."
            )
        prep["answer"] = answer
        prep.pop("canned_answer", None)
        return prep

    @staticmethod
    def _error_result(
        question: str,
        sql: str | None,
        err: str,
        attempts: int,
        start: float,
    ) -> dict[str, Any]:
        return {
            "question": question,
            "sql": sql,
            "data": None,
            "answer": None,
            "error": err,
            "attempts": attempts,
            "elapsed_sec": time.perf_counter() - start,
        }

    def close(self) -> None:
        try:
            self.con.close()
        except Exception:
            pass


# --- CLI smoke test --------------------------------------------------------

def _pretty_print(result: dict[str, Any]) -> None:
    print()
    print("-" * 72)
    print(f"Q: {result['question']}")
    print(f"Attempts: {result['attempts']}   Elapsed: {result['elapsed_sec']:.1f}s")
    if result["error"]:
        print(f"ERROR: {result['error']}")
        if result["sql"]:
            print(f"Last SQL:\n{result['sql']}")
        print("-" * 72)
        return
    print(f"SQL:\n{result['sql']}")
    print(f"\nAnswer: {result['answer']}")
    df = result["data"]
    if df is not None and not df.empty:
        with pd.option_context(
            "display.max_columns", 10,
            "display.width", 160,
            "display.max_colwidth", 40,
        ):
            print(f"\nData ({len(df)} rows):")
            print(df.head(10).to_string(index=False))
    print("-" * 72)


def _cli() -> int:
    print("Open Payments Agent — REPL. Type 'exit' or Ctrl-C to quit.")
    try:
        agent = SQLAgent("config.yaml")
    except Exception as e:
        print(f"Failed to initialize agent: {e}", file=sys.stderr)
        return 1

    history: list[tuple[str, str]] = []
    try:
        while True:
            try:
                q = input("\n> ").strip()
            except EOFError:
                break
            if not q:
                continue
            if q.lower() in {"exit", "quit"}:
                break
            result = agent.run_query(q, history)
            _pretty_print(result)
            if result.get("answer"):
                history.append((q, result["answer"]))
                history = history[-4:]
    finally:
        agent.close()
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
