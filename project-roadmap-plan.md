# Open Payments Data Analyst — Project Roadmap & Implementation Plan

## Guiding Principles

1. **Zero cost** — Everything runs locally. No paid APIs, no cloud services, no subscriptions. Ollama + open-source models only.
2. **Highly configurable** — Swap models, data paths, and UI settings via a single `config.yaml`. No code changes required.
3. **Minimal manual intervention** — Auto-discover CSVs, auto-build schemas from data dictionaries, self-correcting SQL agent.
4. **Simple but extensible** — Chat interface MVP first, with a clear path to dashboard mode and advanced features.

---

## Data Refresh Model

CMS publishes updated Open Payments datasets approximately **every 6 months**. When new data arrives:

- Download the new CSV zip files and extract them into `Datasets/`
- Run `python ingest.py --rebuild` (the **default** mode)
- This **wipes** all existing Parquet files and the DuckDB database, then re-ingests from scratch

This is intentionally a full wipe + rebuild, not an incremental append, because:
- CMS re-publishes the entire dataset each cycle (records can change status: NEW → CHANGED → DELETED)
- A clean rebuild guarantees no stale or orphaned records
- The full ingestion takes under 30 minutes on target hardware — acceptable for a semi-annual operation

---

## Phase 0: Environment Setup

### Goal
Install all dependencies, configure Ollama with the target model, and create the project scaffolding so every subsequent phase can be developed and tested immediately.

### What Gets Built

| File | Purpose |
|------|---------|
| `requirements.txt` | Python dependency manifest |
| `config.yaml` | Central configuration — controls model, data paths, ingestion, and UI settings |

### config.yaml Structure

```yaml
model:
  provider: ollama                    # ollama only for MVP (zero cost)
  name: qwen2.5-coder:14b            # any Ollama model tag
  base_url: http://localhost:11434    # Ollama default
  temperature: 0.1                    # low for deterministic SQL generation
  summarization_temperature: 0.3     # slightly higher for natural language summaries
  max_retries: 3                      # SQL self-correction attempts
  timeout: 120                        # seconds per LLM call

data:
  source_dir: ./Datasets
  parquet_dir: ./data/parquet
  duckdb_path: ./data/openpayments.duckdb
  dictionaries_dir: ./DataDictionaries

ingestion:
  compression: snappy                 # snappy (faster) or zstd (smaller)
  row_group_size: 500000              # rows per Parquet row group
  sample_size: 10000                  # CSV auto-detect sample size

ui:
  title: "Open Payments Data Analyst"
  max_display_rows: 1000
  show_sql: true                      # show generated SQL as a collapsible step
  show_charts: true                   # auto-generate charts for numeric results
  theme: "dark"                       # Chainlit theme: "dark" or "light"
  show_agent_steps: true              # show SQL generation/execution as expandable steps
```

### requirements.txt

```
duckdb>=1.0
pyarrow>=15.0
langchain>=0.3
langchain-community>=0.3
langchain-ollama>=0.2
chainlit>=1.1
ollama>=0.3
pyyaml>=6.0
plotly>=5.18
pandas>=2.0
```

### Setup Steps

```bash
# 1. Install Ollama from https://ollama.com (Windows installer)
# 2. Pull the recommended model (~8.5 GB download)
ollama pull qwen2.5-coder:14b

# 3. Create Python virtual environment
python -m venv .venv
.venv\Scripts\activate

# 4. Install dependencies
pip install -r requirements.txt
```

### Design Decisions
- **`chainlit`** over Streamlit — purpose-built for LLM chat applications. Provides a polished, Amazon Q Business-like chat interface out of the box with native streaming, conversation threading, and expandable agent "steps" (perfect for showing SQL generation → execution → summarization). Event-driven architecture (no full-page reruns like Streamlit). First-class LangChain integration via decorators.
- **`langchain-ollama`** is the dedicated Ollama integration (split from `langchain-community`). Avoids deprecation warnings.
- **`pandas`** is required because Chainlit's table elements and LangChain's SQL toolkit expect DataFrames.
- **`plotly`** is kept for interactive chart generation; Chainlit renders Plotly figures natively via `cl.Plotly` elements.
- **snappy** compression over zstd as default — ingestion speed matters more than an extra GB of disk savings for a semi-annual operation.
- Minimum versions pinned (not exact) to avoid dependency conflicts.

