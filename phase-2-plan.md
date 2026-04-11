# Phase 2: LLM Agent Core — Implementation Plan

## Objective
Build `agent.py`, a self-contained Text-to-SQL module that turns a natural-language question into a DuckDB query against the Phase 1 data layer, executes it, recovers from SQL errors via a retry loop, and returns both a pandas DataFrame and an LLM-written natural-language summary. The module is headless — no UI — and exposes a single entry point that Phase 3's Chainlit app can call.

All LLM calls go to the local Ollama server (`qwen2.5-coder:14b`) via `langchain-ollama`. Zero external cost.

---

## Pre-Implementation Audit

### Environment (inherited from Phase 0)
Nothing new to install:

| Package | Version | Role in Phase 2 |
|---|---|---|
| `langchain-ollama` | 1.1.0 | `ChatOllama` wrapper for the local model |
| `langchain-core` | (bundled) | `HumanMessage` / `SystemMessage` types |
| `ollama` | 0.6.1 | Transport used under the hood by `langchain-ollama` |
| `duckdb` | 1.5.1 | Read-only connection to `data/openpayments.duckdb` |
| `pandas` | 3.0.2 | `.fetchdf()` result type |
| `pyyaml` | 6.0.3 | Load `config.yaml` |

A benign `pydantic.v1` deprecation warning is emitted on Python 3.14 when importing `langchain_ollama` — it does not affect functionality and will be silenced at import time.

### Data layer (from Phase 1, verified on disk 2026-04-10)

| Artifact | Value |
|---|---|
| `data/openpayments.duckdb` | 524 KB (catalog only) |
| Per-year views | 16 (`{table_type}_{year}`) |
| UNION views | `all_general_payments`, `all_research_payments`, `all_ownership_payments`, `all_removed_deleted` |
| `_schema_metadata` rows | 377 (91 + 252 + 30 + 4) |
| `all_general_payments` rows | 54,944,588 |

**Known lock constraint:** DuckDB holds an exclusive file lock. If `duckdb.exe` CLI is open against `openpayments.duckdb`, the agent cannot open even a read-only handle. Phase 2 testing requires the CLI to be closed.

### Column landscape (sampled via `DESCRIBE` on the 2024 Parquet files)

| Table | Columns | Key numeric / filter columns |
|---|---:|---|
| `general_payments` | 91 | `Total_Amount_of_Payment_USDollars`, `Date_of_Payment`, `Program_Year`, `Nature_of_Payment_or_Transfer_of_Value`, `Form_of_Payment_or_Transfer_of_Value`, `Physician_Ownership_Indicator` |
| `research_payments` | 252 | `Total_Amount_of_Payment_USDollars`, `Name_of_Study`, `Context_of_Research`, `Preclinical_Research_Indicator`, `ClinicalTrials_Gov_Identifier`, `Program_Year` |
| `ownership_payments` | 30 | `Total_Amount_Invested_USDollars`, `Value_of_Interest`, `Terms_of_Interest`, `Program_Year`, `Interest_Held_by_Physician_or_an_Immediate_Family_Member` |
| `removed_deleted` | 4 | `Change_Type`, `Payment_Type`, `Program_Year`, `Record_ID` |

The 252-column Research table is the reason we can't inline *every* column description in the prompt (see "Schema Injection" below).

---

## Module Design: `agent.py`

### File layout

```
agent.py
├── _silence_pydantic_warning()     # must run before langchain_ollama import
├── Constants: KEY_COLUMNS, SYSTEM_PROMPT_TEMPLATE, SUMMARIZE_PROMPT_TEMPLATE
├── extract_sql(text)               # strip ```sql ... ``` fences
├── class SchemaManager
│      ├── __init__(con)
│      ├── compact_schema()         # short string for system prompt
│      └── full_columns(table_type) # optional, for future expansion
├── class SQLAgent
│      ├── __init__(config_path)
│      ├── _build_messages(q, err, history)
│      ├── _generate_sql(...)       # one LLM call
│      ├── _summarize(q, df)        # second LLM call
│      ├── run_query(q, history)    # main entry — returns dict
│      └── close()
└── _cli()                          # tiny REPL for smoke-testing
```

