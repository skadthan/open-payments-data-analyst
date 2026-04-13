# RAG Over CMS Docs — Implementation Plan & Summary

## Problem Statement

The AI-generated SQL suffered from categorical column ambiguity. When users asked about "physician" payments, the LLM generated `Covered_Recipient_Type ILIKE '%Physician%'` which matched **both** "Covered Recipient Physician" **and** "Covered Recipient Non-Physician Practitioner" — producing incorrect results. The root cause was that the LLM had no knowledge of actual distinct values stored in the database.

Additionally, the system could only answer data/SQL questions but not policy-level questions about CMS Open Payments rules, methodology, reporting requirements, or definitions.

## Solution: Two Complementary Approaches

### Part 1: Distinct Value Catalog (SQL Quality Fix)

**What**: At startup, query DuckDB for the distinct values of key categorical columns and inject them directly into the LLM's system prompt alongside the schema.

**Changes**:
- **`agent.py`** — Added `CATEGORICAL_COLUMNS` constant defining which low-cardinality columns to enumerate
- **`agent.py`** — Added `SchemaManager._load_distinct_values()` method that queries DuckDB at startup
- **`agent.py`** — Updated `SchemaManager.compact_schema()` to append `VALUES: "val1", "val2", ...` under each categorical column
- **`agent.py`** — Added **Rule 15** to the system prompt: "Categorical columns with VALUES listed store EXACT strings. Use exact values, not partial ILIKE."
- **`agent.py`** — Added a **few-shot example** (#4) demonstrating exact-match categorical filtering

**Categorical columns enumerated**:
| Table | Column | Example Values |
|-------|--------|----------------|
| general_payments | Covered_Recipient_Type | "Covered Recipient Physician", "Covered Recipient Non-Physician Practitioner", "Covered Recipient Teaching Hospital" |
| general_payments | Nature_of_Payment_or_Transfer_of_Value | "Food and Beverage", "Consulting Fee", "Travel and Lodging", ... (16 values) |
| general_payments | Form_of_Payment_or_Transfer_of_Value | "Cash or cash equivalent", "In-kind items and services", ... (6 values) |
| general_payments | Physician_Ownership_Indicator | "True", "False" |
| research_payments | Expenditure_Category1 | "Patient Care", "Non-patient Care", "Professional Salary Support", ... (6 values) |
| ownership_payments | Interest_Held_by_Physician_or_an_Immediate_Family_Member | "Physician Covered Recipient", "Immediate family member" |
| removed_deleted | Change_Type | "DELETED", "REMOVED" |
| removed_deleted | Payment_Type | "General Payments", "Research Payments", "Ownership or Investment Interest" |

### Part 2: RAG Over CMS Documentation (Policy Q&A)

**What**: Parse PDF documents from `ProgramData/`, embed them with `nomic-embed-text` via Ollama, store in ChromaDB, and enable semantic retrieval for policy/methodology questions.

**New files**:
- **`rag.py`** — Complete RAG pipeline: PDF parsing (pymupdf), chunking, Ollama embedding, ChromaDB storage, query interface, query routing, CLI

**Modified files**:
- **`app.py`** — RAG initialization in `on_chat_start`, query routing in `_answer_question` (SQL/RAG/hybrid paths), RAG answer streaming with source citations
- **`agent.py`** — Added `stream_rag_answer()` method for streaming RAG responses through the summary LLM
- **`config.yaml`** — New `rag` section with configuration options
- **`requirements.txt`** — Added `pymupdf>=1.24`, `chromadb>=0.5`

## Architecture

```
User Question
    │
    ▼
┌──────────────────────────┐
│   Query Router            │
│   (keyword-based)         │
│   classify_question()     │
└──────┬───────┬───────┬───┘
       │       │       │
    sql│    rag│  hybrid│
       │       │       │
       ▼       ▼       ▼
┌──────────┐ ┌─────────┐ ┌─────────────────────┐
│ SQL Path │ │RAG Path │ │ Hybrid Path          │
│ (existing│ │ChromaDB │ │ RAG context injected │
│  agent)  │ │→LLM     │ │ into SQL prompt      │
└──────────┘ └─────────┘ └─────────────────────┘
```

### Query Routing Logic
- **SQL indicators**: "how much", "total", "top", "compare", "trend", "by year", "count", "sum", etc.
- **Policy indicators**: "what is open payments", "reporting requirements", "sunshine act", "exemption", "threshold", etc.
- If only policy → RAG path
- If only SQL → SQL path
- If both → hybrid (RAG context injected into SQL prompt)
- If RAG unavailable → always SQL

### PDF Ingestion Summary
| Document | Size | Category | Chunks |
|----------|------|----------|--------|
| Open Payments FAQs | 0.6 MB | faq | 79 |
| 2014 Federal Register | 27.9 MB | law_policy | 1,028 |
| 42 CFR Part 403 | 0.2 MB | law_policy | 30 |
| ACA Section 6002 Final Rule | 0.6 MB | law_policy | 210 |
| BILLS-115hr6enr | 0.7 MB | law_policy | 255 |
| PLAW-111publ148 | 2.4 MB | law_policy | 1,121 |
| Data Dictionary Methodology | 1.4 MB | data_dictionary | 132 |
| User Guide (Recipients) | 20.9 MB | user_guide | 211 |
| User Guide (Reporting Entities) | 32.3 MB | user_guide | 353 |
| 2019 Federal Register | 206.2 MB | law_policy | **Skipped** (>50 MB limit) |
| Transparency Reports (OII) | 1.9 MB | law_policy | **Skipped** (image-only PDF) |
| **Total** | | | **3,259 chunks** |

### Configuration (`config.yaml`)
```yaml
rag:
  enabled: true
  pdf_dir: ./ProgramData
  vectorstore_dir: ./data/vectorstore
  embedding_model: nomic-embed-text
  top_k: 5
  max_file_size_mb: 50
  chunk_size: 3200
  chunk_overlap: 200
```

## Usage

### Building the Vector Store
```bash
# First time — pull the embedding model
ollama pull nomic-embed-text

# Ingest PDFs and build ChromaDB
python rag.py --ingest

# Force rebuild from scratch
python rag.py --rebuild

# Check status
python rag.py --status

# Test a query
python rag.py --query "What is a covered recipient?"
```

### Running the App
```bash
python run.py
```

The app automatically initializes RAG if `rag.enabled: true` in config and the vector store has been built. If RAG is unavailable, the SQL pipeline works exactly as before.

## Testing

### Part 1 — SQL Quality
- Ask: "What is the total % of payments received by physician type in 2024?"
  - Expected: `WHERE Covered_Recipient_Type = 'Covered Recipient Physician'` (NOT `ILIKE '%Physician%'`)
- Ask: "Show payments by recipient type"
  - Expected: All 3 types listed correctly
- Ask: "What forms of payment exist?"
  - Expected: References exact values from the catalog

### Part 2 — RAG
- Ask: "What is the Open Payments program?" → Routes to RAG, cites FAQ
- Ask: "What are the reporting thresholds?" → Routes to RAG, cites methodology
- Ask: "How much did physicians receive in 2024, and what are the reporting requirements?" → Hybrid path

## Dependencies Added
- `pymupdf>=1.24` — PDF text extraction
- `chromadb>=0.5` — Vector store with persistent disk storage
- `nomic-embed-text` — Ollama embedding model (768-dim, local)