### Acceptance Criteria
- [ ] `ollama list` shows `qwen2.5-coder:14b`
- [ ] `ollama run qwen2.5-coder:14b "SELECT 1"` returns a response
- [ ] `pip install -r requirements.txt` completes without errors
- [ ] `python -c "import duckdb, langchain, chainlit, pyarrow, yaml; print('OK')"` prints OK
- [ ] `python -c "import yaml; cfg = yaml.safe_load(open('config.yaml')); print(cfg['model']['name'])"` prints the model name

### Dependencies
None — this is the foundation.

---

## Phase 1: Data Ingestion Pipeline

### Goal
Convert 33 GB of CSVs into compressed Parquet files, register them as DuckDB tables, create UNION views spanning all years, and persist schema metadata from the JSON data dictionaries. Support full wipe + rebuild as the primary mode.

### What Gets Built

| File | Purpose |
|------|---------|
| `ingest.py` | Complete data pipeline: CSV discovery → Parquet conversion → DuckDB registration → schema metadata |

### CSV Auto-Discovery

Filenames follow a strict pattern. Parse with regex:
```
OP_(DTL_GNRL|DTL_RSRCH|DTL_OWNRSHP|REMOVED_DELETED)_PGYR(\d{4})_.*\.csv
```

Map to clean table names:
| Regex Group | Table Name |
|------------|------------|
| `DTL_GNRL` | `general_payments` |
| `DTL_RSRCH` | `research_payments` |
| `DTL_OWNRSHP` | `ownership_payments` |
| `REMOVED_DELETED` | `removed_deleted` |

### CSV → Parquet Conversion Strategy

Use DuckDB itself for conversion — it streams the data without loading the entire CSV into Python memory:

```sql
COPY (
    SELECT * FROM read_csv_auto('{csv_path}',
        all_varchar=false,
        sample_size=10000,
        ignore_errors=true)
) TO '{parquet_path}' (
    FORMAT PARQUET,
    COMPRESSION 'snappy',
    ROW_GROUP_SIZE 500000
)
```

This avoids Python memory pressure. For the ~8 GB General Payments CSVs, each conversion should take approximately 2–4 minutes on the Ryzen 9.

### Data Dictionary Loading

The JSON structure: `{"data": {"fields": [...]}}` where each field has `name`, `description`, `type`, `example`, and `constraints`.

**Important:** The actual filenames contain a typo — `Paymemnts` (not `Payments`). Code must match this.

Dictionary-to-type mapping:
| Dictionary Filename Pattern | Table Type |
|---------------------------|------------|
| `General_Paymemnts_*` | `general_payments` |
| `Research_Paymemnts_*` | `research_payments` |
| `Ownership_Paymemnts_*` | `ownership_payments` |
| *(none)* | `removed_deleted` |

Since schemas are **identical across all years** within each type, load only one dictionary per type (the 2024 version) and apply it to all years.

**No dictionary exists for `removed_deleted`** — hardcode descriptions for its 4 columns: `Change_Type`, `Program_Year`, `Payment_Type`, `Record_ID`.

### Type Mapping (Data Dictionary → DuckDB)

| Dictionary Type | DuckDB Type | Notes |
|----------------|-------------|-------|
| `string` | `VARCHAR` | |
| `integer` | `BIGINT` | Not INT — NPI and Record_ID can exceed 2^31 |
| `number` | `DOUBLE` | |
| `date` | `DATE` | Parse using format from dictionary (MM/DD/YYYY) |

### Table Naming & UNION Views

**Per-year tables** (registered from Parquet files):
- `general_payments_2021`, `general_payments_2022`, `general_payments_2023`, `general_payments_2024`
- `research_payments_2021`, ..., `research_payments_2024`
- `ownership_payments_2021`, ..., `ownership_payments_2024`
- `removed_deleted_2021`, ..., `removed_deleted_2024`