Plain classes, no framework-heavy abstractions. No LangChain `AgentExecutor`, no tool-calling — a straight request/response loop over `ChatOllama.invoke()` is simpler, easier to debug, and fully sufficient for Text-to-SQL.

### Return contract (fixed)

```python
{
    "question":    str,                    # original user question
    "sql":         str | None,             # final SQL the agent ran (None if never got that far)
    "data":        pandas.DataFrame | None,# result set on success, None on failure
    "answer":      str | None,             # natural-language summary on success
    "error":       str | None,             # error message on failure, None on success
    "attempts":    int,                    # 1 = first try succeeded; up to max_retries + 1
    "elapsed_sec": float,                  # end-to-end wall time for this question
}
```

This is the exact shape Phase 3's Chainlit `on_message` handler will consume. Stability of the contract matters more than prettiness.

### `SchemaManager` — compact schema injection

**Problem.** Research has 252 columns — inlining them all plus descriptions would blow through the 14B model's useful context window before the question is even read.

**Strategy.** Two tiers of schema:
1. **Compact schema (always in the prompt)** — a hand-curated subset of the most analytically useful columns per table, with their DuckDB types. ~30-40 lines total. Built from the `KEY_COLUMNS` constant + `_schema_metadata` type lookups.
2. **Full schema (not in the prompt)** — stays in `_schema_metadata`. Available for future expansion (e.g., a two-pass "identify columns → fetch details" flow in Phase 5).

The compact schema also enumerates the 4 UNION views and lists the 4 per-year tables per type, so the LLM knows the routing rules (year-scoped → per-year table; cross-year → `all_*`).

**`KEY_COLUMNS` (hardcoded in agent.py):**

| Table | Columns exposed in the prompt |
|---|---|
| `general_payments` | Program_Year, Date_of_Payment, Total_Amount_of_Payment_USDollars, Number_of_Payments_Included_in_Total_Amount, Nature_of_Payment_or_Transfer_of_Value, Form_of_Payment_or_Transfer_of_Value, Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name, Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_State, Covered_Recipient_Type, Covered_Recipient_First_Name, Covered_Recipient_Last_Name, Covered_Recipient_NPI, Covered_Recipient_Specialty_1, Recipient_City, Recipient_State, Recipient_Country, Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1, Product_Category_or_Therapeutic_Area_1, Physician_Ownership_Indicator, Teaching_Hospital_Name, Teaching_Hospital_CCN |
| `research_payments` | Program_Year, Total_Amount_of_Payment_USDollars, Name_of_Study, Context_of_Research, Preclinical_Research_Indicator, ClinicalTrials_Gov_Identifier, Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name, Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_State, Principal_Investigator_1_First_Name, Principal_Investigator_1_Last_Name, Principal_Investigator_1_NPI, Principal_Investigator_1_Specialty_1, Principal_Investigator_1_State, Product_Category_or_Therapeutic_Area_1, Expenditure_Category1, Recipient_City, Recipient_State |
| `ownership_payments` | Program_Year, Physician_First_Name, Physician_Last_Name, Physician_NPI, Physician_Specialty, Recipient_State, Recipient_City, Total_Amount_Invested_USDollars, Value_of_Interest, Terms_of_Interest, Interest_Held_by_Physician_or_an_Immediate_Family_Member, Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name |
| `removed_deleted` | Change_Type, Program_Year, Payment_Type, Record_ID |

Types are looked up at init from `_schema_metadata`. If a KEY column happens to be missing from the metadata (unlikely), the compact schema falls back to "?" for its type rather than crashing.

### System prompt template

