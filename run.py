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

# MUST happen before any chainlit import.
import nest_asyncio  # noqa: E402

nest_asyncio.apply = lambda: None  # type: ignore[assignment]

# Safe to import chainlit now.
from chainlit.cli import cli  # noqa: E402


def main() -> None:
    extra_args = sys.argv[1:]
    sys.argv = ["chainlit", "run", "app.py", *extra_args]
    cli()


if __name__ == "__main__":
    main()
