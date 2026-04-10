# Phase 1: Data Ingestion Pipeline — Implementation Plan

## Objective
Convert the 33 GB of CMS Open Payments CSVs into compressed Parquet files, register them as DuckDB views, build UNION views spanning all years, and populate a `_schema_metadata` table from the JSON data dictionaries. The end state is a single `data/openpayments.duckdb` file that Phase 2's agent can query directly, plus a `data/parquet/` directory holding the columnar data.

---

## Pre-Implementation Audit

### Environment (inherited from Phase 0)
All Phase 1 dependencies were installed as part of Phase 0 — **nothing new needs to be downloaded**:

| Package | Version | Role in Phase 1 |
|---|---|---|
| `duckdb` | 1.5.1 | CSV→Parquet streaming + persistent catalog |
| `pyarrow` | 23.0.1 | Parquet I/O backend |
| `pyyaml` | 6.0.3 | Load `config.yaml` |
| `pandas` | 3.0.2 | Not used in ingest.py (reserved for agent/UI) |

### Source Data (confirmed on disk, 2026-04-10)
Structure under `./Datasets/`:

```
Datasets/
├── PGYR2021_P01232026_01102026/
│   ├── OP_DTL_GNRL_PGYR2021_P01232026_01102026.csv
│   ├── OP_DTL_OWNRSHP_PGYR2021_P01232026_01102026.csv
│   ├── OP_DTL_RSRCH_PGYR2021_P01232026_01102026.csv
│   ├── OP_REMOVED_DELETED_PGYR2021_P01232026_01102026.csv
│   └── OP_PGYR2021_README_P01232026.txt
├── PGYR2022_P01232026_01102026/
├── PGYR2023_P01232026_01102026/
└── PGYR2024_P01232026_01102026/
```

| File | 2024 size | Notes |
|---|---:|---|
| `OP_DTL_GNRL_*.csv` | 8.91 GB | Largest — general payments |
| `OP_DTL_RSRCH_*.csv` | 727 MB | 252 columns |
| `OP_DTL_OWNRSHP_*.csv` | 2.1 MB | 30 columns |
| `OP_REMOVED_DELETED_*.csv` | 614 KB | 4 columns, no dictionary |

**Total across all 4 years: ~33 GB of CSV.** Expected Parquet output: **4–6 GB** (snappy compression, ~85% reduction).

### Data Dictionaries
Under `./DataDictionaries/{2021..2024}/` — schemas are identical across years within each type, so Phase 1 only reads the 2024 version of each dictionary.

| Dictionary (2024) | Field count | Maps to table type |
|---|---:|---|
| `General_Paymemnts_DataDictionary_2024.json`  | 91  | `general_payments` |
| `Research_Paymemnts_DataDictionary_2024.json` | 252 | `research_payments` |
| `Ownership_Paymemnts_DataDictionary_2024.json`| 30  | `ownership_payments` |
| *(none)* | — | `removed_deleted` |

The CMS filename typo `Paymemnts` is preserved in the code.

JSON structure verified: `{"data": {"fields": [{"name", "description", "type", "example", "constraints"}, ...]}}`.

### Expected end state
| Artifact | Expected value |
|---|---|
| Parquet files in `data/parquet/` | **16** (4 types × 4 years) |
| Per-year views in DuckDB | 16 |
| UNION views | 4 (`all_general_payments`, `all_research_payments`, `all_ownership_payments`, `all_removed_deleted`) |
| `_schema_metadata` rows | **377** (91 + 252 + 30 + 4) |
| `all_general_payments` row count | ~55 M |
| Total ingestion time | < 30 min on Ryzen 9 7900X |

---

## Implementation: `ingest.py`

### Module-level design

Single file (~250 lines), no classes — all functions at module scope, orchestrated from `main()`. Rationale: simpler to read for a one-shot pipeline, and nothing in Phase 1 needs to persist state between calls.

### Function map