**UNION ALL views** (spanning all years):
```sql
CREATE OR REPLACE VIEW all_general_payments AS
    SELECT * FROM general_payments_2021
    UNION ALL SELECT * FROM general_payments_2022
    UNION ALL SELECT * FROM general_payments_2023
    UNION ALL SELECT * FROM general_payments_2024;
```

Same pattern for `all_research_payments`, `all_ownership_payments`, `all_removed_deleted`.

The `Program_Year` column already exists in the data for year filtering. No synthetic partition column needed.

### Schema Metadata Table

Create a `_schema_metadata` table in DuckDB:

```sql
CREATE TABLE _schema_metadata (
    table_type VARCHAR,      -- 'general_payments', 'research_payments', etc.
    column_name VARCHAR,
    data_type VARCHAR,
    description VARCHAR,
    example VARCHAR,
    constraints VARCHAR      -- JSON string of constraints
);
```

Populated from data dictionaries. The agent queries this table to build schema-aware prompts.

### CLI Flags

| Flag | Behavior |
|------|----------|
| `--rebuild` (default) | Delete `data/parquet/` and `data/openpayments.duckdb`, then re-ingest everything. **Primary mode for 6-month refresh.** |
| `--skip-existing` | Skip Parquet conversion if the file already exists. **Dev convenience only.** |

### Progress Reporting

Each CSV conversion prints: filename, row count, elapsed time, and output Parquet file size. Summary at end: total rows, total Parquet size, total time.

### Acceptance Criteria
- [ ] `python ingest.py --rebuild` completes without errors
- [ ] `data/parquet/` contains 16 Parquet files (4 types x 4 years)
- [ ] DuckDB has all 16 per-year tables + 4 UNION views + `_schema_metadata`
- [ ] `SELECT COUNT(*) FROM all_general_payments` returns ~55M
- [ ] `SELECT COUNT(*) FROM _schema_metadata` returns ~373 rows (91 + 252 + 30 for 3 types with dictionaries)
- [ ] `python ingest.py --skip-existing` re-run completes in seconds
- [ ] Total Parquet size on disk: ~4–6 GB
- [ ] Total ingestion time: under 30 minutes

### Dependencies
Phase 0 (Python deps installed, `config.yaml` exists).

---

## Phase 2: LLM Agent Core

### Goal
Build the Text-to-SQL agent that translates natural language questions into DuckDB SQL, executes queries, self-corrects on errors, and summarizes results — all using the local Ollama model at zero cost.

### What Gets Built

| File | Purpose |
|------|---------|
| `agent.py` | LLM agent: schema management, SQL generation, self-correction, result summarization |

### Architecture within agent.py

Three components:

1. **`SchemaManager`** — Loads schema context from DuckDB's `_schema_metadata` table. Builds compact schema strings for LLM prompts.
2. **`SQLAgent`** — LangChain-based agent that generates, validates, executes, and retries SQL.
3. **`run_query(question, chat_history) → dict`** — Main entry point returning `{"answer": str, "sql": str, "data": DataFrame, "error": str|None}`.

### Schema Injection Strategy

**Problem:** The Research table has 252 columns. Injecting all column descriptions into every prompt would consume ~4000 tokens — too much for a 14B model's effective context.

**Solution (MVP):** Build a "compact schema" listing table names with column names and types, but only include descriptions for the ~15–20 most commonly queried columns per table. The full schema lives in `_schema_metadata` for reference.

Key columns to always include in the prompt:
- **General:** `Total_Amount_of_Payment_USDollars`, `Nature_of_Payment_or_Transfer_of_Value`, `Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name`, `Covered_Recipient_Specialty_1`, `Recipient_State`, `Recipient_City`, `Covered_Recipient_Type`, `Date_of_Payment`, `Program_Year`, `Form_of_Payment_or_Transfer_of_Value`, `Name_of_Drug_or_Biological_or_Device_or_Medical_Supply_1`
- **Research:** `Total_Amount_of_Payment_USDollars`, `Name_of_Study`, `Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name`, `Principal_Investigator_1_First_Name/Last_Name`, `Product_Category_or_Therapeutic_Area_1`, `Program_Year`, `Context_of_Research`
- **Ownership:** `Total_Amount_Invested_USDollars`, `Value_of_Interest`, `Physician_Profile_First_Name/Last_Name`, `Program_Year`

