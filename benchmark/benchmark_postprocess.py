"""Post-processing benchmark: what the pipeline does to a FIXED transcript.

``benchmark_asr.py`` measures recognition quality (RAW WER against a reference)
and deliberately stops there - post-processing cannot fix a misheard word, and
scoring it as if it could would hide ASR errors.

This script measures the OTHER half, which ``benchmark_asr.py`` never exercises:
given an ASR transcript that is already correct, does the deterministic layer
preserve it?  Every case here is a transcript the engine got RIGHT, so any error
this benchmark reports was introduced by our own code - an invented number, a
destroyed medical term, a corrupted vital sign.

The metric functions are imported from ``benchmark_asr`` so both benchmarks
score identically.  Both pipeline paths are measured:

    single    - MedicalLayer.canonicalize() on the whole transcript
    streamed  - the same text delivered as separate Speechmatics finals
                through app.FinalStreamCanonicalizer (what is actually pasted)

Reported metrics are deliberately SEPARATED by pipeline stage, because a raw
ASR metric and a post-processing metric answer different questions:

    A raw        - the stored ASR segments verbatim (what Speechmatics said)
    B normalized - generic normalization only (script/digits/spacing)
    C medical    - the dictionary pass ALONE, numeric fold disabled
    D numeric    - the number/clock/ratio fold ALONE, dictionary disabled
    E canonical  - the deterministic medical layer, single pass (C then D)
    E' injected  - the streamed canonical text, i.e. what the clinician pastes
                 (end-to-end for the post-processing half of the pipeline)

C and D are measured separately because the merged stage cannot say which half
broke: a destroyed term and an invented dose look identical in E. Splitting
them is what makes a regression self-diagnosing.

Each stage reports WER, exact match, and the unambiguous numeric metrics
``number_precision`` / ``number_recall`` / ``number_f1``.  ``number_accuracy``
is NOT reported as an accuracy number: it is recall-only and cannot see a
fabricated value (see ``speechmatics_test/evaluation.py``).

Usage:
    python benchmark/benchmark_postprocess.py
    python benchmark/benchmark_postprocess.py --out benchmark/postprocess.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from benchmark.benchmark_asr import extract_entities, word_error_rate  # noqa: E402
from speechmatics_test.evaluation import (  # noqa: E402
    number_f1,
    number_precision,
    number_recall,
)
from speechmatics_test.matcher import NUMERIC_CONTEXT  # noqa: E402
from speechmatics_test.medical_layer import MedicalLayer  # noqa: E402
from speechmatics_test.text import (  # noqa: E402
    fold_numeric_expressions,
    normalize_text,
)

CASES_PATH = Path(__file__).resolve().parent / "postprocess_cases.json"

#: Reported in pipeline order: A raw, B normalized, C medical only, D numeric
#: only, E end to end (single pass) and E' end to end through the stream.
STAGE_LABELS = (
    "raw",
    "normalized",
    "medical_canonicalization_only",
    "numeric_fold_only",
    "canonical_single_pass",
    "canonical_streamed",
)


def _load_cases(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def _single_pass(layer: MedicalLayer, segments: list[str]) -> str:
    text = normalize_text(" ".join(segments))
    return layer.canonicalize(text, preserve_narrative=True)[0]


def _lexical_only(layer: MedicalLayer, segments: list[str]) -> str:
    """Stage C: the dictionary pass with the numeric fold DISABLED.

    ``MedicalMatcher.canonicalize`` is the lexical scan followed by
    ``fold_numeric_expressions``; running only the scan is what separates a
    destroyed medical TERM from an invented NUMBER, which the merged
    end-to-end stage cannot tell apart. Uses the same private entry point the
    test suite uses for this split (``tests/test_matcher.py`` calls ``_scan``
    directly for the same reason), and the same narrative policy as the live
    single-pass stage.
    """
    text = normalize_text(" ".join(segments))
    return layer.fst._scan(text, None, preserve_narrative=True)[0]


def _numeric_only(layer: MedicalLayer, segments: list[str]) -> str:
    """Stage D: the numeric fold with the dictionary pass DISABLED.

    The mirror image of stage C, so a case that scores badly here and well in
    stage C is a number-pipeline defect (a fabricated dose, a half-converted
    group) and not a dictionary one.
    """
    text = normalize_text(" ".join(segments))
    return fold_numeric_expressions(text, NUMERIC_CONTEXT)


def _streamed(layer: MedicalLayer, segments: list[str]) -> str:
    import app

    accumulator = app.FinalStreamCanonicalizer(layer)
    for segment in segments:
        accumulator.add(normalize_text(segment), [])
    accumulator.flush()
    return accumulator.canonical_text


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _score(cases: list[dict], produced: list[str]) -> dict:
    errors = reference_tokens = 0
    correct = detected = expected_total = 0
    exact = 0
    precisions: list[float] = []
    recalls: list[float] = []
    f1s: list[float] = []
    for case, hypothesis in zip(cases, produced):
        expected = case["expected"]
        measured = word_error_rate(expected, hypothesis)
        errors += int(measured["errors"])
        reference_tokens += int(measured["reference_tokens"])
        exact += int(hypothesis == expected)

        precision = number_precision(expected, hypothesis)
        recall = number_recall(expected, hypothesis)
        f1 = number_f1(expected, hypothesis)
        if precision is not None:
            precisions.append(precision)
        if recall is not None:
            recalls.append(recall)
        if f1 is not None:
            f1s.append(f1)

        expected_entities = extract_entities(expected)
        detected_entities = extract_entities(hypothesis)
        expected_total += len(expected_entities)
        detected += len(detected_entities)
        correct += sum(
            1 for key, value in detected_entities.items()
            if expected_entities.get(key) == value
        )
    return {
        "case_count": len(cases),
        "exact_match": round(exact / len(cases), 6) if cases else None,
        "wer": round(errors / reference_tokens, 6) if reference_tokens else None,
        "errors": errors,
        "reference_tokens": reference_tokens,
        # Macro-averaged over the cases that have numbers on the relevant
        # side. A fabricated value lowers precision, a dropped value lowers
        # recall; both must be read together (F1) to claim numeric quality.
        "number_precision": _mean(precisions),
        "number_recall": _mean(recalls),
        "number_f1": _mean(f1s),
        "entity_precision": round(correct / detected, 6) if detected else None,
        "entity_recall": round(correct / expected_total, 6) if expected_total else None,
        "entity_correct": correct,
        "entity_detected": detected,
        "entity_expected": expected_total,
    }


def run(cases: list[dict]) -> dict:
    layer = MedicalLayer(ROOT)
    single = [_single_pass(layer, case["segments"]) for case in cases]
    streamed = [_streamed(layer, case["segments"]) for case in cases]
    # Stage metrics: the stored ASR text as delivered, and after generic
    # normalization only. Both are computed on the same case subset as the
    # single-pass score (a ``streaming_only`` case has no meaningful joined
    # form, so it is excluded there and reported on the streamed path only).
    raw = [" ".join(case["segments"]) for case in cases]
    normalized = [normalize_text(text) for text in raw]
    lexical = [_lexical_only(layer, case["segments"]) for case in cases]
    numeric = [_numeric_only(layer, case["segments"]) for case in cases]
    # A "streaming_only" case reproduces a final boundary INSIDE a written
    # value ("ساعت 10:" + "30"). Joining its segments with a space is not a
    # transcript the app ever produces, so it is scored on the streamed path
    # only rather than counted as a single-pass defect.
    scored = [
        (case, single_text)
        for case, single_text in zip(cases, single)
        if not case.get("streaming_only")
    ]
    scored_ids = {id(case) for case, _ in scored}
    stage_subset = [
        (case, text) for case, text in zip(cases, raw) if id(case) in scored_ids
    ]
    lexical_subset = [
        (case, text) for case, text in zip(cases, lexical) if id(case) in scored_ids
    ]
    numeric_subset = [
        (case, text) for case, text in zip(cases, numeric) if id(case) in scored_ids
    ]
    report = {
        "case_count": len(cases),
        "single_pass": _score([c for c, _ in scored], [t for _, t in scored]),
        "streamed": _score(cases, streamed),
        # The five separated stages this benchmark exists to expose, in
        # pipeline order. A and B carry no medical knowledge at all; C and D
        # are the two halves of the deterministic layer measured ALONE, so a
        # regression names its own cause instead of hiding inside E; E is what
        # the clinician actually receives.
        #   A raw        -> how good the stored ASR output is as delivered
        #   B normalized -> generic normalization only (script/digits/spacing)
        #   C medical    -> the dictionary pass, numeric fold disabled
        #   D numeric    -> the number/clock/ratio fold, dictionary disabled
        #   E canonical  -> both, over the whole transcript (single pass)
        #   E' streamed  -> both, through the realtime final boundaries, which
        #                   is the text actually injected
        "stages": {
            "raw": _score([c for c, _ in stage_subset], [
                text for _, text in stage_subset
            ]),
            "normalized": _score(
                [c for c, _ in stage_subset],
                [normalize_text(text) for _, text in stage_subset],
            ),
            "medical_canonicalization_only": _score(
                [c for c, _ in lexical_subset],
                [text for _, text in lexical_subset],
            ),
            "numeric_fold_only": _score(
                [c for c, _ in numeric_subset],
                [text for _, text in numeric_subset],
            ),
            "canonical_single_pass": _score(
                [c for c, _ in scored], [t for _, t in scored]
            ),
            "canonical_streamed": _score(cases, streamed),
        },
        "cases": [
            {
                "id": case["id"],
                "segments": case["segments"],
                "expected": case["expected"],
                "single_pass": single_text,
                "streamed": streamed_text,
                "single_pass_ok": single_text == case["expected"],
                "streamed_ok": streamed_text == case["expected"],
                "streaming_only": bool(case.get("streaming_only")),
            }
            for case, single_text, streamed_text in zip(cases, single, streamed)
        ],
    }
    report["streaming_agrees_with_single_pass"] = sum(
        1 for case, a, b in zip(cases, single, streamed)
        if a == b or case.get("streaming_only")
    )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=CASES_PATH)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--show-failures", action="store_true")
    args = parser.parse_args(argv)

    cases = _load_cases(args.cases)
    report = run(cases)

    for label in STAGE_LABELS:
        score = report["stages"][label]
        number = (
            f"num P {score['number_precision']:.3f} "
            f"R {score['number_recall']:.3f} F1 {score['number_f1']:.3f}"
            if score["number_f1"] is not None else "num n/a"
        )
        print(
            f"{label:29} | exact {score['exact_match']:.3f} "
            f"| WER {score['wer']:.6f} "
            f"| {number} "
            f"| entity P {score['entity_precision']:.3f} "
            f"R {score['entity_recall']:.3f}"
        )
    print(
        f"streaming == single-pass: "
        f"{report['streaming_agrees_with_single_pass']}/{report['case_count']}"
    )

    if args.show_failures:
        for case in report["cases"]:
            broken = not case["streamed_ok"] or (
                not case["single_pass_ok"] and not case["streaming_only"]
            )
            if broken:
                print(f"\n  [{case['id']}]")
                print(f"    expected : {case['expected']}")
                print(f"    single   : {case['single_pass']}")
                print(f"    streamed : {case['streamed']}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
