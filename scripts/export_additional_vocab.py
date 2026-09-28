#!/usr/bin/env python3
"""Generate or validate the derived Speechmatics vocabulary artifact.

``medical_knowledge/medical_dictionary.json`` is the single source of truth.
The Speechmatics ``additional_vocab`` payload is derived from its
``speechmatics: true`` entries by the same code the application uses at
runtime (``MedicalMatcher.additional_vocab``), so the committed artifact can
never drift from what is actually sent to the engine.

Usage:

    python scripts/export_additional_vocab.py            # write the artifact
    python scripts/export_additional_vocab.py --check    # verify, no writes
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from speechmatics_test.matcher import MedicalMatcher  # noqa: E402

ARTIFACT = ROOT / "medical_knowledge" / "speechmatics_additional_vocab.json"


def render(vocab: list) -> str:
    """Serialize deterministically, exactly as the artifact is stored."""
    return json.dumps(vocab, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true",
        help="verify the committed artifact instead of rewriting it",
    )
    args = parser.parse_args()

    matcher = MedicalMatcher(ROOT)
    vocab = matcher.additional_vocab
    payload = render(vocab)
    print(
        f"{len(matcher.rules)} dictionary rules, "
        f"{len(matcher.terms)} terms, "
        f"{len(vocab)} speechmatics-eligible vocab entries, "
        f"{len(matcher.warnings)} loader warning(s)"
    )
    # The loader emits a few hundred deterministic warnings (tier conflicts,
    # punctuation-bearing forms); they are only worth printing when a human
    # is regenerating the artifact.
    if not args.check:
        for warning in matcher.warnings:
            print(f"warning: {warning}")

    if args.check:
        if not ARTIFACT.exists():
            print(f"CHECK: FAILED - {ARTIFACT} is missing")
            return 1
        current = ARTIFACT.read_text(encoding="utf-8")
        if current != payload:
            print(f"CHECK: FAILED - {ARTIFACT} is out of sync with the dictionary")
            print("run: python scripts/export_additional_vocab.py")
            return 1
        print(f"CHECK: OK - {ARTIFACT.name} is in sync")
        return 0

    ARTIFACT.write_text(payload, encoding="utf-8")
    print(f"wrote {ARTIFACT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