### System Prompt Template

```
You are a SQL analyst for CMS Open Payments data. You write DuckDB SQL queries.

Available tables:
{compact_schema}

UNION views spanning all years:
- all_general_payments (all years combined)
- all_research_payments (all years combined)
- all_ownership_payments (all years combined)

Per-year tables: {table_type}_YYYY (e.g., general_payments_2024)

Rules:
1. Use DuckDB SQL syntax only.
2. Always include LIMIT unless the user explicitly asks for all results. Default LIMIT 100.
3. For year-specific queries, use per-year tables. For cross-year queries, use all_* views.
4. Column names use Snake_Case and are case-sensitive. Use exact names from the schema.
5. Return ONLY the SQL query, no explanation.
6. For monetary amounts, use Total_Amount_of_Payment_USDollars.
7. For company names, use Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name.
```

### LLM Integration

```python
from langchain_ollama import ChatOllama

llm = ChatOllama(
    model=config["model"]["name"],
    base_url=config["model"]["base_url"],
    temperature=config["model"]["temperature"],
)
```

### Self-Correction Loop

```
for attempt in range(max_retries + 1):
    sql = call_llm_for_sql(question, schema_context, error_context)
    sql = extract_sql(sql)           # strip markdown fences
    try:
        result_df = duckdb_con.execute(sql).fetchdf()
        break                         # success
    except Exception as e:
        error_context = f"SQL failed: {e}. Fix the query."
        if attempt == max_retries:
            return {"error": str(e), "sql": sql, "data": None, "answer": None}
```

**SQL extraction:** Regex to strip ` ```sql ... ``` ` or ` ``` ... ``` ` fences, falling back to raw response.

### Result Summarization

Separate LLM call after successful SQL execution:
```
Given this question: "{question}"
And this data result (first 20 rows):
{result_df.head(20).to_markdown()}

Provide a concise natural language summary of the findings.
```

Uses `summarization_temperature` (0.3) from config — slightly higher than SQL generation for more natural language.

### Conversation History

Accept `chat_history` as a list of `(question, answer)` tuples. Include the last 3–5 exchanges so the model handles follow-ups like "now show me the same for 2023" or "break that down by state."

### DuckDB Connection

Open in **read-only** mode: `duckdb.connect(path, read_only=True)`. Prevents accidental writes and allows concurrent reads from the Chainlit app.

### Acceptance Criteria
- [ ] A simple query ("How many rows are in general payments 2024?") returns the correct count
- [ ] A complex query ("Top 10 companies by total payments across all years") returns reasonable results
- [ ] Self-correction works: a query that triggers a column name error on first attempt succeeds on retry
- [ ] Return dict includes `sql`, `data` (DataFrame), `answer`, and `error` (None on success)
- [ ] Follow-up questions correctly use context from prior exchanges
- [ ] All queries complete within 30 seconds (LLM generation + DuckDB execution)

### Dependencies
Phase 0 (Ollama running, deps installed), Phase 1 (DuckDB populated with data and schema metadata).

---

## Phase 3: Chat UI (Chainlit)

### Goal
Build a professional, Amazon Q Business-like chat interface using **Chainlit** — a framework purpose-built for LLM applications. Users ask questions in plain English, see the agent's reasoning steps (SQL generation → execution → summarization) in real-time, and receive answers with data tables and interactive charts.

### Why Chainlit over Streamlit

| Aspect | Streamlit | Chainlit |
|--------|-----------|----------|
| **Architecture** | Full script reruns on every interaction | Event-driven, async — never freezes |
| **Chat UX** | Chat components feel bolted-on | Purpose-built chat with threading, streaming |
| **Agent visibility** | Manual expanders for SQL display | Native `@cl.step` shows agent reasoning as collapsible steps |
| **Session management** | Manual `st.session_state` wiring | Built-in conversation history with resumable threads |
| **LangChain integration** | Manual callbacks/generators | First-class decorators (`@cl.on_message`, `@cl.step`) |
| **Visual polish** | Professional but dashboard-oriented | Modern chat UI comparable to Amazon Q / ChatGPT |
| **Streaming** | Via generators (feels hacky) | Native `msg.stream_token()` |
| **Code required** | ~200+ lines with manual state | ~150–200 lines, decorator-driven |

