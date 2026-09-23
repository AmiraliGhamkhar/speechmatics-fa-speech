"""Audit ASR quality without altering clinical transcripts.

The benchmark separates recognition quality from deterministic post-processing:

* RAW WER compares the ASR transcript with the supplied reference;
* entity metrics compare the entities the engine actually recognized;
* ClinicalEntityGuard flags are measured as review metadata, never as a
  correction mechanism.

No audio is needed for the default ``--dry-run``/offline path.  Stored
transcripts make regressions reproducible and safe to run in CI.  ``--mode
live`` streams compatible WAV files to the existing Speechmatics realtime
adapter and requires ``SPEECHMATICS_API_KEY``.

Examples::

    python benchmark/benchmark_asr.py --dry-run
    python benchmark/benchmark_asr.py --mode offline --cases cases.json --out report.json
    python benchmark/benchmark_asr.py --mode live --cases cases.json

A case contains ``id``, ``reference``, ``expected_entities``, and either a
stored ``transcript``/``stored_transcript`` (offline) or ``audio_path`` (live).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import wave
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from speechmatics_test.entity_guard import ClinicalEntityGuard
from speechmatics_test import entity_guard as _guard_patterns
from speechmatics_test.text import normalize_text, tokens

ROOT = Path(__file__).resolve().parent.parent

# A deliberately synthetic stored result. It is useful both as a smoke test
# and as a regression example: several ASR values differ from reference, and
# only values the deterministic guard considers suspicious are flagged.
DEFAULT_TEST_CASES: list[dict[str, Any]] = [
    {
        "id": "nursing_report_001",
        "audio_path": "audio/nursing_001.wav",
        "reference": (
            "آقای 58 ساله با BP 145/90 و Temp 36.7 و SpO2 96% و IV-Line 20G "
            "و نرمال سالین 0.9% و Morse 45 و Braden 20"
        ),
        "stored_transcript": (
            "آقای 40 ساله با BP 100/80 و Temp 36 و SpO2 96% و IV-Line 40G "
            "و نرمال سالین 0.9% و Morse 10 و Braden 20"
        ),
        "expected_entities": {
            "age": "58",
            "bp": "145/90",
            "temp": "36.7",
            "spo2": "96%",
            "iv_gauge": "20G",
            "saline": "0.9%",
            "morse": "45",
            "braden": "20",
        },
    }
]

_ENTITY_ALIASES = {
    "temperature": "temperature",
    "temp": "temperature",
    "spo2": "spo2",
    "sp_o2": "spo2",
    "pulse": "pulse",
    "pr": "pulse",
    "iv_gauge": "iv_gauge",
    "gauge": "iv_gauge",
    "saline": "concentration",
    "concentration": "concentration",
    "bp": "bp",
    "age": "age",
    "morse": "morse",
    "braden": "braden",
}


def _entity_key(name: str) -> str:
    return _ENTITY_ALIASES.get(str(name).strip().casefold(), str(name).strip().casefold())


def _stored_transcript(case: dict[str, Any]) -> str:
    """Get an offline hypothesis without guessing an absent transcript."""
    for key in ("stored_transcript", "transcript", "raw_transcript", "hypothesis"):
        value = case.get(key)
        if isinstance(value, str):
            return value
    raise ValueError(
        f"case {case.get('id', '<unknown>')!r} has no stored transcript; "
        "provide stored_transcript/transcript for offline or --dry-run mode"
    )


def word_error_rate(reference: str, hypothesis: str) -> dict[str, int | float]:
    """Compute ordinary token-level WER with deterministic edit breakdown."""
    ref = tokens(reference)
    hyp = tokens(hypothesis)
    # Each cell is (cost, substitutions, deletions, insertions). The tuple
    # ordering makes equal-cost ties deterministic without affecting WER.
    table: list[list[tuple[int, int, int, int]]] = [
        [(0, 0, 0, 0)] * (len(hyp) + 1) for _ in range(len(ref) + 1)
    ]
    for i in range(1, len(ref) + 1):
        table[i][0] = (i, 0, i, 0)
    for j in range(1, len(hyp) + 1):
        table[0][j] = (j, 0, 0, j)
    for i, ref_token in enumerate(ref, 1):
        for j, hyp_token in enumerate(hyp, 1):
            if ref_token == hyp_token:
                table[i][j] = table[i - 1][j - 1]
                continue
            sub = table[i - 1][j - 1]
            delete = table[i - 1][j]
            insert = table[i][j - 1]
            choices = (
                (sub[0] + 1, sub[1] + 1, sub[2], sub[3]),
                (delete[0] + 1, delete[1], delete[2] + 1, delete[3]),
                (insert[0] + 1, insert[1], insert[2], insert[3] + 1),
            )
            table[i][j] = min(choices)
    distance, substitutions, deletions, insertions = table[-1][-1]
    return {
        "reference_tokens": len(ref),
        "hypothesis_tokens": len(hyp),
        "substitutions": substitutions,
        "deletions": deletions,
        "insertions": insertions,
        "errors": distance,
        "wer": round(distance / len(ref), 6) if ref else (0.0 if not hyp else 1.0),
    }


def _near_context(pattern: re.Pattern[str], text: str, start: int, end: int) -> bool:
    return pattern.search(text[max(0, start - 32):min(len(text), end + 32)]) is not None


def extract_entities(text: str) -> dict[str, str]:
    """Extract first labeled entity values for benchmark comparison only.

    This is a deterministic extractor, not a correction layer. It is kept in
    the benchmark so valid values (which correctly produce no guard flag) can
    still be included in entity accuracy/precision/recall.
    """
    normalized = normalize_text(text or "")
    result: dict[str, str] = {}

    match = _guard_patterns._BP_RE.search(normalized)
    if match:
        result["bp"] = f"{match.group(1)}/{match.group(2)}"
    match = _guard_patterns._TEMPERATURE_RE.search(normalized)
    if match:
        result["temperature"] = match.group(1)
    match = _guard_patterns._SPO2_RE.search(normalized)
    if match:
        result["spo2"] = match.group(1) + "%"
    match = _guard_patterns._PULSE_RE.search(normalized)
    if match:
        result["pulse"] = match.group(1)
    match = _guard_patterns._AGE_RE.search(normalized)
    if match:
        result["age"] = match.group(1) or match.group(2)
    for match in _guard_patterns._GAUGE_RE.finditer(normalized):
        if _near_context(_guard_patterns._IV_CONTEXT_RE, normalized, match.start(), match.end()):
            result["iv_gauge"] = match.group(1) + "G"
            break
    spo2_value_spans = [
        (match.start(1), match.end(1))
        for match in _guard_patterns._SPO2_RE.finditer(normalized)
    ]
    for match in _guard_patterns._PERCENT_RE.finditer(normalized):
        if any(start <= match.start(1) < end for start, end in spo2_value_spans):
            continue
        if _near_context(
            _guard_patterns._SOLUTION_CONTEXT_RE, normalized, match.start(), match.end()
        ):
            result["concentration"] = match.group(1) + "%"
            break
    match = _guard_patterns._MORSE_RE.search(normalized)
    if match:
        result["morse"] = match.group(1)
    match = _guard_patterns._BRADEN_RE.search(normalized)
    if match:
        result["braden"] = match.group(1)
    return result


def _normal_entity_value(entity_type: str, value: Any) -> str:
    """Compare chart spellings semantically but without changing transcripts."""
    text = normalize_text(str(value)).strip().replace(" ", "")
    entity_type = _entity_key(entity_type)
    if entity_type in {"temperature", "concentration"}:
        suffix = "%" if text.endswith("%") else ""
        number = text[:-1] if suffix else text
        try:
            return f"{float(number):g}{suffix}"
        except ValueError:
            return text.casefold()
    if entity_type == "spo2":
        number = text[:-1] if text.endswith("%") else text
        return number + "%"
    if entity_type == "iv_gauge":
        return text.upper()
    return text.casefold()


def _metric_ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def score_case(case: dict[str, Any], transcript: str, guard: ClinicalEntityGuard | None = None) -> dict[str, Any]:
    """Score one raw ASR transcript against its reference and expected entities."""
    if not isinstance(case.get("reference"), str):
        raise ValueError(f"case {case.get('id', '<unknown>')!r} requires a string reference")
    expected_raw = case.get("expected_entities", {})
    if not isinstance(expected_raw, dict):
        raise ValueError(f"case {case.get('id', '<unknown>')!r} expected_entities must be an object")

    expected = {
        _entity_key(name): str(value)
        for name, value in expected_raw.items()
        if value is not None
    }
    detected = extract_entities(transcript)
    expected_compare = {
        key: _normal_entity_value(key, value) for key, value in expected.items()
    }
    detected_compare = {
        key: _normal_entity_value(key, value) for key, value in detected.items()
    }
    all_types = sorted(set(expected) | set(detected))
    correct = {
        entity_type
        for entity_type in expected
        if entity_type in detected_compare
        and detected_compare[entity_type] == expected_compare[entity_type]
    }
    entity_results = {
        entity_type: {
            "expected": expected.get(entity_type),
            "detected": detected.get(entity_type),
            "correct": entity_type in correct,
        }
        for entity_type in all_types
    }

    guard = guard or ClinicalEntityGuard()
    flags = guard.scan(transcript)
    flags_json = [flag.to_dict() for flag in flags]
    wrong = {
        entity_type
        for entity_type in expected
        if entity_type in detected_compare and entity_type not in correct
    }
    flag_types = {_entity_key(flag.entity_type) for flag in flags}
    flagged_wrong = wrong & flag_types
    false_positive_types = correct & flag_types

    return {
        "id": case.get("id"),
        "audio_path": case.get("audio_path"),
        "reference": case["reference"],
        "raw_transcript": transcript,
        "raw_wer": word_error_rate(case["reference"], transcript),
        "expected_entities": expected,
        "detected_entities": detected,
        "entity_results": entity_results,
        "entity_metrics": {
            "correct": len(correct),
            "detected": len(detected),
            "expected": len(expected),
            "accuracy": _metric_ratio(len(correct), len(expected)),
            "precision": _metric_ratio(len(correct), len(detected)),
            "recall": _metric_ratio(len(correct), len(expected)),
        },
        "entity_flags": flags_json,
        "flag_effectiveness": {
            "wrong_entities": len(wrong),
            "wrong_entities_flagged": len(flagged_wrong),
            "effectiveness": _metric_ratio(len(flagged_wrong), len(wrong)),
            "false_positive_flags": len(false_positive_types),
            "correct_entities": len(correct),
        },
    }


async def _wav_chunks(path: Path) -> AsyncIterator[bytes]:
    """Yield an adapter-compatible WAV without persisting or converting audio."""
    with wave.open(str(path), "rb") as audio:
        if audio.getnchannels() != 1 or audio.getsampwidth() != 2 \
                or audio.getframerate() != 16_000 or audio.getcomptype() != "NONE":
            raise ValueError(
                f"{path} must be a 16 kHz mono 16-bit PCM WAV for live mode "
                "(conversion is intentionally not performed by this benchmark)"
            )
        while True:
            chunk = audio.readframes(3200)  # 200 ms at 16 kHz
            if not chunk:
                return
            yield chunk


async def transcribe_live_case(case: dict[str, Any], api_key: str) -> str:
    """Run the existing realtime adapter against one WAV and return RAW text."""
    audio_path = case.get("audio_path")
    if not isinstance(audio_path, str) or not audio_path.strip():
        raise ValueError(f"case {case.get('id', '<unknown>')!r} requires audio_path in live mode")
    path = Path(audio_path)
    if not path.is_absolute():
        path = ROOT / path
    if not path.exists():
        raise FileNotFoundError(f"audio file not found for case {case.get('id')!r}: {path}")

    from speechmatics_test.medical_layer import MedicalLayer
    from speechmatics_test.realtime import SpeechmaticsRealtime

    language = str(case.get("language", "fa"))
    medical = MedicalLayer(ROOT)
    client = SpeechmaticsRealtime(
        api_key=api_key,
        language=language,
        additional_vocab=medical.additional_vocab,
    )
    result = await client.run(_wav_chunks(path), lambda _text: None, lambda _text: None)
    if result.error:
        raise RuntimeError(f"live ASR failed for {case.get('id')!r}: {result.error}")
    return result.final_text


def _summary(case_reports: list[dict[str, Any]]) -> dict[str, Any]:
    wer_errors = sum(int(item["raw_wer"]["errors"]) for item in case_reports)
    wer_references = sum(int(item["raw_wer"]["reference_tokens"]) for item in case_reports)
    detected = sum(int(item["entity_metrics"]["detected"]) for item in case_reports)
    expected = sum(int(item["entity_metrics"]["expected"]) for item in case_reports)
    correct = sum(int(item["entity_metrics"]["correct"]) for item in case_reports)
    wrong = sum(int(item["flag_effectiveness"]["wrong_entities"]) for item in case_reports)
    flagged_wrong = sum(
        int(item["flag_effectiveness"]["wrong_entities_flagged"]) for item in case_reports
    )
    false_positives = sum(
        int(item["flag_effectiveness"]["false_positive_flags"]) for item in case_reports
    )

    per_entity: dict[str, Counter[str]] = defaultdict(Counter)
    for report in case_reports:
        for entity_type, entity in report["entity_results"].items():
            bucket = per_entity[entity_type]
            if entity["expected"] is not None:
                bucket["expected"] += 1
            if entity["detected"] is not None:
                bucket["detected"] += 1
            if entity["correct"]:
                bucket["correct"] += 1

    per_entity_metrics = {
        entity_type: {
            "correct": counts["correct"],
            "detected": counts["detected"],
            "expected": counts["expected"],
            "accuracy": _metric_ratio(counts["correct"], counts["expected"]),
            "precision": _metric_ratio(counts["correct"], counts["detected"]),
            "recall": _metric_ratio(counts["correct"], counts["expected"]),
        }
        for entity_type, counts in sorted(per_entity.items())
    }
    return {
        "case_count": len(case_reports),
        "raw_wer": {
            "errors": wer_errors,
            "reference_tokens": wer_references,
            "wer": _metric_ratio(wer_errors, wer_references),
        },
        "entity_metrics": {
            "correct": correct,
            "detected": detected,
            "expected": expected,
            "accuracy": _metric_ratio(correct, expected),
            "precision": _metric_ratio(correct, detected),
            "recall": _metric_ratio(correct, expected),
            "per_entity": per_entity_metrics,
        },
        "flag_effectiveness": {
            "wrong_entities": wrong,
            "wrong_entities_flagged": flagged_wrong,
            "effectiveness": _metric_ratio(flagged_wrong, wrong),
            "false_positive_flags": false_positives,
        },
    }


def run_benchmark(
    test_cases: list[dict[str, Any]],
    *,
    mode: str = "offline",
    dry_run: bool = False,
    api_key: str | None = None,
    live_transcriber: Callable[[dict[str, Any], str], str] | None = None,
) -> dict[str, Any]:
    """Run cases and return a JSON-serializable ASR quality report.

    ``dry_run`` is deliberately offline even if ``mode="live"`` was supplied:
    it guarantees no network or audio-file access and uses the stored
    transcript in every case.
    """
    if mode not in {"offline", "live"}:
        raise ValueError("mode must be 'offline' or 'live'")
    if not isinstance(test_cases, list):
        raise ValueError("test cases must be a list")
    effective_mode = "offline" if dry_run else mode
    guard = ClinicalEntityGuard()
    reports: list[dict[str, Any]] = []
    # An explicitly supplied key (including empty) wins; the environment is
    # only a fallback when no key argument was given at all.
    key = api_key if api_key is not None else os.getenv("SPEECHMATICS_API_KEY", "").strip()
    if isinstance(key, str):
        key = key.strip()
    if effective_mode == "live" and not key:
        raise RuntimeError("SPEECHMATICS_API_KEY is required for --mode live")

    for case in test_cases:
        if not isinstance(case, dict):
            raise ValueError("each test case must be an object")
        if effective_mode == "offline":
            transcript = _stored_transcript(case)
        elif live_transcriber is not None:
            transcript = live_transcriber(case, key)
        else:
            transcript = asyncio.run(transcribe_live_case(case, key))
        if not isinstance(transcript, str):
            raise ValueError(f"case {case.get('id', '<unknown>')!r} produced a non-string transcript")
        reports.append(score_case(case, transcript, guard))

    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "mode": effective_mode,
        "dry_run": bool(dry_run),
        "cases": reports,
        "summary": _summary(reports),
    }


def _load_cases(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return DEFAULT_TEST_CASES
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid test-case JSON {path}: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("test_cases", payload.get("cases"))
    if not isinstance(payload, list):
        raise ValueError("case file must be a JSON list or an object with test_cases/cases")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--dry-run", action="store_true",
                        help="use pre-stored transcripts; no audio/network access")
    parser.add_argument("--cases", type=Path,
                        help="JSON list of ASR cases (default: built-in dry-run sample)")
    parser.add_argument("--out", type=Path,
                        default=ROOT / "results" / "asr_benchmark.json")
    args = parser.parse_args(argv)

    try:
        cases = _load_cases(args.cases)
        report = run_benchmark(cases, mode=args.mode, dry_run=args.dry_run)
    except (OSError, RuntimeError, ValueError) as exc:
        parser.error(str(exc))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = report["summary"]
    print(f"ASR benchmark: {summary['case_count']} case(s), mode={report['mode']}")
    print(f"RAW WER: {summary['raw_wer']['wer']}")
    print(
        "Entity precision/recall: "
        f"{summary['entity_metrics']['precision']} / {summary['entity_metrics']['recall']}"
    )
    print(f"Report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
