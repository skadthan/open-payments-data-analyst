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

import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_ollama import ChatOllama


# --- Multi-provider LLM factory -------------------------------------------

# Provider presets: default models and base URLs.
PROVIDER_DEFAULTS: dict[str, dict[str, Any]] = {
    "ollama": {
        "models": [],  # populated dynamically from `ollama list`
        "default_model": "qwen2.5-coder:14b",
        "base_url": "http://localhost:11434",
        "needs_api_key": False,
    },
    "openai": {
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o3-mini"],
        "default_model": "gpt-4o-mini",
        "needs_api_key": True,
    },
    "anthropic": {
        "models": [
            "claude-sonnet-4-20250514",
            "claude-haiku-4-5-20251001",
            "claude-opus-4-20250514",
        ],
        "default_model": "claude-sonnet-4-20250514",
        "needs_api_key": True,
    },
    "google": {
        "models": ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"],
        "default_model": "gemini-2.5-flash",
        "needs_api_key": True,
    },
    "deepseek": {
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "default_model": "deepseek-chat",
        "base_url": "https://api.deepseek.com/v1",
        "needs_api_key": True,
    },
}


def get_ollama_models(base_url: str = "http://localhost:11434") -> list[str]:
    """Query local Ollama for pulled models. Returns empty list on failure."""
    try:
        import urllib.request
        resp = urllib.request.urlopen(f"{base_url}/api/tags", timeout=3)
        data = json.loads(resp.read())
        return sorted(m["name"] for m in data.get("models", []))
    except Exception:
        return []


def create_llm(
    provider: str,
    model: str,
    temperature: float,
    api_key: str | None = None,
    base_url: str | None = None,
):
    """Create a LangChain chat model for the given provider.

    All returned objects share the same .invoke() / .astream() interface,
    so the rest of the agent code doesn't need to know which provider is
    active.
    """
    if provider == "ollama":
        return ChatOllama(
            model=model,
            base_url=base_url or "http://localhost:11434",
            temperature=temperature,
        )
    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(model=model, api_key=api_key, temperature=temperature)
    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(model=model, api_key=api_key, temperature=temperature)
    elif provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        return ChatGoogleGenerativeAI(
            model=model, google_api_key=api_key, temperature=temperature,
        )
    elif provider == "deepseek":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=model,
            api_key=api_key,
            base_url=base_url or "https://api.deepseek.com/v1",
            temperature=temperature,
        )
    else:
        raise ValueError(f"Unknown provider: {provider!r}")


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

# Categorical columns whose distinct values should be queried at startup and
# injected into the system prompt so the LLM uses exact values instead of
# ambiguous ILIKE wildcards (e.g. "Covered Recipient Physician" not "%Physician%").
CATEGORICAL_COLUMNS: dict[str, list[str]] = {
    "general_payments": [
        "Covered_Recipient_Type",
        "Nature_of_Payment_or_Transfer_of_Value",
        "Form_of_Payment_or_Transfer_of_Value",
        "Physician_Ownership_Indicator",
    ],
    "research_payments": [
        "Expenditure_Category1",
    ],
    "ownership_payments": [
        "Interest_Held_by_Physician_or_an_Immediate_Family_Member",
    ],
    "removed_deleted": [
        "Change_Type",
        "Payment_Type",
    ],
}


# --- Prompt templates ------------------------------------------------------