### What Gets Built

| File | Purpose |
|------|---------|
| `app.py` | Chainlit chat application (~150–200 lines) |
| `.chainlit/config.toml` | Chainlit-specific UI configuration (auto-generated on first run, then customized) |

### Application Structure

```python
import chainlit as cl
from agent import SQLAgent

@cl.on_chat_start
async def on_start():
    """Initialize the agent when a new chat session begins."""
    agent = SQLAgent("config.yaml")
    cl.user_session.set("agent", agent)
    cl.user_session.set("chat_history", [])

    await cl.Message(
        content="Welcome to the Open Payments Data Analyst. "
                "Ask me anything about pharmaceutical and medical device "
                "industry payments to physicians and hospitals (2021–2024)."
    ).send()

@cl.on_message
async def on_message(message: cl.Message):
    """Handle each user message."""
    agent = cl.user_session.get("agent")
    chat_history = cl.user_session.get("chat_history")

    # Step 1: Generate SQL (visible as collapsible step)
    async with cl.Step(name="Generating SQL", type="tool") as step:
        result = await cl.make_async(agent.run_query)(
            message.content, chat_history
        )
        if result["sql"]:
            step.output = f"```sql\n{result['sql']}\n```"

    # Step 2: Display results
    if result.get("error"):
        await cl.Message(content=f"Sorry, I couldn't answer that.\n\n**Error:** {result['error']}").send()
    else:
        # Build response with elements
        elements = []

        # Data table as a Text element
        if result["data"] is not None and not result["data"].empty:
            table_md = result["data"].head(config["ui"]["max_display_rows"]).to_markdown()
            elements.append(cl.Text(name="Results", content=table_md, display="side"))

            # Auto-generate chart if applicable
            chart = generate_auto_chart(result["data"])
            if chart:
                elements.append(cl.Plotly(name="Chart", figure=chart, display="inline"))

        await cl.Message(
            content=result["answer"],
            elements=elements
        ).send()

    # Update conversation history
    chat_history.append((message.content, result.get("answer", "")))
    cl.user_session.set("chat_history", chat_history[-5:])  # keep last 5
```

### Key Chainlit Features Used

| Feature | How It's Used |
|---------|--------------|
| **`@cl.on_chat_start`** | Initialize SQLAgent and DuckDB connection once per session |
| **`@cl.on_message`** | Handle each user question — the main event loop |
| **`cl.Step`** | Show "Generating SQL" and "Executing Query" as collapsible steps in the UI. Users can expand to see the generated SQL and execution details. |
| **`cl.Message`** | Send the natural language answer back to the user |
| **`cl.Text`** | Attach data tables (Markdown-formatted) as side-panel elements |
| **`cl.Plotly`** | Attach interactive Plotly charts inline with the response |
| **`cl.user_session`** | Store agent instance and conversation history per session (no manual state management) |
| **`cl.make_async`** | Wrap synchronous agent calls to prevent UI blocking |
| **Streaming** | For the summarization step, use `msg.stream_token()` to stream the LLM's natural language response token-by-token |

### Auto-Chart Heuristics

| Result Shape | Chart Type |
|-------------|------------|
| 2 columns: 1 categorical + 1 numeric | Horizontal bar chart |
| 2+ columns with a year/date column + numeric | Line chart |
| All other shapes | Table only (no auto-chart) |

Charts generated with Plotly and attached via `cl.Plotly` element.

### Chainlit Configuration (`.chainlit/config.toml`)

```toml
[project]
name = "Open Payments Data Analyst"
enable_telemetry = false

[UI]
name = "Open Payments Data Analyst"
description = "Ask questions about pharmaceutical payments to physicians (2021-2024)"
default_theme = "dark"

[features]
prompt_playground = false
multi_modal = false

[UI.theme.dark]
primary = "#3B82F6"
background = "#1E1E1E"
paper = "#2D2D2D"
```