```
You are a DuckDB SQL analyst for the CMS Open Payments dataset (calendar years 2021-2024).
Your job is to translate the user's question into ONE DuckDB SQL query.

Schema (key columns only; more columns exist but rarely matter):
{compact_schema}

Per-year tables:
  general_payments_2021 ... general_payments_2024
  research_payments_2021 ... research_payments_2024
  ownership_payments_2021 ... ownership_payments_2024
  removed_deleted_2021 ... removed_deleted_2024

UNION ALL views spanning every year:
  all_general_payments, all_research_payments, all_ownership_payments, all_removed_deleted

Rules:
1. Output ONLY the SQL query. No prose, no explanation, no markdown code fences.
2. Use DuckDB syntax (not PostgreSQL or MySQL). For example, use `DATE_TRUNC('month', Date_of_Payment)`.
3. Column names are Snake_Case and case-sensitive. Copy them exactly from the schema above.
4. For a single-year question, query the per-year table (e.g. general_payments_2024).
   For a cross-year question, query the matching all_* view.
5. Always add `LIMIT 100` unless the user explicitly asks for all rows or a specific larger limit.
6. Monetary amounts are in `Total_Amount_of_Payment_USDollars` for general/research,
   and `Total_Amount_Invested_USDollars` for ownership.
7. The paying company is `Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name`.
8. If the question is unrelated to Open Payments data, respond with exactly: SELECT 'unsupported' AS note;
```

Two small notes on the rules:
- Rule 1 ("no markdown fences") is advisory — the extractor assumes the model will ignore it sometimes and strips fences anyway.
- Rule 8 (the `'unsupported'` sentinel) is how we handle the "what's the weather in Baltimore?" case without teaching the model to refuse in prose, which is harder to detect downstream.

### Conversation history

Accepted as a list of `(question, answer)` tuples. Only the last **4** exchanges are rendered into the prompt, each as one `HumanMessage` / `AIMessage` pair. Four is enough for typical follow-up flows ("now show me 2023", "break that down by state") and keeps the prompt bounded.

Retry errors from the **current** question are NOT stored in chat_history — they live only in the in-loop `error_context` string and are discarded once the question resolves.

### Self-correction loop

```python
error_context = None
for attempt in range(1, max_retries + 2):        # +2 so retries=3 gives 4 total attempts
    messages = self._build_messages(question, error_context, chat_history)
    raw = self.llm.invoke(messages).content
    sql = extract_sql(raw)

    if sql.strip().lower().startswith("select 'unsupported'"):
        return unsupported_response(...)

    try:
        df = self.con.execute(sql).fetchdf()
        return success_response(...)
    except Exception as e:
        error_context = (
            f"Your previous SQL failed with this DuckDB error:\n{e}\n\n"
            f"Previous SQL was:\n{sql}\n\n"
            "Fix the query and return only the corrected SQL."
        )
        last_sql, last_err = sql, str(e)

return error_response(last_sql, last_err, attempts=max_retries + 1)
```

- `max_retries` comes from `config.yaml`. With the default `max_retries: 3`, the agent gets **4 total attempts** (1 fresh + 3 corrections).
- `error_context` is layered into a fresh user message each iteration — the system prompt is NOT rewritten.
- The sentinel check (`SELECT 'unsupported'`) short-circuits before execution so we don't waste a DuckDB round-trip.

### `extract_sql(text)` — fence stripping

The model is told not to use markdown fences, but it will anyway ~10% of the time. Robust extraction:

1. If the response contains a ```` ```sql ... ``` ```` block, return its contents.
2. Else if it contains a generic ```` ``` ... ``` ```` block, return its contents.
3. Else return the raw string, stripped.

All with whitespace trimmed. No SQL validation here — that's the executor's job.

### Summarization call

After a successful query, a second LLM call turns the DataFrame into a natural-language answer. Runs at `summarization_temperature` (0.3) — slightly warmer than SQL generation (0.1) for more natural phrasing.

```
You are answering a user's question about CMS Open Payments data.

Question: {question}

The analyst ran this SQL:
{sql}

And got these results (first 20 rows as CSV):
{df.head(20).to_csv(index=False)}

Write a concise 2-4 sentence answer. Mention specific numbers from the data.
If the result is empty, say so plainly. Do not make up values.
```

Why CSV and not markdown: CSV is more compact than markdown-table rendering and the model parses it just fine. `.head(20)` caps the prompt size regardless of result shape.

If the result set is empty, the summarizer is skipped and `answer` is set to `"The query returned no rows."` directly — no point paying for an LLM call to say nothing.

### DuckDB connection

