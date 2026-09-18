"""Regenerate the Speechmatics additional_vocab artifact from the dictionary.

The bounded ``additional_vocab`` is derived at runtime from the
``speechmatics: true`` entries of ``medical_knowledge/medical_dictionary.json``
(the single source of truth). After editing the dictionary, run this script
to regenerate the committed mirror
``medical_knowledge/speechmatics_additional_vocab.json`` (a diffable record
of exactly what is sent to Speechmatics). ``--check`` verifies the artifact
is still in sync without writing anything; the test suite runs it as a
parity guard.

Usage:
    python scripts/export_additional_vocab.py           # regenerate + report
    python scripts/export_additional_vocab.py --check   # verify sync only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from speechmatics_test.matcher import MedicalMatcher  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
VOCAB_PATH = ROOT / "medical_knowledge" / "speechmatics_additional_vocab.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="verify the committed artifact is in sync; "
                             "write nothing")
    args = parser.parse_args()

    # Derive through the exact runtime path (validated dictionary -> matcher)
    # so the artifact can never disagree with what the app sends.
    matcher = MedicalMatcher(ROOT)
    vocab = matcher.additional_vocab
    print(f"dictionary terms          : {len(matcher.terms)}")
    print(f"speechmatics-eligible vocab: {len(vocab)} (speechmatics: true only)")

    if args.check:
        current = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
        if current == vocab:
            print("CHECK: OK (speechmatics_additional_vocab.json is in sync)")
            return 0
        print("CHECK: FAILED (artifact differs from the derived vocabulary; "
              "run scripts/export_additional_vocab.py to regenerate)")
        return 1

    VOCAB_PATH.write_text(
        json.dumps(vocab, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote: {VOCAB_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