### Error Handling in UI

| Scenario | User Sees |
|----------|-----------|
| Ollama not running | Error message on chat start with setup instructions |
| DuckDB file missing | Message directing user to run `python ingest.py` |
| Agent returns error after retries | Error message + last attempted SQL in a collapsible step |
| LLM timeout | Timeout message with suggestion to simplify the question |

Errors are caught in `@cl.on_chat_start` (startup checks) and `@cl.on_message` (query errors). No unhandled exceptions reach the user.

### Launch Command

```bash
chainlit run app.py
```

Opens at `http://localhost:8000` by default. Port configurable in `.chainlit/config.toml`.

### Acceptance Criteria
- [ ] `chainlit run app.py` launches and shows a professional chat interface
- [ ] Typing a question shows agent steps (SQL generation → execution) as collapsible sections
- [ ] Answer displays with natural language summary, data table, and chart (where applicable)
- [ ] Conversation history persists across messages within a session
- [ ] Follow-up questions work correctly using conversation context
- [ ] Ollama not running → informative error message (not a crash)
- [ ] DuckDB missing → informative error message (not a crash)
- [ ] UI does not freeze during long-running queries (async architecture)

### Dependencies
Phase 0 (Chainlit installed), Phase 1 (DuckDB exists), Phase 2 (agent module).

---

## Phase 4: Integration & Testing

### Goal
End-to-end validation, prompt tuning, error hardening, and performance verification.

### Files Modified
All files from prior phases — bug fixes, prompt tuning, edge case handling.

### Test Query Suite (15 queries)

