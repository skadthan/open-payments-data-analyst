# Open Payments Data Analyst — Architecture Plan

## 1. Overview

A locally-hosted, AI-powered data analyst for CMS Open Payments data (2021–2024). Users ask questions in plain English through a chat interface; an LLM agent translates them into SQL, executes against a high-performance analytical database, and returns summarized answers with optional charts. The goal is an experience similar to **Amazon Q Business** — upload data, ask questions, get answers — but running entirely on local hardware with open-source models.

---

## 2. Data Profile

| Dataset | Years | Rows (Total) | CSV Size |
|---------|-------|-------------|----------|
| General Payments | 2021–2024 | ~54.9M | ~31 GB |
| Research Payments | 2021–2024 | ~3.6M | ~1.5 GB |
| Ownership/Investment | 2021–2024 | ~17K | Tiny |
| Removed/Deleted | 2021–2024 | ~19K | Tiny |
| **Total** | | **~58.5M rows, up to 91 columns** | **~33 GB CSV** |

After Parquet conversion (snappy/zstd compression): **~4–6 GB on disk**.

### Data Dictionary Coverage

12 JSON data dictionary files under `DataDictionaries/` (3 per year: General, Research, Ownership) provide complete field-level metadata — names, descriptions, types, constraints, and examples. These are automatically injected into the LLM system prompt for schema-aware SQL generation.

---

## 3. Target Hardware

| Component | Spec |
|-----------|------|
| CPU | AMD Ryzen 9 7900X (12-core / 24-thread) |
| RAM | 32 GB DDR5 |
| GPU | NVIDIA GeForce RTX 4070 Ti SUPER (16 GB VRAM) |
| OS | Windows 11 |

This hardware comfortably supports a quantized 14B-parameter model on GPU while leaving CPU/RAM headroom for DuckDB analytical queries across the full dataset.

---

## 4. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                   Chainlit Chat UI                      │
│         (simple chat, table display, basic charts)       │
└──────────────────────┬──────────────────────────────────┘
                       │  user question / conversation
┌──────────────────────▼──────────────────────────────────┐
│              Agent Orchestrator (LangChain)               │
│                                                          │
│  1. Receive natural language question                     │
│  2. Build schema-aware prompt (from data dictionaries)   │
│  3. Call LLM → generate SQL                              │
│  4. Validate & execute SQL against DuckDB                │
│  5. If error → feed error back to LLM → retry (max 3)   │
│  6. Summarize results in natural language                 │
│  7. Return answer + data table to UI                     │
└──────────┬───────────────────────┬──────────────────────┘
           │                       │
┌──────────▼──────────┐  ┌────────▼─────────────────────┐
│   Ollama (LLM)      │  │   DuckDB (Analytics Engine)   │
│                     │  │                               │
│  - Local model      │  │   - Queries Parquet files     │
│    server           │  │     directly (4–6 GB)         │
│  - OpenAI-compat    │  │   - In-process (no server)    │
│    API              │  │   - Schema + metadata views   │
│  - GPU-accelerated  │  │   - 58.5M rows, sub-second   │
│  - Swap models via  │  │     for most aggregations     │
│    config           │  │                               │
└─────────────────────┘  └───────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                 Data Ingestion (one-time)                 │
│                                                          │
│  ingest.py:                                              │
│  - Auto-discovers CSVs under Datasets/                   │
│  - Converts to Parquet (snappy compression)              │
│  - Registers tables in DuckDB catalog                    │
│  - Loads data dictionaries as schema metadata             │
└─────────────────────────────────────────────────────────┘
```

### Why This Architecture

| Decision | Rationale |
|----------|-----------|
| **DuckDB in-process** (no FastAPI) | Eliminates an unnecessary network hop. DuckDB runs inside the Python process and queries 55M rows in milliseconds. No serialization overhead, no extra service to manage. |
| **Parquet over raw CSV** | Columnar storage with compression: 33 GB → 4–6 GB. DuckDB reads Parquet natively. Orders of magnitude faster for analytical queries. |
| **Ollama as model server** | Standard OpenAI-compatible API. Swap models with one config change. Automatic GPU offloading. Every LLM framework integrates natively. |
| **LangChain orchestrator** | Mature agent framework with built-in SQL agent tooling, retry logic, and conversation memory. Avoids reinventing the wheel. |
| **Chainlit UI** | Professional LLM chat interface in ~150–200 lines of Python. Native streaming, agent step visibility, table/chart rendering. No frontend build step. |
| **Single YAML config** | One file controls model, data paths, UI settings. Swap LLM providers or models without touching code. |

---

## 5. Model Recommendation

For Text-to-SQL + data summarization on 16 GB VRAM:

| Model | Quantized Size | VRAM | SQL Quality | Notes |
|-------|---------------|------|-------------|-------|
| **Qwen2.5-Coder-14B-Q4_K_M** | ~8.5 GB | ~10 GB | Excellent | **Top pick.** Best SQL generation at this size class. Comfortable VRAM fit. |
| DeepSeek-Coder-V2-Lite-16B-Q4 | ~9 GB | ~11 GB | Excellent | Strong alternative, good reasoning. |
| Mistral-Small-24B-Q4 | ~14 GB | ~15 GB | Very Good | Tight VRAM fit but good all-rounder. |
| Gemma 4 26B MoE A4B | ~15 GB | ~16 GB | Very Good | May cause VRAM swapping at 16 GB. |
| Gemma 4 E4B (3B effective) | ~3 GB | ~4 GB | Decent | Lightweight fallback; weaker on complex SQL. |

**Primary**: `Qwen2.5-Coder-14B` in Q4_K_M quantization — dominates SQL benchmarks, fits in VRAM with room for KV cache, leaves system RAM free for DuckDB.

**Fallback**: `Gemma 4 E4B` for quick/simple queries or if VRAM is needed elsewhere.

All models are swappable via `config.yaml` — no code changes required.

---

## 6. Configuration Design

```yaml
# config.yaml — single file controls the entire application

