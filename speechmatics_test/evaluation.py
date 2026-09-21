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


def _number_match_count(reference: str, hypothesis: str) -> tuple[int, int, int]:
    """Multiset-aware overlap between reference and hypothesis numeric
    tokens: ``(matched, count(reference), count(hypothesis))``."""
    nums_r = numbers(reference)
    nums_h = numbers(hypothesis)
    remaining = Counter(nums_h)
    matched = 0
    for x in nums_r:
        if remaining[x] > 0:
            matched += 1
            remaining[x] -= 1
    return matched, len(nums_r), len(nums_h)


def number_accuracy(reference: str, hypothesis: str) -> Optional[float]:
    """Fraction of reference numeric tokens present in the hypothesis
    (multiset-aware, so repeated values are not over-counted).

    This is RECALL-only (kept exactly as-is for API stability: existing
    callers/reports rely on this precise definition and value). It does
    NOT penalize extra/hallucinated numbers in the hypothesis - e.g.
    reference "20 mg" vs hypothesis "20 mg 50 mg" scores a perfect 1.0
    here, even though "50 mg" was fabricated. Use ``number_precision``/
    ``number_f1`` (or ``evaluate``'s additive fields) to also catch that
    case.
    """
    matched, total_r, _ = _number_match_count(reference, hypothesis)
    if not total_r:
        return None
    return matched / total_r


def number_precision(reference: str, hypothesis: str) -> Optional[float]:
    """Fraction of HYPOTHESIS numeric tokens that also appear in the
    reference (multiset-aware). ``None`` when the hypothesis has no numeric
    tokens at all (nothing to score for precision, symmetric with
    ``number_accuracy``'s ``None`` when the reference has none).

    This is what flags a fabricated/extra number: reference "20 mg" vs
    hypothesis "20 mg 50 mg" has recall 1.0 (every reference number is
    present) but precision 0.5 (only half the hypothesis numbers are
    correct) - the extra "50 mg" is caught here.
    """
    matched, _, total_h = _number_match_count(reference, hypothesis)
    if not total_h:
        return None
    return matched / total_h


def number_recall(reference: str, hypothesis: str) -> Optional[float]:
    """Alias for ``number_accuracy`` under its more precise metric name."""
    return number_accuracy(reference, hypothesis)


def number_f1(reference: str, hypothesis: str) -> Optional[float]:
    """Harmonic mean of ``number_precision`` and ``number_recall``.

    ``None`` when both the reference and hypothesis have no numeric tokens
    (nothing to score); ``0.0`` when only one side has numbers and none of
    them overlap (a genuine total miss, not "nothing to compare").
    """
    precision = number_precision(reference, hypothesis)
    recall = number_recall(reference, hypothesis)
    if precision is None and recall is None:
        return None
    precision = precision or 0.0
    recall = recall or 0.0
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)


def evaluate(reference: str, hypothesis: str) -> dict[str, Any]:
    precision = number_precision(reference, hypothesis)
    recall = number_recall(reference, hypothesis)
    f1 = number_f1(reference, hypothesis)
    return {
        "wer": round(wer(reference, hypothesis), 4),
        # Kept exactly as-is (recall-only) for API/report stability.
        "number_accuracy": number_accuracy(reference, hypothesis),
        # Additive fields: also catch fabricated/extra numbers that
        # "number_accuracy" alone cannot (see its docstring).
        "number_precision": round(precision, 4) if precision is not None else None,
        "number_recall": round(recall, 4) if recall is not None else None,
        "number_f1": round(f1, 4) if f1 is not None else None,
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