| Function | Responsibility |
|---|---|
| `load_config(path)` | Parse `config.yaml` |
| `discover_csvs(source_dir)` | Walk `./Datasets`, regex-match filenames, return sorted `(table_type, year, path)` tuples |
| `convert_csv_to_parquet(con, csv_path, parquet_path, …)` | Stream one CSV into a Parquet file using `COPY … TO … (FORMAT PARQUET)`; return `(row_count, elapsed_sec)` |
| `register_parquet_tables(con, manifest)` | Create per-year `CREATE OR REPLACE VIEW` statements over the Parquet files, then build the 4 UNION views |
| `load_dictionary(path)` | Load a JSON dictionary, return its `data.fields` list |
| `build_schema_metadata(con, dict_dir)` | DROP + CREATE the `_schema_metadata` table, INSERT rows from each dictionary, plus 4 hardcoded rows for `removed_deleted` |
| `main()` | CLI parsing → wipe → discover → convert → register → metadata → summary |

### CSV discovery regex

```python
OP_(DTL_GNRL|DTL_RSRCH|DTL_OWNRSHP|REMOVED_DELETED)_PGYR(\d{4})_.*\.csv$
```

Group 1 → table type (via a mapping dict), group 2 → calendar year.

### CSV → Parquet conversion

Using **DuckDB itself** for the conversion — no Python row iteration, no pandas in the hot path:

```sql
COPY (
    SELECT * FROM read_csv_auto(
        '{csv}',
        all_varchar=false,
        sample_size=10000,
        ignore_errors=true
    )
) TO '{parquet}' (
    FORMAT PARQUET,
    COMPRESSION 'snappy',
    ROW_GROUP_SIZE 500000
)
```

- `all_varchar=false` — let DuckDB infer numeric/date types from the sample.
- `sample_size=10000` — from config.yaml (`ingestion.sample_size`).
- `ignore_errors=true` — skip the rare malformed line without aborting a 15M-row file.
- `COMPRESSION 'snappy'` — from config (`ingestion.compression`); prioritizes write speed over size.
- `ROW_GROUP_SIZE 500000` — from config; tuned for DuckDB scan efficiency on aggregate queries.

The conversion uses a **throwaway `duckdb.connect(":memory:")` connection**. The persistent `openpayments.duckdb` file is opened only at the end, for view + metadata creation, which keeps the DB file tiny (only catalog definitions — the data lives in Parquet).

### Table registration: views, not tables

For each Parquet file we do:

```sql
CREATE OR REPLACE VIEW general_payments_2024 AS
    SELECT * FROM read_parquet('/abs/path/to/general_payments_2024.parquet');
```

Views are preferred over `CREATE TABLE AS SELECT` because:
- **Instant** — view creation is metadata-only, no data copy.
- **Single source of truth** — the Parquet file is the data; the DuckDB file just catalogs pointers.
- **Persistent** — DuckDB stores the view SQL in the DB file; queries in later sessions transparently re-read the Parquet.
- **Keeps `openpayments.duckdb` small** — expected ≪ 1 MB.

Then for each of the 4 table types:

```sql
CREATE OR REPLACE VIEW all_general_payments AS
    SELECT * FROM general_payments_2021
    UNION ALL SELECT * FROM general_payments_2022
    UNION ALL SELECT * FROM general_payments_2023
    UNION ALL SELECT * FROM general_payments_2024;
```

The source data already has a `Program_Year` column for year filtering — no synthetic partition key needed.

### `_schema_metadata` table

```sql
CREATE TABLE _schema_metadata (
    table_type  VARCHAR,   -- 'general_payments', 'research_payments', 'ownership_payments', 'removed_deleted'
    column_name VARCHAR,
    data_type   VARCHAR,   -- mapped from dictionary 'type'
    description VARCHAR,
    example     VARCHAR,
    constraints VARCHAR    -- JSON string of the dictionary's 'constraints' object (or NULL)
);
```

**Type mapping (dictionary `type` → `data_type` column value):**

| Dictionary type | `data_type` |
|---|---|
| `string`  | `VARCHAR` |
| `integer` | `BIGINT` (NPI and Record_ID exceed 2³¹ — never INT) |
| `number`  | `DOUBLE` |
| `date`    | `DATE` |