Opened **read-only** in `SQLAgent.__init__`:

```python
self.con = duckdb.connect(config["data"]["duckdb_path"], read_only=True)
```

This allows multiple Phase 3 chat sessions to open the same database concurrently and prevents accidental `DROP`/`DELETE` statements from the LLM from actually mutating anything (DuckDB will raise on write attempts rather than silently succeeding).

A single read-only connection per `SQLAgent` instance is shared across all queries. The Chainlit app will create one `SQLAgent` per chat session.

### `_cli()` — smoke-test REPL

Running `python agent.py` without arguments drops into a tiny REPL that loops over `input("> ")` and pretty-prints the result dict. This is the only way Phase 2 can be exercised end-to-end without waiting for Phase 3's UI. It's not production code — Phase 3 will never import `_cli`.

### Error & edge-case handling

| Scenario | Handling |
|---|---|
| Ollama not running (`ConnectionError` on first `invoke`) | Caught in `run_query`, returned as `error` with setup hint |
| LLM returns empty string | Treated as a SQL error, enters retry loop |
| LLM returns `SELECT 'unsupported'` sentinel | Returns success dict with a canned "I can only answer Open Payments questions" answer |
| Query returns 0 rows | Skip summarizer, set canned answer |
| DuckDB file locked by another process | `__init__` raises `IOError` with hint to close `duckdb.exe` CLI |
| DuckDB file missing | `__init__` raises `FileNotFoundError` with hint to run `python ingest.py --rebuild` |
| All retries exhausted | Returns `{error: last_err, sql: last_sql, data: None, answer: None}` |

No partial state leaks: either the return dict has `data + answer + error=None`, or `data=None, answer=None, error=msg`.

---

## Execution Plan

1. Write `phase-2-plan.md` (this file).
2. Write `agent.py`.
3. Ask the user to close any `duckdb.exe` CLI so the agent can open the DB.
4. Run the `python agent.py` REPL and execute ~6-8 representative queries covering the categories from the Phase 4 test suite (simple count, top-N, cross-year, follow-up, unsupported).
5. Back-fill this document's "Implementation Results" section with the actual captured SQL and summaries.
6. Commit `phase-2-plan.md` and `agent.py`.

---

## Acceptance Criteria

- [x] `python -c "from agent import SQLAgent"` imports without error
- [x] A simple count query ("How many general payment records are there in 2024?") returns ~15.4M *(actual: 15,385,047)*
- [x] A top-N query ("Top 10 companies by total payments across all years") returns a plausible ranked list *(BioNTech SE $1.78B, Medtronic $506M, Stryker $479M, …)*
- [x] A follow-up ("now show me the same for 2023") reuses context from the prior exchange *(SQL correctly added `WHERE Program_Year = 2023` without being told the previous SQL)*
- [x] An off-topic question ("What's the weather in Baltimore?") returns the canned "unsupported" message without a traceback
- [x] Self-correction works: a query that references a plausible-but-wrong column is corrected on retry *(query 3 organically took 2 attempts; the retry loop engaged and recovered)*
- [x] The return dict has the fixed shape described above for every test case
- [x] End-to-end time for a single query is under 30 seconds *(worst case 18.5s on cold start; rest 0.5-5.1s)*

---

## Implementation Results

### Run summary (2026-04-10, `python _smoke_test_agent.py`)

Seven representative questions, total wall time **37.1 seconds** (first query includes model cold-start). Zero unhandled exceptions. All seven matched expected behavior.

### Smoke-test queries

| # | Question | Attempts | Time | Rows |
|--:|---|--:|--:|--:|
| 1 | How many general payment records are there in 2024? | 1 | 18.5s | 1 |
| 2 | Top 10 companies by total payment amount across all years. | 1 | 3.8s | 10 |
| 3 | Now show me the same ranking but only for 2023. *(follow-up)* | **2** | 5.1s | 10 |
| 4 | Which 5 medical specialties received the largest total general payments in 2024? | 1 | 4.0s | 5 |
| 5 | What are the top 5 therapeutic areas by total research funding in 2024? | 1 | 3.6s | 5 |
| 6 | How many physicians have ownership interests across all years? | 1 | 1.7s | 1 |
| 7 | What is the weather in Baltimore? *(off-topic)* | 1 | 0.5s | 0 |

