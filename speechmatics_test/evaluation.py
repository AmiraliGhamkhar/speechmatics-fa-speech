"""Benchmark metrics for mixed Persian/English medical transcripts.

Compares a reference (expected) transcript against hypotheses using
medical-aware tokenization. ``evaluate_stages`` runs the same metrics for
each pipeline stage so RAW ASR vs NORMALIZED vs FST CANONICAL can be
compared side by side.
"""

from __future__ import annotations

from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Optional

from .text import normalize_text, tokens


def edit_distance(a: list, b: list) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def wer(reference: str, hypothesis: str) -> float:
    """Word error rate over medical-aware tokens (case-sensitive)."""
    r = tokens(reference)
    h = tokens(hypothesis)
    return edit_distance(r, h) / max(1, len(r))


def numbers(text: str) -> list[str]:
    """Numeric tokens only: values, decimals, ratios (20 | 5.5 | 3,14 | 120/80).

    Alphanumeric codes (C3-C4, q2h, O2) are tokens but not *numbers*.
    """
    return [t for t in tokens(text) if t and t[0].isdigit()]


def number_accuracy(reference: str, hypothesis: str) -> Optional[float]:
    """Fraction of reference numeric tokens present in the hypothesis
    (multiset-aware, so repeated values are not over-counted)."""
    nums_r = numbers(reference)
    if not nums_r:
        return None
    remaining = Counter(numbers(hypothesis))
    matched = 0
    for x in nums_r:
        if remaining[x] > 0:
            matched += 1
            remaining[x] -= 1
    return matched / len(nums_r)


def evaluate(reference: str, hypothesis: str) -> dict[str, Any]:
    return {
        "wer": round(wer(reference, hypothesis), 4),
        "number_accuracy": number_accuracy(reference, hypothesis),
        "similarity": round(
            SequenceMatcher(
                None, normalize_text(reference), normalize_text(hypothesis)
            ).ratio(),
            4,
        ),
    }


def evaluate_stages(
    reference: str, stages: dict[str, str]
) -> dict[str, dict[str, Any]]:
    """Evaluate several pipeline stages (e.g. raw/normalized/canonical)
    against the same reference transcript."""
    return {name: evaluate(reference, text) for name, text in stages.items()}