This column is **metadata for the agent prompt builder**; it does not drive the actual Parquet dtypes (DuckDB's auto-detection does that). Storing it anyway gives the agent a clean type hint without having to query `information_schema`.

**Hardcoded rows for `removed_deleted`:**

| Column | Type | Description |
|---|---|---|
| `Change_Type` | VARCHAR | Indicator showing the record was REMOVED or DELETED relative to the previous publication |
| `Program_Year` | BIGINT | Calendar year in which the payment was originally reported |
| `Payment_Type` | VARCHAR | Category of the removed/deleted record: General, Research, or Ownership |
| `Record_ID` | BIGINT | System-generated unique identifier of the removed/deleted record |

### CLI flags

| Flag | Behavior |
|---|---|
| *(none)* or `--rebuild` | Default. Wipe `data/parquet/` + `data/openpayments.duckdb`, re-ingest everything. Primary mode for the 6-month refresh. |
| `--skip-existing` | Dev convenience. Skip Parquet conversion for files that already exist; still rebuild views + metadata. |

`--rebuild` and `--skip-existing` are mutually exclusive (`argparse` group).

### Progress reporting

Per CSV: `[idx/total] filename.csv -> filename.parquet ...` then on completion `done: N rows, size, elapsed_sec`.

End-of-run summary: file count, total rows, total Parquet size on disk, total conversion wall time.

### Known risks & how the implementation handles them

| Risk | Mitigation |
|---|---|
| Free-text fields with embedded commas / quotes break parsing | `read_csv_auto` with `ignore_errors=true` — worst case, a handful of rows are skipped out of 55M+ |
| Dictionary filename typo (`Paymemnts`) | Hardcoded into the prefix map; verified present on disk |
| `removed_deleted` has no dictionary | Hardcoded 4 rows inserted alongside the dictionary-driven rows |
| DuckDB persistent file grows huge if we INSERT data into it | We use a throwaway `:memory:` connection for CSV→Parquet; the persistent DB only stores views + metadata |
| Parquet paths containing Windows backslashes break DuckDB SQL string literals | `str(path).replace("\\", "/")` before embedding in SQL |
| Full 33 GB ingestion exceeds Bash 2-minute default timeout | Run the ingest as a background job and poll its log file |

---

## Execution Plan

1. Write `phase-1-plan.md` (this file).
2. Write `ingest.py`.
3. Kick off `python ingest.py --rebuild` as a background process, tee-ing stdout/stderr to `ingest.log`.
4. Monitor `ingest.log` for progress; on completion, verify acceptance criteria.
5. Back-fill this document's "Implementation Results" section with the actual numbers.
6. Commit `phase-1-plan.md` and `ingest.py`.

---

## Acceptance Criteria

- [x] `python ingest.py --rebuild` completes without errors
- [x] `data/parquet/` contains **16** Parquet files (4 types × 4 years)
- [x] DuckDB has all 16 per-year views + 4 UNION views + `_schema_metadata`
- [x] `SELECT COUNT(*) FROM all_general_payments` returns ~55M
- [x] `SELECT COUNT(*) FROM _schema_metadata` returns **377** rows
- [x] `python ingest.py --skip-existing` re-run completes in seconds
- [x] Total Parquet size on disk: ~4–6 GB  *(actual: **1.9 GB** — better than target)*
- [x] Total ingestion time: under 30 minutes  *(actual: **10.1 min**)*

---

## Implementation Results

### Run summary (2026-04-10, `python ingest.py --rebuild`)

Total wall time: **10.1 minutes** (606.5 seconds of CSV→Parquet conversion + ~1 s for views/metadata).

### Per-file conversion stats

| # | File | Rows | Parquet size | Time |
|--:|---|--:|--:|--:|
|  1 | `general_payments_2021.parquet`  | 11,552,288 | 389.5 MB |  52.7 s |
|  2 | `general_payments_2022.parquet`  | 13,306,467 | 447.8 MB |  96.0 s |
|  3 | `general_payments_2023.parquet`  | 14,700,786 | 521.3 MB | 156.5 s |
|  4 | `general_payments_2024.parquet`  | 15,385,047 | 518.8 MB | 169.1 s |
|  5 | `ownership_payments_2021.parquet`|      4,203 | 273.6 KB |   0.1 s |
|  6 | `ownership_payments_2022.parquet`|      4,146 | 272.1 KB |   0.1 s |
|  7 | `ownership_payments_2023.parquet`|      4,316 | 280.5 KB |   0.1 s |
|  8 | `ownership_payments_2024.parquet`|      4,591 | 283.0 KB |   0.1 s |
|  9 | `removed_deleted_2021.parquet`   |         24 |   1.1 KB |   0.0 s |
| 10 | `removed_deleted_2022.parquet`   |         81 |   1.4 KB |   0.0 s |
| 11 | `removed_deleted_2023.parquet`   |      3,824 |  17.2 KB |   0.0 s |
| 12 | `removed_deleted_2024.parquet`   |     14,939 |  64.2 KB |   0.0 s |
| 13 | `research_payments_2021.parquet` |    735,276 |  28.6 MB |  26.8 s |
| 14 | `research_payments_2022.parquet` |  1,002,997 |  30.7 MB |  37.3 s |
| 15 | `research_payments_2023.parquet` |  1,079,798 |  31.7 MB |  42.3 s |
| 16 | `research_payments_2024.parquet` |    756,906 |  25.7 MB |  25.2 s |

**Totals:** 58,555,689 rows, 1.9 GB on disk.

### UNION view row counts

| View | Rows |
|---|--:|
| `all_general_payments`   | 54,944,588 |
| `all_research_payments`  |  3,574,977 |
| `all_removed_deleted`    |     18,868 |
| `all_ownership_payments` |     17,256 |

### `_schema_metadata` contents

| `table_type` | Rows | Source |
|---|--:|---|
| `general_payments`   |  91 | `General_Paymemnts_DataDictionary_2024.json` |
| `research_payments`  | 252 | `Research_Paymemnts_DataDictionary_2024.json` |
| `ownership_payments` |  30 | `Ownership_Paymemnts_DataDictionary_2024.json` |
| `removed_deleted`    |   4 | Hardcoded in `ingest.py` |
| **Total**            | **377** | |

### Persistent DuckDB file size

`data/openpayments.duckdb` = **524 KB** — confirms the views-over-Parquet design (catalog-only; the 1.9 GB of actual data lives in the 16 Parquet files). This makes the DB file cheap to copy, back up, or snapshot between ingestion runs.

### Functional verification

A sample aggregation query executed successfully against the persistent DB (read-only mode):

```sql
SELECT Applicable_Manufacturer_or_Applicable_GPO_Making_Payment_Name AS company,
       ROUND(SUM(Total_Amount_of_Payment_USDollars), 2) AS total_usd
FROM all_general_payments
GROUP BY 1 ORDER BY 2 DESC LIMIT 5;
```

| Company | Total USD (all years) |
|---|--:|
| BioNTech SE          | $1,782,543,859.57 |
| Medtronic, Inc.      |   $506,153,987.87 |
| Stryker Corporation  |   $478,538,037.12 |
| ABBVIE INC.          |   $405,959,897.08 |
| Arthrex, Inc.        |   $402,059,658.09 |

BioNTech's dominance is consistent with COVID-19 vaccine royalty arrangements being reported via Open Payments during the 2021–2024 window — the data is plausible.

### `--skip-existing` re-run

Completed in **1.2 seconds**, as designed (skip conversion, rebuild views + metadata only). Confirms the dev-convenience flag works.

---

## Files created / modified in Phase 1

| File | Type | Purpose |
|---|---|---|
| `phase-1-plan.md` | New | This document |
| `ingest.py` | New | ~320-line ingestion pipeline |
| `data/parquet/*.parquet` | Generated (gitignored) | 16 columnar files, 1.9 GB total |
| `data/openpayments.duckdb` | Generated (gitignored) | 524 KB catalog with 20 views + `_schema_metadata` |
| `ingest.log` | Generated (gitignored) | Captured stdout of the rebuild run |

---

## Ready for Phase 2

Phase 2 (LLM Agent Core) can now start against a known-good data layer. The agent's `SchemaManager` will read `_schema_metadata` to build prompts, and its query executor will open `data/openpayments.duckdb` in read-only mode to run generated SQL against the `all_*` views or per-year views.