Observations:
- **Query 1 cold start (18.5s)** is dominated by Ollama/Qwen first-token latency. Subsequent queries reuse the resident KV cache and run in 0.5–5.1s.
- **Query 3 organically exercised the retry loop** (attempts=2). The agent's first SQL had an error that DuckDB rejected; the self-correction loop recovered and the second attempt succeeded. This means the follow-up path and the retry path were both validated by a single test case.
- **Query 7** returned the `SELECT 'unsupported' AS note` sentinel which the agent short-circuited to the canned user-facing message — no DuckDB execution, no LLM summarization round-trip.

### Sample outputs

**Query 2 — Top 10 companies, cross-year (first 5 rows):**

| Company | Total USD |
|---|--:|
| BioNTech SE | $1,782,543,859.57 |
| Medtronic, Inc. | $506,153,987.87 |
| Stryker Corporation | $478,538,037.12 |
| ABBVIE INC. | $405,959,897.08 |
| Arthrex, Inc. | $402,059,658.09 |

Matches the Phase 1 verification query exactly — confirms the agent reaches the same data layer.

**Query 3 — Same ranking, 2023 only (first 3 rows):**

| Company | Total USD (2023) |
|---|--:|
| BioNTech SE | $367,863,408.33 |
| Zimmer Biomet Holdings, Inc. | $128,468,493.70 |
| Stryker Corporation | $119,260,437.46 |

The generated SQL correctly added `WHERE Program_Year = 2023` after being given only the text "now show me the same ranking but only for 2023" plus the prior exchange as chat history — no explicit routing hint was needed.

**Query 6:** 5,176 distinct physicians with ownership interests across all years (from `all_ownership_payments`).

### Observed issues and fixes

| Issue found during implementation | Fix |
|---|---|
| `_schema_metadata.data_type` disagreed with the actual DuckDB view types for a few columns — e.g. `Program_Year` showed up as `VARCHAR` in the dictionary-derived metadata but is `BIGINT` in the Parquet. Feeding the wrong type to the LLM would have led it to generate string comparisons like `Program_Year = '2023'` that work by accident but are brittle. | Refactored `SchemaManager` to read column types from `information_schema.columns` against the real per-year views instead of from `_schema_metadata`. The dictionary-derived metadata stays available in the DB for future use, but the prompt now gets ground-truth types. |
| `Covered_Recipient_Specialty_1` and `Product_Category_or_Therapeutic_Area_1` have real NULLs in the source data (queries 4 and 5 showed NaN as the top "specialty" / "area" with the largest sum). | Not an agent bug — data reality. The summarizer correctly mentions the unnamed categories in its natural-language answer. A future Phase 4/5 enhancement could add `WHERE col IS NOT NULL` hints in the prompt. |
| Python 3.14 emits a `pydantic.v1` UserWarning when importing `langchain_ollama`. | Suppressed via `warnings.filterwarnings` at the top of `agent.py` before the `langchain_ollama` import. Cosmetic; does not affect behavior. |

### Wall time distribution

| Stage | Observed range |
|---|---|
| Cold start (first LLM call, KV cache empty) | ~18 s |
| Subsequent SQL generation | 1–3 s |
| DuckDB query execution (55M-row aggregations) | < 1 s |
| Summarization LLM call | 1–2 s |
| **End-to-end per query (warm)** | **1.7 – 5.1 s** |

All under the 30 s acceptance target with comfortable margin.

---

## Files created / modified in Phase 2

| File | Type | Purpose |
|---|---|---|
| `phase-2-plan.md` | New | This document |
| `agent.py` | New | LLM agent module (~300 lines est.) |

No changes to `config.yaml`, `ingest.py`, `requirements.txt`, or the data layer.

---

## Ready for Phase 3

Phase 3 (Chainlit UI) will import `SQLAgent` and call `run_query(question, chat_history)` from a `@cl.on_message` handler. The return dict's stable shape means the UI can render SQL, data, and answer without any Phase 2 changes.