SYSTEM_PROMPT_TEMPLATE = """\
You are a DuckDB SQL analyst for the CMS Open Payments dataset (calendar years 2018-2024).
Your job is to translate the user's question into ONE DuckDB SQL query.

Dataset overview:
  CMS Open Payments tracks financial relationships between the healthcare industry
  (pharmaceutical/medical device companies) and healthcare providers (physicians,
  teaching hospitals). There are three payment categories:
  - **general_payments**: Direct payments to physicians and teaching hospitals
    (consulting fees, food & beverage, travel, education, royalties, speaking fees, etc.).
    This is the largest table (~12M+ rows/year). Key field: Nature_of_Payment_or_Transfer_of_Value
    with values like 'Food and Beverage', 'Consulting Fee', 'Travel and Lodging',
    'Education', 'Compensation for services other than consulting', 'Royalty or License',
    'Current or prospective ownership or investment interest', 'Honoraria', 'Gift',
    'Entertainment', 'Charitable Contribution', 'Grant'.
  - **research_payments**: Payments for clinical research funded by industry.
    Includes principal investigator details and research study info.
  - **ownership_payments**: Physician ownership/investment interests in companies.
    Uses Total_Amount_Invested_USDollars (NOT Total_Amount_of_Payment_USDollars).

Schema (key columns only; more columns exist but rarely matter):
{compact_schema}

Per-year tables (replace YYYY with 2018, 2019, 2020, 2021, 2022, 2023, or 2024):
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
9. State columns (Recipient_State, Principal_Investigator_1_State, Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_State) store **2-character USPS state codes** ('CA', 'TX', 'NY', 'FL', 'NJ', 'PA', 'IL', 'OH', 'MA', 'GA', 'WA', 'CO', 'MI', 'MN', 'NC', 'VA', 'AZ', 'MD', 'MO', 'TN', 'IN', 'WI', 'OR', 'CT', 'SC', 'KY', 'OK', 'LA', 'AL', 'IA', 'UT', 'NV', 'AR', 'MS', 'KS', 'NM', 'NE', 'ID', 'HI', 'ME', 'NH', 'MT', 'RI', 'DE', 'SD', 'ND', 'AK', 'VT', 'WY', 'WV', 'DC', 'PR'), NEVER full state names. When the user mentions a state by name (e.g. "California", "New York"), you MUST convert it to its 2-letter USPS code in the SQL filter. Example: `Recipient_State ILIKE 'CA'` — NOT `Recipient_State ILIKE 'California'`. Use ILIKE on state columns too so the casing is irrelevant.
10. Specialty columns (Covered_Recipient_Specialty_1, Physician_Specialty, Principal_Investigator_1_Specialty_1) are stored in the format `Provider Taxonomy|Specialty|Subspecialty`, e.g. `'Allopathic & Osteopathic Physicians|Orthopaedic Surgery'` or `'Allopathic & Osteopathic Physicians|Orthopaedic Surgery|Sports Medicine'`. When the user mentions a specialty by its short name (e.g. "Orthopaedic Surgery", "Internal Medicine", "Cardiology"), you MUST use a partial-match ILIKE with wildcards so the taxonomy prefix and any subspecialty suffix are tolerated. Example: `Covered_Recipient_Specialty_1 ILIKE '%Orthopaedic Surgery%'` — NOT `ILIKE 'Orthopaedic Surgery'`. Only use an exact ILIKE on a specialty column if the user literally supplied the full `Taxonomy|Specialty` string.
11. Manufacturer / GPO / company filters MUST use wildcard ILIKE, not exact ILIKE. Company names are stored with legal suffixes like `'Arthrex, Inc.'`, `'Zimmer Biomet Holdings, Inc.'`, `'Pfizer Inc.'`, `'Johnson & Johnson Services, Inc.'`. When the user mentions a company by its short name (e.g. "Arthrex", "Pfizer", "Zimmer"), you MUST use `Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name ILIKE '%Arthrex%'` — NEVER `ILIKE 'Arthrex'`. The same rule applies to Submitting_Applicable_Manufacturer_or_Applicable_GPO_Name if you ever reference it.
12. The product column `Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1` is **dirty**. When the user asks for "top products", "highest-paying drugs", "most paid-for devices", or similar (i.e. the question is ABOUT which products/drugs/devices received payments), you MUST copy this exact pattern — do not omit any guard:

    ```sql
    SELECT
        MAX(Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1) AS product,
        SUM(Total_Amount_of_Payment_USDollars) AS total_payments
    FROM <table>
    WHERE <user filters>
      -- Guard A: ~60% of rows have NULL product (meals, travel, consulting — not tied to a product)
      AND Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1 IS NOT NULL
      -- Guard B: some rows have the manufacturer name leaked into the product column
      -- (e.g. product='Arthrex' when manufacturer='Arthrex, Inc.'). Exclude by containment,
      -- because equality fails when the manufacturer has a legal suffix ("Inc.", "Corp.", etc).
      AND LENGTH(Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1) >= 3
      AND UPPER(Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name)
          NOT LIKE '%' || UPPER(Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1) || '%'
    -- Guard C: collapse casing duplicates (e.g. 'Arthrex' vs 'ARTHREX', 'Attune' vs 'ATTUNE').
    -- ALWAYS GROUP BY UPPER(product), never by the raw column, or duplicates will split rows.
    GROUP BY UPPER(Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1)
    ORDER BY total_payments DESC
    LIMIT 100;
    ```

    All three guards (A, B, C) are required together — omitting any one produces misleading results. This rule applies ONLY when the user is asking ABOUT products/drugs/devices. If the user is filtering BY a known product name (e.g. "how much was spent on Humira?") or asking about something unrelated (payment types, specialties, manufacturers), ignore this rule.
13. When combining rows from different payment tables with UNION ALL, the monetary column names differ: general/research use `Total_Amount_of_Payment_USDollars`, ownership uses `Total_Amount_Invested_USDollars`. You MUST alias them to a common name so the UNION is valid. Example:
    ```sql
    SELECT Total_Amount_of_Payment_USDollars AS amount FROM general_payments_2024
    UNION ALL
    SELECT Total_Amount_of_Payment_USDollars AS amount FROM research_payments_2024
    UNION ALL
    SELECT Total_Amount_Invested_USDollars AS amount FROM ownership_payments_2024
    ```
    Then the outer query can safely reference `amount`. Never reference the original differing column names after a UNION ALL.
14. If the question is unrelated to CMS Open Payments AND the chat history shows no prior on-topic exchange, respond with exactly: SELECT 'unsupported' AS note;
   However, if the chat history shows the user is refining, retrying, or follow-up-asking about a prior on-topic question (e.g. "try case-insensitive", "what about 2023?", "show me the chart"), treat the new question as on-topic and answer it.
15. Categorical columns with a `VALUES:` list in the schema above store EXACT strings. When filtering on these columns, use the EXACT value from the VALUES list — do NOT use partial ILIKE matches. For example, to find physician payments use `Covered_Recipient_Type = 'Covered Recipient Physician'` — NOT `ILIKE '%Physician%'` (which would also match "Covered Recipient Non-Physician Practitioner"). When the user says "physician" they mean "Covered Recipient Physician"; when they say "non-physician" they mean "Covered Recipient Non-Physician Practitioner"; when they say "teaching hospital" they mean "Covered Recipient Teaching Hospital". Use IN (...) only when the user explicitly wants multiple categories combined.
"""

