"""Compatibility wrapper -> scripts/swiftmedics_tools.py export-vocab.

The vocabulary export is implemented ONCE, in the consolidated tool. This
shim keeps the historical entry point (and its ``--check`` flag) working
for existing docs, CI and muscle memory.

Usage:
    python scripts/export_additional_vocab.py           # regenerate + report
    python scripts/export_additional_vocab.py --check   # verify sync only
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.swiftmedics_tools import main  # noqa: E402


if __name__ == "__main__":
    argv = sys.argv[1:]
    raise SystemExit(main(["check-vocab"] if "--check" in argv
                          else ["export-vocab"]))
