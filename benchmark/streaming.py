"""Streaming-boundary benchmark: the SAME fixtures, through the REAL accumulator.

Why this exists
---------------

``run_benchmark.py`` feeds each fixture to the post-processing chain as one
complete string. Realtime dictation never does that: Speechmatics finalizes on
its own timing, so a fixture arrives as several *final segments*, and the
production path is

    normalize_text -> FinalStreamCanonicalizer.add() (x N) -> .flush()

with ``MedicalLayer.canonicalize`` running inside the accumulator. A
whole-text benchmark can therefore report a perfect score while every
construct that spans a final-segment boundary is broken in the field - which
is exactly the class of bug this mode is here to catch.

This module does NOT reimplement the pipeline. It imports
``app.FinalStreamCanonicalizer`` and ``MedicalLayer`` - the very objects
``app.main()`` uses - splits each fixture into synthetic final segments, and
compares the accumulated canonical text with the fixture's expected output.

Both benchmarks measure DETERMINISTIC POST-PROCESSING. Neither measures
Speechmatics recognition accuracy: there is no audio involved.

Split strategies
----------------

Deterministic, derived from the fixture's own token count:

``whole``        one segment (control: must equal the whole-text result)
``first_token``  after token 1
``last_token``   before the final token
``midpoint``     at the token midpoint
``every_two``    a boundary after every second token (worst case)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.dataset import ALL_CASES, Case  # noqa: E402
from speechmatics_test.text import normalize_text  # noqa: E402

#: Split strategy name -> boundary token indices, in reading order.
SPLIT_STRATEGIES = ("whole", "first_token", "last_token", "midpoint", "every_two")


def split_points(token_count: int, strategy: str) -> tuple[int, ...]:
    """Boundary indices for ``strategy`` (empty tuple = one segment)."""
    if token_count < 2 or strategy == "whole":
        return ()
    if strategy == "first_token":
        return (1,)
    if strategy == "last_token":
        return (token_count - 1,)
    if strategy == "midpoint":
        return (token_count // 2,)
    if strategy == "every_two":
        return tuple(range(2, token_count, 2))
    raise ValueError(f"unknown split strategy: {strategy!r}")


def segments(spoken: str, strategy: str) -> list[str]:
    """Split a fixture into synthetic Speechmatics final segments."""
    tokens = normalize_text(spoken).split()
    pieces: list[str] = []
    previous = 0
    for boundary in (*split_points(len(tokens), strategy), len(tokens)):
        piece = " ".join(tokens[previous:boundary])
        previous = boundary
        if piece:
            pieces.append(piece)
    return pieces


def stream_case(accumulator_factory, spoken: str, strategy: str) -> str:
    """Feed one fixture through a fresh production accumulator."""
    accumulator = accumulator_factory()
    for segment in segments(spoken, strategy):
        accumulator.add(segment, [])
    accumulator.flush()
    return accumulator.canonical_text


def _matches(produced: str, expected: str) -> bool:
    """Same terminal-period tolerance as the whole-text benchmark.

    ``polish_document``'s sentence-final period is only added to a complete
    document; the streaming path emits segments, so it never adds one.
    """
    return produced.rstrip(".") == expected.rstrip(".")


def evaluate_streaming(
    accumulator_factory, cases: Iterable[Case] = ALL_CASES
) -> dict:
    """Run every fixture under every split strategy.

    ``accumulator_factory`` returns a fresh ``FinalStreamCanonicalizer``;
    injecting it keeps this module free of app-startup concerns and lets the
    tests drive it with a matcher-less accumulator.
    """
    results: list[dict] = []
    for case in cases:
        for strategy in SPLIT_STRATEGIES:
            produced = stream_case(accumulator_factory, case.spoken, strategy)
            results.append({
                "id": case.id,
                "stage": case.stage,
                "group": case.group,
                "strategy": strategy,
                "segments": segments(case.spoken, strategy),
                "expected": case.expected,
                "produced": produced,
                "exact_match": _matches(produced, case.expected),
            })

    by_strategy: dict[str, dict] = {}
    for strategy in SPLIT_STRATEGIES:
        subset = [r for r in results if r["strategy"] == strategy]
        exact = sum(1 for r in subset if r["exact_match"])
        by_strategy[strategy] = {
            "runs": len(subset),
            "exact_match": exact,
            "accuracy": round(exact / len(subset), 4) if subset else None,
            "failures": [r["id"] for r in subset if not r["exact_match"]],
        }

    total = len(results)
    exact = sum(1 for r in results if r["exact_match"])
    return {
        "runs": results,
        "aggregates": {
            "total_runs": total,
            "exact_match": exact,
            "exact_match_accuracy": round(exact / total, 4) if total else None,
            "strategies": list(SPLIT_STRATEGIES),
            "by_strategy": by_strategy,
            "failures": sorted({
                f"{r['id']}:{r['strategy']}"
                for r in results if not r["exact_match"]
            }),
        },
    }


def production_accumulator_factory(polish: bool = True):
    """Factory for the REAL production accumulator (app + MedicalLayer).

    The medical layer is built once and shared: it is stateless per call
    apart from its warning lists, and rebuilding it per fixture would make
    the benchmark dominated by dictionary compilation.
    """
    from app import FinalStreamCanonicalizer
    from speechmatics_test.medical_layer import MedicalLayer

    medical = MedicalLayer(ROOT, polish=polish)
    return lambda: FinalStreamCanonicalizer(medical)