# Few-shot examples injected as user/assistant pairs before the real question.
# These teach the model SQL *patterns* that rules alone struggle to convey.
FEW_SHOT_EXAMPLES: list[tuple[str, str]] = [
    # 1. Cross-table UNION ALL with proper column aliasing (the #1 failure mode)
    (
        "What is the total dollar value for each payment type in 2024?",
        """\
SELECT 'General Payments' AS Payment_Type, SUM(Total_Amount_of_Payment_USDollars) AS Total_Value FROM general_payments_2024
UNION ALL
SELECT 'Research Payments', SUM(Total_Amount_of_Payment_USDollars) FROM research_payments_2024
UNION ALL
SELECT 'Ownership Interest', SUM(Total_Amount_Invested_USDollars) FROM ownership_payments_2024
UNION ALL
SELECT 'Grand Total', SUM(amount) FROM (
    SELECT Total_Amount_of_Payment_USDollars AS amount FROM general_payments_2024
    UNION ALL
    SELECT Total_Amount_of_Payment_USDollars AS amount FROM research_payments_2024
    UNION ALL
    SELECT Total_Amount_Invested_USDollars AS amount FROM ownership_payments_2024
) AS combined;""",
    ),
    # 2. Single-table aggregation with manufacturer wildcard ILIKE
    (
        "Top 5 companies by total general payments in 2023",
        """\
SELECT Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name AS Company,
       SUM(Total_Amount_of_Payment_USDollars) AS Total_Payments
FROM general_payments_2023
GROUP BY Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name
ORDER BY Total_Payments DESC
LIMIT 5;""",
    ),
    # 3. Cross-year query using the all_* view
    (
        "Compare total general payments in 2021 vs 2024",
        """\
SELECT Program_Year, SUM(Total_Amount_of_Payment_USDollars) AS Total_Payments
FROM all_general_payments
WHERE Program_Year IN (2021, 2024)
GROUP BY Program_Year
ORDER BY Program_Year
LIMIT 100;""",
    ),
    # 4. Categorical column exact-match (NOT ILIKE wildcard)
    (
        "How much did physicians receive in general payments in 2024?",
        """\
SELECT SUM(Total_Amount_of_Payment_USDollars) AS Total_Payments
FROM general_payments_2024
WHERE Covered_Recipient_Type = 'Covered Recipient Physician'
LIMIT 100;""",
    ),
]

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

Accuracy rules:
- Product/drug/device names and manufacturer/company names are DIFFERENT columns
  and often different things. Never describe a company name (e.g. "Arthrex",
  "Pfizer", "Zimmer") as if it were a product. If a result row's product label
  looks like a company, treat it as ambiguous data and either skip it or call it
  out as "unspecified product (company name in product field)" — do NOT pluralize
  it into a fake product category like "Arthrex products".