model:
  provider: ollama              # options: ollama, openai, anthropic
  name: qwen2.5-coder:14b      # any Ollama model tag
  base_url: http://localhost:11434
  temperature: 0.1              # low for deterministic SQL
  max_retries: 3                # SQL self-correction attempts

data:
  source_dir: ./Datasets
  parquet_dir: ./data/parquet
  duckdb_path: ./data/openpayments.duckdb
  dictionaries_dir: ./DataDictionaries

ui:
  title: "Open Payments Data Analyst"
  max_display_rows: 1000
  show_sql: true                # show generated SQL in UI
  show_charts: true             # auto-generate charts for numeric results
```

### Configuration Scenarios

| Want to... | Change |
|------------|--------|
| Switch to a different local model | `model.name: mistral-small:24b` |
| Use Claude API instead of local | `model.provider: anthropic`, add API key |
| Use OpenAI GPT instead | `model.provider: openai`, add API key |
| Add 2025 data | Drop CSVs into `Datasets/`, re-run `ingest.py` |
| Hide SQL from end users | `ui.show_sql: false` |

---

## 7. Implementation Files

| File | Purpose | Approx. Size |
|------|---------|-------------|
| `config.yaml` | All application settings | ~20 lines |
| `ingest.py` | CSV → Parquet conversion, DuckDB schema registration, data dictionary loading | ~150 lines |
| `agent.py` | LLM agent: schema-aware prompt building, Text-to-SQL, self-correction loop, result summarization | ~200 lines |
| `app.py` | Chainlit chat UI with conversation history, agent steps, table/chart display | ~150–200 lines |
| `requirements.txt` | Python dependencies | ~10 lines |

---

## 8. Agent Workflow (Detail)

```
User: "What are the top 10 pharma companies by total payments in 2024?"
                │
                ▼
┌─ Agent Orchestrator ──────────────────────────────────┐
│                                                        │
│  1. Load schema context from data dictionaries         │
│     → table names, column names, types, descriptions   │
│                                                        │
│  2. Build prompt:                                      │
│     SYSTEM: You are a SQL analyst. Here are the        │
│     available tables and their schemas: [...]           │
│     USER: "Top 10 pharma companies by payments 2024"   │
│                                                        │
│  3. LLM generates SQL:                                 │
│     SELECT Applicable_Manufacturer_or_Applicable_GPO   │
│       _Making_Payment_Name AS company,                 │
│       SUM(Total_Amount_of_Payment_USDollars) AS total  │
│     FROM general_payments_2024                         │
│     GROUP BY company ORDER BY total DESC LIMIT 10;     │
│                                                        │
│  4. Execute SQL on DuckDB                              │
│     → Success? Return results.                         │
│     → Error? Feed error to LLM, retry (up to 3x).     │
│                                                        │
│  5. LLM summarizes: "The top pharmaceutical company    │
│     by payments in 2024 was X with $Y..."              │
│                                                        │
│  6. Return to UI: summary + data table + bar chart     │
└────────────────────────────────────────────────────────┘
```

---

## 9. Prerequisites & Setup

```bash
# 1. Install Ollama (from ollama.com)
# 2. Pull the recommended model
ollama pull qwen2.5-coder:14b

# 3. Install Python dependencies
pip install -r requirements.txt

# 4. Ingest data (one-time, converts CSV → Parquet, builds DuckDB catalog)
python ingest.py

# 5. Launch the app
chainlit run app.py
```

### Python Dependencies

```
chainlit>=1.1
duckdb>=0.10
langchain>=0.2
langchain-community>=0.2
ollama>=0.3
pyarrow>=15.0
pyyaml>=6.0
plotly>=5.18
```

---

## 10. Future Enhancements (Out of Scope for MVP)

- **RAG over documentation**: Embed the CMS methodology documents for policy-level Q&A beyond pure data queries.
- **Scheduled reports**: Auto-generate weekly/monthly payment trend summaries.
- **Multi-user support**: Add authentication and session isolation.
- **Export**: Download query results as CSV/Excel/PDF.
- **Voice input**: Whisper-based speech-to-text for hands-free querying.
- **Dashboard mode**: Pinned queries that auto-refresh as saved dashboard cards.
