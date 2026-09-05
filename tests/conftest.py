"""
Test bootstrap.

The package can be present twice: once at `src/mcp_logseq_db` and once as a
pip-installed copy from `pip install .`. Which one wins has depended on how
pytest was invoked -- `pytest` and `python -m pytest` put different things on
`sys.path` -- so a test run could silently exercise an old installed build.

This file removes the ambiguity: `src` goes to the front of `sys.path`, and the
resolved package file is reported so a surprising result can be traced to the
copy that produced it.

Run `pytest -q -s` to see the banner, or `pytest --collect-only` to check
collection without executing anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
else:
    # Present but possibly not first. Force it to the front.
    sys.path.remove(str(SRC))
    sys.path.insert(0, str(SRC))


def pytest_report_header(config) -> list[str]:
    """Show which copy of the package the run actually imported."""
    try:
        import mcp_logseq_db
    except Exception as error:  # noqa: BLE001 -- reported, not raised
        return [f"mcp_logseq_db: IMPORT FAILED ({error})"]

    location = getattr(mcp_logseq_db, "__file__", "unknown")
    expected = str(SRC)
    marker = "src" if location.startswith(expected) else "INSTALLED COPY"
    return [
        f"mcp_logseq_db: {location}  [{marker}]",
        f"python: {sys.version.split()[0]}",
    ]