- Only describe what the rows literally contain. Do not invent categories that
  aren't in the data.

Formatting rules for numbers in your answer:
- Format dollar amounts as $1.2M, $834K, or $1,234,567 — never as raw
  decimals like 1234567.0 or 2345678.89.
- Format percentages as 42.3%, not 0.423.
- Thousands separators (commas) on all large integers.
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
- Questions must be about the same CMS Open Payments dataset (2018-2024).
- Prefer drill-downs (by year, state, specialty, product) over brand-new topics.
"""

EMPTY_RESULT_MESSAGE = (
    "No matching records were found for that query. A few things to try:\n"
    "- Double-check the spelling of any names, companies, or drugs.\n"
    "- Try a partial match (e.g. just the last name) — I search case-insensitively.\n"
    "- Broaden the year range or remove other filters.\n"
    "- Confirm the entity is in the CMS Open Payments dataset (2018-2024)."
)

UNSUPPORTED_MESSAGE = (
    "I can only answer questions about the CMS Open Payments dataset "
    "(pharmaceutical and medical device payments to physicians, 2018-2024). "
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

    # Map table_type → data dictionary filename prefix (CMS typo included).
    _DICT_PREFIX = {
        "general_payments": "General_Paymemnts",
        "research_payments": "Research_Paymemnts",
        "ownership_payments": "Ownership_Paymemnts",
    }

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        dictionaries_dir: str | Path | None = None,
    ) -> None:
        self._types: dict[tuple[str, str], str] = {}
        for table_type, view_name in self._REP_VIEW.items():
            rows = con.execute(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'main' AND table_name = ?",
                [view_name],
            ).fetchall()
            for column_name, data_type in rows:
                self._types[(table_type, column_name)] = data_type or "?"

        # Load column descriptions from data dictionary JSONs.
        self._descriptions: dict[tuple[str, str], str] = {}
        if dictionaries_dir:
            self._load_descriptions(Path(dictionaries_dir))

        # Load distinct values for categorical columns.
        self._distinct_values: dict[tuple[str, str], list[str]] = {}
        self._load_distinct_values(con)

    def _load_descriptions(self, base_dir: Path) -> None:
        """Load column descriptions from the most recent year's data dictionary."""
        # Use 2024 (most recent) — descriptions are stable across years.
        year_dir = base_dir / "2024"
        if not year_dir.exists():
            return
        for table_type, prefix in self._DICT_PREFIX.items():
            dict_file = year_dir / f"{prefix}_DataDictionary_2024.json"
            if not dict_file.exists():
                continue
            try:
                data = json.loads(dict_file.read_text(encoding="utf-8"))
                for field in data.get("data", {}).get("fields", []):
                    col_name = field.get("name", "")
                    desc = field.get("description", "")
                    if col_name and desc:
                        # Truncate long descriptions to keep prompt concise.
                        short = desc[:120].rstrip()
                        if len(desc) > 120:
                            short += "..."
                        self._descriptions[(table_type, col_name)] = short
            except Exception:
                continue

    def _load_distinct_values(
        self, con: duckdb.DuckDBPyConnection
    ) -> None:
        """Query DuckDB for distinct values of low-cardinality categorical columns."""
        for table_type, columns in CATEGORICAL_COLUMNS.items():
            view = self._REP_VIEW.get(table_type)
            if not view:
                continue
            for col in columns:
                try:
                    rows = con.execute(
                        f'SELECT DISTINCT "{col}" FROM {view} '
                        f'WHERE "{col}" IS NOT NULL '
                        f'ORDER BY "{col}" LIMIT 50'
                    ).fetchall()
                    values = [str(r[0]) for r in rows]
                    if values:
                        self._distinct_values[(table_type, col)] = values
                except Exception:
                    continue

    def compact_schema(self) -> str:
        lines: list[str] = []
        for table_type, cols in KEY_COLUMNS.items():
            lines.append(f"  {table_type}:")
            for col in cols:
                dtype = self._types.get((table_type, col), "?")
                desc = self._descriptions.get((table_type, col))
                if desc:
                    lines.append(f"    - {col} [{dtype}] — {desc}")
                else:
                    lines.append(f"    - {col} [{dtype}]")
                # Append distinct values for categorical columns.
                values = self._distinct_values.get((table_type, col))
                if values:
                    quoted = ", ".join(f'"{v}"' for v in values)
                    lines.append(f"      VALUES: {quoted}")
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

        dict_dir = cfg["data"].get("dictionaries_dir")
        self.schema = SchemaManager(self.con, dictionaries_dir=dict_dir)
        self.system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            compact_schema=self.schema.compact_schema()
        )

        provider = cfg["model"].get("provider", "ollama")
        model_name = cfg["model"]["name"]
        base_url = cfg["model"].get("base_url")
        sql_temp = float(cfg["model"]["temperature"])
        sum_temp = float(cfg["model"]["summarization_temperature"])

        self.llm_sql = create_llm(provider, model_name, sql_temp, base_url=base_url)
        self.llm_summary = create_llm(provider, model_name, sum_temp, base_url=base_url)

        # Session-scoped user corrections injected into _build_messages().
        self._corrections: list[str] = []

    # --- LLM management -----------------------------------------------------

    def swap_llm(
        self,
        provider: str,
        model: str,
        api_key: str | None = None,
        temperature: float | None = None,
        summarization_temperature: float | None = None,
    ) -> None:
        """Hot-swap the LLM clients without restarting DuckDB."""
        sql_temp = temperature if temperature is not None else float(
            self.cfg["model"]["temperature"]
        )
        sum_temp = summarization_temperature if summarization_temperature is not None else float(
            self.cfg["model"]["summarization_temperature"]
        )
        base_url = PROVIDER_DEFAULTS.get(provider, {}).get("base_url")
        self.llm_sql = create_llm(provider, model, sql_temp, api_key=api_key, base_url=base_url)
        self.llm_summary = create_llm(provider, model, sum_temp, api_key=api_key, base_url=base_url)

    def set_corrections(self, corrections: list[str]) -> None:
        """Replace the session-scoped user corrections list."""
        self._corrections = list(corrections)

    def add_correction(self, correction: str) -> None:
        """Append a single user correction for this session."""
        self._corrections.append(correction)

    # --- LLM call construction -------------------------------------------

    def _build_messages(
        self,
        question: str,
        error_context: str | None,
        chat_history: list[tuple[str, str]],
    ) -> list:
        msgs: list = [SystemMessage(content=self.system_prompt)]

        # Inject session-scoped user corrections as an additional system message.
        if self._corrections:
            corrections_text = "User corrections for this session (follow these strictly):\n"
            corrections_text += "\n".join(f"- {c}" for c in self._corrections)
            msgs.append(SystemMessage(content=corrections_text))

        # Few-shot examples teach SQL patterns more effectively than rules.
        for example_q, example_sql in FEW_SHOT_EXAMPLES:
            msgs.append(HumanMessage(content=example_q))
            msgs.append(AIMessage(content=example_sql))

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

    async def stream_rag_answer(self, prompt: str):
        """Async generator yielding RAG answer chunks from the summary LLM."""
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
        # Per-attempt history of failed tries: [(sql, error_message), ...].
        # The final successful attempt is NOT appended — only the failures
        # that preceded it, so the UI can show "first try failed, here's why,
        # retry succeeded." Kept short (first line of error) to stay readable.
        attempt_history: list[tuple[str | None, str]] = []
        total_attempts = self.max_retries + 1

        for attempt in range(1, total_attempts + 1):
            try:
                sql = self._generate_sql(question, error_context, chat_history)
            except Exception as e:
                return self._error_result(
                    question, None, f"LLM call failed: {e}",
                    attempt, start, attempt_history,
                )

            last_sql = sql

            if not sql:
                error_context = "You returned an empty response. Return valid DuckDB SQL."
                last_err = "empty LLM response"
                attempt_history.append((None, last_err))
                continue

            if sql.strip().lower().startswith("select 'unsupported'"):
                return {
                    "question": question,
                    "sql": sql,
                    "data": pd.DataFrame(),
                    "canned_answer": UNSUPPORTED_MESSAGE,
                    "error": None,
                    "attempts": attempt,
                    "attempt_history": attempt_history,
                    "elapsed_sec": time.perf_counter() - start,
                }

            try:
                df = self.con.execute(sql).fetchdf()
            except Exception as e:
                last_err = str(e)
                attempt_history.append((sql, last_err))
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
                "attempt_history": attempt_history,
                "elapsed_sec": time.perf_counter() - start,
            }

        return self._error_result(
            question, last_sql, last_err or "unknown error",
            total_attempts, start, attempt_history,
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
        attempt_history: list[tuple[str | None, str]] | None = None,
    ) -> dict[str, Any]:
        return {
            "question": question,
            "sql": sql,
            "data": None,
            "answer": None,
            "error": err,
            "attempts": attempts,
            "attempt_history": attempt_history or [],
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
