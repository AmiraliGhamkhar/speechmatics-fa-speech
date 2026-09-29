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

    raw        - the stored ASR segments verbatim (what Speechmatics said)
    normalized - generic normalization only (script/digits/spacing)
    canonical  - the deterministic medical layer, single pass
    injected   - the streamed canonical text, i.e. what the clinician pastes
                 (end-to-end for the post-processing half of the pipeline)

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
from speechmatics_test.medical_layer import MedicalLayer  # noqa: E402
from speechmatics_test.text import normalize_text  # noqa: E402

CASES_PATH = Path(__file__).resolve().parent / "postprocess_cases.json"


def _load_cases(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def _single_pass(layer: MedicalLayer, segments: list[str]) -> str:
    text = normalize_text(" ".join(segments))
    return layer.canonicalize(text, preserve_narrative=True)[0]


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
    report = {
        "case_count": len(cases),
        "single_pass": _score([c for c, _ in scored], [t for _, t in scored]),
        "streamed": _score(cases, streamed),
        # The five separated metrics this benchmark exists to expose:
        #   raw        -> how good the stored ASR output is as delivered
        #   normalized -> generic normalization only (no medical knowledge)
        #   single_pass-> medical canonicalization over the whole transcript
        #   streamed   -> the same through the realtime final boundaries,
        #                 which is the text actually injected (end to end for
        #                 this half of the pipeline)
        "stages": {
            "raw": _score([c for c, _ in stage_subset], [
                text for _, text in stage_subset
            ]),
            "normalized": _score(
                [c for c, _ in stage_subset],
                [normalize_text(text) for _, text in stage_subset],
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

    for label in ("raw", "normalized", "canonical_single_pass",
                  "canonical_streamed"):
        score = report["stages"][label]
        number = (
            f"num P {score['number_precision']:.3f} "
            f"R {score['number_recall']:.3f} F1 {score['number_f1']:.3f}"
            if score["number_f1"] is not None else "num n/a"
        )
        print(
            f"{label:19} | exact {score['exact_match']:.3f} "
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
