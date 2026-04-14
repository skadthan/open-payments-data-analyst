"""Diagnose why DocumentRAG.is_available() returns False.

Run from the project root:
    python diagnose_rag.py

Prints the resolved vectorstore path, directory contents, chromadb version,
and the full traceback of any failure opening the collection.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

import yaml

from rag import DocumentRAG


def main() -> int:
    cfg = yaml.safe_load(open("config.yaml"))
    rag = DocumentRAG(cfg)

    store_dir = rag._store_dir.resolve()
    print(f"store_dir = {store_dir}")
    print(f"exists    = {store_dir.exists()}")
    if store_dir.exists():
        contents = sorted(p.name for p in store_dir.iterdir())
        print(f"contents  = {contents}")

    try:
        import chromadb
        print(f"chromadb  = {chromadb.__version__}")
    except Exception:
        traceback.print_exc()

    try:
        import sqlite3
        print(f"sqlite3   = {sqlite3.sqlite_version}")
    except Exception:
        traceback.print_exc()

    print()
    try:
        col = rag._get_collection()
        print(f"collection opened OK — count = {col.count()}")
    except Exception:
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
