"""
Launcher for the Open Payments Data Analyst Chainlit app.

Use this instead of `chainlit run app.py` directly. It exists to work
around a Python 3.14 + nest_asyncio + chainlit incompatibility:

  chainlit/cli/__init__.py line 11 unconditionally calls
  ``nest_asyncio.apply()``. On Python 3.14 that monkey-patch breaks
  ``asyncio.current_task()`` (it returns None inside running tasks),
  which cascades through sniffio → anyio → starlette FileResponse
  and 500-errors every static frontend asset (favicon, logo, JS/CSS
  bundles). The browser sees a blank page.

  ``nest_asyncio`` 1.6.0 is the latest release and the project is
  effectively unmaintained, so there is no upstream fix. Neither
  chainlit nor this app actually need nested-loop support, so the
  cheapest correct workaround is to NOT call ``apply()`` at all.

  We pre-import ``nest_asyncio`` and replace ``apply`` with a no-op
  before ``chainlit.cli`` is imported. Once chainlit's CLI module
  loads, its top-level ``nest_asyncio.apply()`` becomes a no-op and
  asyncio behaves normally for the lifetime of the process.

Usage:
    python run.py [chainlit-run-flags...]

Examples:
    python run.py
    python run.py --port 9000
    python run.py --headless --port 8765
"""
from __future__ import annotations

import sys
from pathlib import Path

# MUST happen before any chainlit import.
import nest_asyncio  # noqa: E402

nest_asyncio.apply = lambda: None  # type: ignore[assignment]

# Safe to import chainlit now.
from chainlit.cli import cli  # noqa: E402


# R3.3 — Live record counts in the Readme dialog.
#
# We render chainlit.md.template → chainlit.md on each startup with real
# COUNT(*) values queried against the local DuckDB database. The placeholders
# `{{GENERAL_COUNT}}` etc. are replaced with formatted numbers; if the query
# fails (DuckDB missing, views missing, whatever), we fall back to human-
# readable "—" so the rest of the readme still renders cleanly.
#
# chainlit.md is gitignored — chainlit.md.template is the source of truth.
def _render_chainlit_md() -> None:
    root = Path(__file__).resolve().parent
    template_path = root / "chainlit.md.template"
    output_path = root / "chainlit.md"

    if not template_path.exists():
        return  # nothing to render; keep whatever chainlit.md exists

    template = template_path.read_text(encoding="utf-8")

    placeholders = {
        "{{GENERAL_COUNT}}": "—",
        "{{RESEARCH_COUNT}}": "—",
        "{{OWNERSHIP_COUNT}}": "—",
        "{{REMOVED_COUNT}}": "—",
        "{{TOTAL_COUNT}}": "—",
    }

    try:
        import yaml
        import duckdb

        cfg = yaml.safe_load((root / "config.yaml").read_text(encoding="utf-8"))
        db_path = Path(cfg["data"]["duckdb_path"]).resolve()
        if db_path.exists():
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                counts = {}
                for key, view in [
                    ("GENERAL_COUNT", "all_general_payments"),
                    ("RESEARCH_COUNT", "all_research_payments"),
                    ("OWNERSHIP_COUNT", "all_ownership_payments"),
                    ("REMOVED_COUNT", "all_removed_deleted"),
                ]:
                    n = con.execute(f"SELECT COUNT(*) FROM {view}").fetchone()[0]
                    counts[key] = int(n)
                    placeholders["{{" + key + "}}"] = f"{int(n):,}"
                placeholders["{{TOTAL_COUNT}}"] = f"{sum(counts.values()):,}"
            finally:
                con.close()
    except Exception as e:  # noqa: BLE001 — readme render must never block startup
        print(f"[run.py] chainlit.md live-count render skipped: {e}", file=sys.stderr)

    rendered = template
    for token, value in placeholders.items():
        rendered = rendered.replace(token, value)
    output_path.write_text(rendered, encoding="utf-8")


def main() -> None:
    _render_chainlit_md()
    extra_args = sys.argv[1:]
    sys.argv = ["chainlit", "run", "app.py", *extra_args]
    cli()


if __name__ == "__main__":
    main()
