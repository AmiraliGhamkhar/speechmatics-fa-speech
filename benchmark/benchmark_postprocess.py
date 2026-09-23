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


def _score(cases: list[dict], produced: list[str]) -> dict:
    errors = reference_tokens = 0
    correct = detected = expected_total = 0
    exact = 0
    for case, hypothesis in zip(cases, produced):
        expected = case["expected"]
        measured = word_error_rate(expected, hypothesis)
        errors += int(measured["errors"])
        reference_tokens += int(measured["reference_tokens"])
        exact += int(hypothesis == expected)

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
    # A "streaming_only" case reproduces a final boundary INSIDE a written
    # value ("ساعت 10:" + "30"). Joining its segments with a space is not a
    # transcript the app ever produces, so it is scored on the streamed path
    # only rather than counted as a single-pass defect.
    scored = [
        (case, single_text)
        for case, single_text in zip(cases, single)
        if not case.get("streaming_only")
    ]
    report = {
        "case_count": len(cases),
        "single_pass": _score([c for c, _ in scored], [t for _, t in scored]),
        "streamed": _score(cases, streamed),
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

    for label in ("single_pass", "streamed"):
        score = report[label]
        print(
            f"{label:12} | exact {score['exact_match']:.3f} "
            f"| WER {score['wer']:.6f} "
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