| # | Category | Query | Expected Behavior |
|---|----------|-------|-------------------|
| 1 | Simple count | "How many general payment records are in 2024?" | Returns ~15.4M |
| 2 | Simple count | "How many research payment records exist across all years?" | Returns ~3.6M |
| 3 | Top-N aggregation | "Top 10 companies by total payment amount across all years" | Ranked list with dollar amounts |
| 4 | Filtered aggregation | "Consulting fee payments over $100,000 in New York in 2023" | Filtered results |
| 5 | Cross-year comparison | "Compare total payments by year from 2021 to 2024" | 4-row result, line chart |
| 6 | Specialty analysis | "Which medical specialties received the most payments in 2024?" | Ranked by specialty |
| 7 | Research-specific | "Top 5 therapeutic areas by research funding in 2024" | Uses research_payments table |
| 8 | Ownership | "How many physicians have ownership interests?" | Uses ownership_payments table |
| 9 | Geographic | "Show total payments by state in 2024, top 20" | State-level aggregation |
| 10 | Product analysis | "What are the top drugs by associated payment amount in 2024?" | Product-level analysis |
| 11 | Payment type breakdown | "Break down payment forms (cash, in-kind, etc.) for 2024" | Category distribution |
| 12 | Time series | "Monthly payment trends for 2024" | Date-based aggregation |
| 13 | Follow-up | "Now show me the same for 2023" (after query #12) | Conversation context used |
| 14 | Unanswerable | "What is the weather in Baltimore?" | Graceful decline |
| 15 | Ambiguous | "Show me the biggest payments" | Reasonable assumption or clarifying question |

**Target:** At least 12 of 15 queries produce correct, useful answers.

### Prompt Engineering Iteration

Common issues to watch for and fix:
- LLM using approximate column names (`Payment_Amount` instead of `Total_Amount_of_Payment_USDollars`) → emphasize exact names in prompt
- LLM forgetting UNION views for cross-year queries → add explicit routing instructions
- LLM generating PostgreSQL/MySQL syntax → add DuckDB-specific examples
- LLM not applying LIMIT → reinforce default LIMIT rule

### Performance Validation

| Metric | Target |
|--------|--------|
| Ingestion time (full 33 GB) | < 30 minutes |
| RAM during ingestion | < 8 GB (DuckDB streams) |
| Ollama VRAM usage | ~10 GB for Qwen2.5-Coder-14B Q4_K_M |
| Aggregation on 55M rows (DuckDB) | < 5 seconds |
| End-to-end query response | < 30 seconds |
| Stable interactive session | 30+ minutes without memory leaks |

### Error Handling Hardening
- All DuckDB queries wrapped in try/except with meaningful messages
- Ollama connection errors handled (timeout, model not loaded)
- Malformed LLM responses handled (no SQL found)
- Configurable LLM timeout (default 120s)
- Startup config validation in `app.py` via `@cl.on_chat_start` (required keys exist, paths valid)

### Acceptance Criteria
- [ ] 12+ of 15 test queries produce correct answers
- [ ] No unhandled exceptions during any test scenario
- [ ] Ingestion completes in under 30 minutes
- [ ] Query response time under 30 seconds for all test queries
- [ ] Application runs stably for 30+ minute interactive session

### Dependencies
Phases 0–3 all complete.

---

## Phase 5: Future Enhancements (Post-MVP)

These are out of scope for the initial build but documented for future planning.

| Enhancement | Effort | Value | Description |
|------------|--------|-------|-------------|
| **Export results** | Low | High | Chainlit `cl.File` element for CSV/Excel download of query results |
| **Saved queries** | Medium | High | Pin frequently-used queries to sidebar. Store in JSON or DuckDB table. |
| **Smart column selection** | Medium | High | Two-pass approach: LLM first identifies relevant tables/columns, then gets detailed schema. Improves accuracy on the 252-column Research table. |
| **Data freshness check** | Low | Medium | Compare CSV dates vs Parquet dates. Warn in UI if CSVs are newer, suggesting re-ingest. |
| **Alternative model providers** | Low | Medium | Implement provider abstraction in agent.py to support `openai` and `anthropic` providers from config (paid option for users who want it). |
| **RAG over CMS docs** | High | Medium | Embed CMS methodology documents using local `nomic-embed-text` model + FAISS/ChromaDB. Answer policy-level questions beyond pure data queries. |
| **Dashboard mode** | High | High | Separate Chainlit page or custom element with pinned auto-refreshing charts: payment trends, top companies, geographic distribution. |
| **Voice input** | Medium | Low | Whisper-based speech-to-text via local model for hands-free querying. |

---

## Known Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Research table (252 columns) too large for LLM prompt | SQL errors on research queries | Compact schema with key columns only; full schema in `_schema_metadata` for reference |
| CSV parsing edge cases (free text with commas/quotes) | Failed ingestion | DuckDB's `read_csv_auto` with `sample_size=10000` and `ignore_errors=true`; fallback to explicit CSV options |
| Removed/Deleted has no data dictionary | Missing metadata | Hardcode 4 column descriptions in `ingest.py` |
| LLM generates invalid SQL | Query failures | Self-correction loop (3 retries); prompt engineering in Phase 4 |
| VRAM pressure (model + KV cache) | Slow inference or OOM | Qwen2.5-Coder-14B Q4_K_M uses ~10 GB of 16 GB available; monitor and fall back to smaller model if needed |
| Data dictionary filename typo (`Paymemnts`) | File not found | Code matches the actual typo in filenames |

---

## File Summary

| File | Phase | Lines (est.) | Purpose |
|------|-------|-------------|---------|
| `config.yaml` | 0 | ~25 | All application settings |
| `requirements.txt` | 0 | ~12 | Python dependencies |
| `ingest.py` | 1 | ~150–200 | CSV → Parquet → DuckDB pipeline |
| `agent.py` | 2 | ~200–250 | LLM agent: Text-to-SQL + summarization |
| `app.py` | 3 | ~150–200 | Chainlit chat UI |
| **Total** | | **~550–700** | |

---

## Dependency Graph

```
Phase 0: Environment Setup
    │
    ▼
Phase 1: Data Ingestion Pipeline
    │
    ▼
Phase 2: LLM Agent Core
    │
    ▼
Phase 3: Chat UI
    │
    ▼
Phase 4: Integration & Testing
    │
    ▼
Phase 5: Future Enhancements (post-MVP)
```

All phases are sequential. Phase 2 and 3 could theoretically be parallelized (UI can be stubbed with mock data), but sequential is simpler since the UI depends on the agent's return type contract.
