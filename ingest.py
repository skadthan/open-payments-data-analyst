"""
Open Payments Data Ingestion Pipeline.

Discovers CMS Open Payments CSVs under ./Datasets, converts them to Parquet,
registers them in DuckDB as per-year views plus all_* UNION views, and
populates a _schema_metadata table from the JSON data dictionaries.

Usage:
    python ingest.py                 # full rebuild (default)
    python ingest.py --rebuild       # same as default
    python ingest.py --skip-existing # dev: skip Parquet conversion if file exists

See phase-1-plan.md for the full design rationale.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path

import duckdb
import yaml


# --- Constants --------------------------------------------------------------

CSV_PATTERN = re.compile(
    r"OP_(DTL_GNRL|DTL_RSRCH|DTL_OWNRSHP|REMOVED_DELETED)_PGYR(\d{4})_.*\.csv$",
    re.IGNORECASE,
)

# Matches the parquet filenames written by convert_csv_to_parquet(), e.g.
# `general_payments_2024.parquet`. Used by refresh_views() to rediscover
# files on a machine that did not itself run ingest (i.e. when the
# .duckdb file was copied from elsewhere).
PARQUET_PATTERN = re.compile(
    r"^(general_payments|research_payments|ownership_payments|removed_deleted)_(\d{4})\.parquet$"
)

GROUP_TO_TABLE = {
    "DTL_GNRL": "general_payments",
    "DTL_RSRCH": "research_payments",
    "DTL_OWNRSHP": "ownership_payments",
    "REMOVED_DELETED": "removed_deleted",
}

# Dictionary filename prefixes. NOTE the CMS typo: "Paymemnts" (not "Payments").
DICT_PREFIX_TO_TABLE = {
    "General_Paymemnts": "general_payments",
    "Research_Paymemnts": "research_payments",
    "Ownership_Paymemnts": "ownership_payments",
}

# Map dictionary 'type' field to the DuckDB type name we store in _schema_metadata.
# This is metadata for the agent's prompt builder — it does not drive Parquet dtypes
# (DuckDB's read_csv_auto handles that).
TYPE_MAP = {
    "string": "VARCHAR",
    "integer": "BIGINT",  # NPI and Record_ID can exceed 2^31 — never INT.
    "number": "DOUBLE",
    "date": "DATE",
}

# removed_deleted has no published data dictionary. Hardcode the 4 columns.
REMOVED_DELETED_META = [
    (
        "Change_Type",
        "VARCHAR",
        "Indicator showing the record was REMOVED or DELETED relative to the previous publication.",
        "REMOVED",
        None,
    ),
    (
        "Program_Year",
        "BIGINT",
        "Calendar year in which the payment was originally reported.",
        "2024",
        None,
    ),
    (
        "Payment_Type",
        "VARCHAR",
        "Category of the removed/deleted record: General, Research, or Ownership.",
        "General",
        None,
    ),
    (
        "Record_ID",
        "BIGINT",
        "System-generated unique identifier of the removed/deleted record.",
        "123456789",
        None,
    ),
]


# --- Helpers ----------------------------------------------------------------

def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def sql_path(p: Path) -> str:
    """Convert a Path to a forward-slash string safe for embedding in DuckDB SQL."""
    return str(p).replace("\\", "/")


def parquet_path_for(parquet_dir: Path, table_type: str, year: int) -> Path:
    return parquet_dir / f"{table_type}_{year}.parquet"


# --- CSV discovery ----------------------------------------------------------

def discover_csvs(source_dir: Path) -> list[tuple[str, int, Path]]:
    """Walk source_dir and return (table_type, year, csv_path) tuples, sorted."""
    found: list[tuple[str, int, Path]] = []
    for path in source_dir.rglob("*.csv"):
        m = CSV_PATTERN.search(path.name)
        if not m:
            continue
        table_type = GROUP_TO_TABLE[m.group(1).upper()]
        year = int(m.group(2))
        found.append((table_type, year, path))
    found.sort(key=lambda t: (t[0], t[1]))
    return found


# --- CSV -> Parquet ---------------------------------------------------------

def convert_csv_to_parquet(
    con: duckdb.DuckDBPyConnection,
    csv_path: Path,
    parquet_path: Path,
    compression: str,
    row_group_size: int,
    sample_size: int,
) -> tuple[int, float]:
    """Stream one CSV into a Parquet file. Returns (row_count, seconds)."""
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    csv_sql = sql_path(csv_path)
    pq_sql = sql_path(parquet_path)

    start = time.perf_counter()
    con.execute(
        f"""
        COPY (
            SELECT * FROM read_csv_auto(
                '{csv_sql}',
                all_varchar=false,
                sample_size={sample_size},
                ignore_errors=true
            )
        ) TO '{pq_sql}' (
            FORMAT PARQUET,
            COMPRESSION '{compression}',
            ROW_GROUP_SIZE {row_group_size}
        )
        """
    )
    elapsed = time.perf_counter() - start
    rows = con.execute(
        f"SELECT COUNT(*) FROM read_parquet('{pq_sql}')"
    ).fetchone()[0]
    return rows, elapsed


# --- DuckDB view registration -----------------------------------------------

def discover_parquets(parquet_dir: Path) -> list[tuple[str, int, Path]]:
    """Scan parquet_dir for files matching PARQUET_PATTERN.

    Mirrors discover_csvs() but operates on already-ingested parquet
    files. Used by refresh_views() to rebuild view DDL on a machine
    that received a copy of openpayments.duckdb without running ingest.
    """
    found: list[tuple[str, int, Path]] = []
    if not parquet_dir.exists():
        return found
    for path in sorted(parquet_dir.glob("*.parquet")):
        m = PARQUET_PATTERN.match(path.name)
        if not m:
            continue
        found.append((m.group(1), int(m.group(2)), path.resolve()))
    found.sort(key=lambda t: (t[0], t[1]))
    return found


def register_parquet_tables(
    con: duckdb.DuckDBPyConnection,
    manifest: list[tuple[str, int, Path]],
) -> dict[str, list[int]]:
    """Create per-year views + all_* UNION views. Returns {table_type: [years]}.

    all_* views use read_parquet([...], union_by_name=true) rather than
    SQL-level UNION ALL so column drift across program years (CMS has
    added/renamed columns between publications) does not blow up at
    query time.
    """
    by_type: dict[str, list[int]] = {}
    paths_by_type: dict[str, list[str]] = {}
    for table_type, year, pq_path in manifest:
        view_name = f"{table_type}_{year}"
        pq_sql = sql_path(pq_path)
        con.execute(
            f"""
            CREATE OR REPLACE VIEW {view_name} AS
                SELECT * FROM read_parquet('{pq_sql}')
            """
        )
        by_type.setdefault(table_type, []).append(year)
        paths_by_type.setdefault(table_type, []).append(pq_sql)

    for table_type, paths in paths_by_type.items():
        path_list = ", ".join(f"'{p}'" for p in sorted(paths))
        con.execute(
            f"""
            CREATE OR REPLACE VIEW all_{table_type} AS
                SELECT * FROM read_parquet([{path_list}], union_by_name=true)
            """
        )
    return by_type


def refresh_views(
    con: duckdb.DuckDBPyConnection,
    parquet_dir: Path,
) -> dict[str, list[int]]:
    """Re-register all per-year and all_* views against parquet_dir.

    Idempotent. Call this on app startup to repair a .duckdb file that
    was copied from another machine — CREATE VIEW bakes absolute paths
    into its SQL, so the views in a copied DB point at the original
    machine's filesystem and must be rewritten.

    Raises FileNotFoundError if no recognized parquet files are present.
    """
    manifest = discover_parquets(parquet_dir)
    if not manifest:
        raise FileNotFoundError(
            f"No recognized parquet files under {parquet_dir}. "
            "Run `python ingest.py` first."
        )
    return register_parquet_tables(con, manifest)


# --- Schema metadata --------------------------------------------------------

def load_dictionary(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("data", {}).get("fields", [])


def find_dictionary_file(dict_dir: Path, prefix: str) -> Path | None:
    """
    Return the first matching dictionary file, preferring the 2024 subdirectory
    (schemas are identical across years, so we only load one per type).
    """
    year_dirs = sorted(
        (d for d in dict_dir.iterdir() if d.is_dir()), reverse=True
    )
    preferred = dict_dir / "2024"
    search = [preferred] + [d for d in year_dirs if d != preferred]
    for d in search:
        if not d.exists():
            continue
        candidates = list(d.glob(f"{prefix}_DataDictionary_*.json"))
        if candidates:
            return candidates[0]
    return None


def build_schema_metadata(
    con: duckdb.DuckDBPyConnection,
    dict_dir: Path,
) -> int:
    """Create and populate _schema_metadata. Returns total row count inserted."""
    con.execute("DROP TABLE IF EXISTS _schema_metadata")
    con.execute(
        """
        CREATE TABLE _schema_metadata (
            table_type  VARCHAR,
            column_name VARCHAR,
            data_type   VARCHAR,
            description VARCHAR,
            example     VARCHAR,
            constraints VARCHAR
        )
        """
    )

    rows_inserted = 0
    for prefix, table_type in DICT_PREFIX_TO_TABLE.items():
        dict_file = find_dictionary_file(dict_dir, prefix)
        if dict_file is None:
            print(f"  WARNING: No dictionary found for prefix '{prefix}'")
            continue

        fields = load_dictionary(dict_file)
        for fld in fields:
            dtype = TYPE_MAP.get((fld.get("type") or "").lower(), "VARCHAR")
            example = fld.get("example")
            constraints = fld.get("constraints")
            con.execute(
                "INSERT INTO _schema_metadata VALUES (?, ?, ?, ?, ?, ?)",
                [
                    table_type,
                    fld.get("name"),
                    dtype,
                    fld.get("description"),
                    None if example is None else str(example),
                    None if not constraints else json.dumps(constraints),
                ],
            )
            rows_inserted += 1
        print(f"  Loaded {len(fields):>3} fields for {table_type:<19} from {dict_file.name}")

    for name, dtype, desc, example, constraints in REMOVED_DELETED_META:
        con.execute(
            "INSERT INTO _schema_metadata VALUES (?, ?, ?, ?, ?, ?)",
            ["removed_deleted", name, dtype, desc, example, constraints],
        )
        rows_inserted += 1
    print(f"  Loaded {len(REMOVED_DELETED_META):>3} hardcoded fields for removed_deleted")

    return rows_inserted


# --- Main -------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Open Payments ingestion pipeline: CSV -> Parquet -> DuckDB"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--rebuild",
        action="store_true",
        help="Wipe Parquet + DuckDB and re-ingest everything (default).",
    )
    mode.add_argument(
        "--skip-existing",
        action="store_true",
        help="Dev: skip Parquet conversion for files that already exist.",
    )
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    # Default mode: rebuild (unless --skip-existing is explicitly set).
    rebuild = args.rebuild or not args.skip_existing

    cfg = load_config(args.config)
    source_dir = Path(cfg["data"]["source_dir"]).resolve()
    parquet_dir = Path(cfg["data"]["parquet_dir"]).resolve()
    duckdb_path = Path(cfg["data"]["duckdb_path"]).resolve()
    dict_dir = Path(cfg["data"]["dictionaries_dir"]).resolve()

    compression = cfg["ingestion"]["compression"]
    row_group_size = cfg["ingestion"]["row_group_size"]
    sample_size = cfg["ingestion"]["sample_size"]

    print("=" * 72)
    print("Open Payments Data Ingestion Pipeline")
    print("=" * 72)
    print(f"Source CSVs:      {source_dir}")
    print(f"Parquet output:   {parquet_dir}")
    print(f"DuckDB file:      {duckdb_path}")
    print(f"Dictionaries:     {dict_dir}")
    print(f"Mode:             {'REBUILD (wipe + re-ingest)' if rebuild else 'SKIP-EXISTING (dev)'}")
    print()

    if rebuild:
        if parquet_dir.exists():
            print(f"Wiping {parquet_dir} ...")
            shutil.rmtree(parquet_dir)
        if duckdb_path.exists():
            print(f"Wiping {duckdb_path} ...")
            duckdb_path.unlink()
    parquet_dir.mkdir(parents=True, exist_ok=True)
    duckdb_path.parent.mkdir(parents=True, exist_ok=True)

    # --- Discover -----------------------------------------------------------
    print("Discovering CSVs ...")
    csv_files = discover_csvs(source_dir)
    if not csv_files:
        print(f"ERROR: No matching CSVs found under {source_dir}", file=sys.stderr)
        return 1
    for table_type, year, path in csv_files:
        size = path.stat().st_size
        print(f"  [{table_type:<19}] {year}  {human_bytes(size):>10}  {path.name}")
    print(f"Found {len(csv_files)} CSVs.")
    print()

    # --- CSV -> Parquet -----------------------------------------------------
    # Use a throwaway :memory: connection for the conversion so the persistent
    # DuckDB file stays tiny (only views + metadata).
    conv_con = duckdb.connect(":memory:")

    manifest: list[tuple[str, int, Path]] = []
    total_rows = 0
    total_bytes = 0
    total_time = 0.0

    print("Converting CSV -> Parquet ...")
    for idx, (table_type, year, csv_path) in enumerate(csv_files, 1):
        pq_path = parquet_path_for(parquet_dir, table_type, year)

        if args.skip_existing and pq_path.exists():
            rows = conv_con.execute(
                f"SELECT COUNT(*) FROM read_parquet('{sql_path(pq_path)}')"
            ).fetchone()[0]
            size = pq_path.stat().st_size
            print(
                f"  [{idx:>2}/{len(csv_files)}] SKIP  {pq_path.name}  "
                f"({rows:,} rows, {human_bytes(size)})"
            )
            manifest.append((table_type, year, pq_path))
            total_rows += rows
            total_bytes += size
            continue

        print(
            f"  [{idx:>2}/{len(csv_files)}] {csv_path.name} -> {pq_path.name} ...",
            flush=True,
        )
        try:
            rows, elapsed = convert_csv_to_parquet(
                conv_con,
                csv_path,
                pq_path,
                compression,
                row_group_size,
                sample_size,
            )
        except Exception as e:
            print(f"    FAILED: {e}", file=sys.stderr)
            return 2

        size = pq_path.stat().st_size
        manifest.append((table_type, year, pq_path))
        total_rows += rows
        total_bytes += size
        total_time += elapsed
        print(
            f"           done: {rows:>12,} rows   "
            f"{human_bytes(size):>10}   {elapsed:6.1f}s",
            flush=True,
        )

    conv_con.close()
    print()

    # --- Persistent DuckDB: views + metadata --------------------------------
    print(f"Opening persistent DuckDB: {duckdb_path}")
    con = duckdb.connect(str(duckdb_path))
    try:
        print("Registering per-year views + UNION views ...")
        by_type = register_parquet_tables(con, manifest)
        for table_type in sorted(by_type.keys()):
            print(f"  all_{table_type}  <-  years {sorted(by_type[table_type])}")

        print("Building _schema_metadata from data dictionaries ...")
        meta_rows = build_schema_metadata(con, dict_dir)
        print(f"  _schema_metadata: {meta_rows} rows total")

        print()
        print("Row counts (from UNION views):")
        for table_type in sorted(by_type.keys()):
            view = f"all_{table_type}"
            n = con.execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0]
            print(f"  {view:<28} {n:>15,}")
    finally:
        con.close()

    # --- Summary ------------------------------------------------------------
    print()
    print("=" * 72)
    print("Ingestion complete.")
    print(f"  Parquet files:     {len(manifest)}")
    print(f"  Total rows:        {total_rows:,}")
    print(f"  Total Parquet:     {human_bytes(total_bytes)}")
    if total_time > 0:
        print(f"  Conversion time:   {total_time:.1f}s ({total_time / 60:.1f} min)")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
