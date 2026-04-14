# Open Payments Data Analyst

A locally-hosted, AI-powered data analyst for **CMS Open Payments** data. Ask questions about pharmaceutical and medical device industry payments to physicians and teaching hospitals in plain English — get answers backed by real data.

## What is Open Payments?

[Open Payments](https://www.cms.gov/OpenPayments) is a federal program run by the Centers for Medicare & Medicaid Services (CMS) that collects and publishes information about financial relationships between the healthcare industry (drug and device manufacturers, group purchasing organizations) and healthcare providers (physicians, teaching hospitals). This transparency data is mandated by the Sunshine Act.

## Project Goal

Build an **Amazon Q Business-like experience** that runs entirely on local hardware using open-source models:

- **Upload data** — Point at CMS Open Payments CSV datasets (2021–2024)
- **Ask questions** — Natural language queries through a simple chat interface
- **Get answers** — AI translates questions to SQL, executes against the data, and returns summarized results with tables and charts

No cloud dependencies. No API costs. No data leaving your machine.

## Data Scope

| Dataset | Years | Total Rows | Description |
|---------|-------|-----------|-------------|
| General Payments | 2021–2024 | ~54.9M | Payments to physicians/hospitals not tied to research (consulting fees, travel, food, speaking, etc.) |
| Research Payments | 2021–2024 | ~3.6M | Payments made in connection with research agreements or protocols |
| Ownership/Investment | 2021–2024 | ~17K | Physician ownership or investment interests in manufacturers/GPOs |
| Removed/Deleted | 2021–2024 | ~19K | Previously published records that were deleted or became ineligible |

**~58.5 million records** across 4 program years, with up to 91 columns per record.

## Example Questions

- "What are the top 10 pharmaceutical companies by total payments in 2024?"
- "How much did payments for consulting fees grow from 2021 to 2024?"
- "Which medical specialties received the most research funding in 2023?"
- "Show me the distribution of payment types for cardiologists in New York"
- "Compare food and beverage spending vs consulting fees across all years"

## Architecture

```
User (Chat UI)  →  Agent Orchestrator  →  LLM (Ollama, local)
                                       →  DuckDB (analytical queries on Parquet files)
```

- **DuckDB** — In-process analytical database. Queries 58.5M rows in milliseconds. CSV data is converted to Parquet (~4-6 GB compressed) for optimal performance.
- **Ollama** — Local model server with OpenAI-compatible API. Runs quantized open-source models on GPU. Swap models via config.
- **LangChain** — Agent framework for Text-to-SQL generation, self-correction, and result summarization.
- **Chainlit** — Professional chat interface purpose-built for LLM apps, with streaming, agent step visibility, and interactive charts.

See [to-be-architecture-plan.md](to-be-architecture-plan.md) for the full architecture design, model recommendations, and implementation details.

## Configuration

A single `config.yaml` controls the entire application — model selection, data paths, UI settings. Switch between local models (Gemma, Qwen, Mistral, DeepSeek) or cloud APIs (OpenAI, Anthropic) with a one-line change.

## Hardware Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| RAM | 16 GB | 32 GB |
| GPU VRAM | 8 GB | 16 GB |
| Disk (for Parquet data) | 10 GB free | 20 GB free |
| CPU | 4 cores | 8+ cores |

## Quick Start

```bash
# 1. Install Ollama (https://ollama.com)
ollama pull qwen2.5-coder:14b

# 2. Install Python dependencies
pip install -r requirements.txt

# 3. Ingest data (one-time CSV → Parquet conversion)
python ingest.py

# 4. Launch
python run.py
```

> **Note:** Use `python run.py`, not `chainlit run app.py` directly. The
> wrapper neutralizes a `nest_asyncio.apply()` call inside chainlit's CLI
> that breaks `asyncio.current_task()` on Python 3.14 and leaves the
> browser with a blank screen. See `phase-3-plan.md` "Observed issues
> and fixes" for the full bisect.

### Moving the project between machines

DuckDB bakes absolute paths into view DDL, so `openpayments.duckdb` is
**not portable by itself**. Two supported options:

1. **Re-ingest on each machine (recommended).** Copy `Datasets/` and run
   `python ingest.py` locally. Fastest to reason about; the DB is always
   in sync with the parquet files on the machine that built it.
2. **Copy `data/parquet/` only, let startup re-register the views.**
   `python run.py` calls `ingest.refresh_views()` on every startup, which
   rediscovers parquets under `config.data.parquet_dir` and rewrites all
   per-year and `all_*` views with correct local paths. If you copy the
   `.duckdb` file too it's repaired in place; if not, it's built fresh.

Python 3.11 or 3.12 is recommended (3.9 is EOL and chainlit ≥1.1 hits
ContextVar issues on it; 3.14 works because `run.py` patches
`nest_asyncio`).

## Project Structure

```
├── AI-Chat/                  # Research and conversation logs
├── DataDictionaries/         # JSON schema definitions per year (2021–2024)
│   ├── 2021/
│   ├── 2022/
│   ├── 2023/
│   └── 2024/
├── Datasets/                 # Raw CMS CSV files (not committed — .gitignored)
├── to-be-architecture-plan.md  # Detailed architecture and implementation plan
├── config.yaml               # Application configuration (TBD)
├── ingest.py                 # Data ingestion pipeline (TBD)
├── agent.py                  # LLM agent orchestrator (TBD)
├── app.py                    # Chainlit chat UI (TBD)
└── requirements.txt          # Python dependencies (TBD)
```

## Data Source

All datasets are sourced from the [CMS Open Payments](https://www.cms.gov/OpenPayments) program and are publicly available. Data dictionaries describing every field are included in this repository under `DataDictionaries/`.

## License

This project is for educational and analytical purposes. The Open Payments data is public domain, published by CMS.
