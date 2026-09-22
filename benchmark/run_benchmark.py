"""Nursing normalization benchmark runner.

Measures the deterministic post-processing pipeline

    normalize_text -> MedicalMatcher.canonicalize -> polish_nursing_text

against the versioned fixtures in ``benchmark/dataset.py`` and writes a
machine-readable result file plus a human-readable summary.

Everything reported here is MEASURED. Nothing is hardcoded: the term
counts, rule counts, engine name, latency and accuracy numbers all come
from the run that produced the file.

Reported metrics
----------------
* per-stage and per-group exact-match accuracy (stages are never averaged
  together - an ASR error, a lexical canonicalization and a formatting fix
  are different things);
* medical term precision / recall / F1 / false-positive / false-negative;
* number precision / recall / F1 and a false-number rate (a fabricated or
  dropped numeral is a patient-safety event, so it is scored separately);
* warning behaviour for the deliberately ambiguous fixtures;
* build latency, per-sentence and per-character match latency, peak memory.

Usage::

    python benchmark/run_benchmark.py --out benchmark/results_current.json
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.dataset import (  # noqa: E402
    ALL_CASES,
    DATASET_VERSION,
    Case,
    cases_by_group,
    cases_by_stage,
    dataset_summary,
)
from speechmatics_test.evaluation import numbers  # noqa: E402
from speechmatics_test.matcher import MedicalMatcher  # noqa: E402
from speechmatics_test.nursing_text import (  # noqa: E402
    PolishReport,
    polish_document,
    prepolish_asr_artifacts,
)
from speechmatics_test.presentation import BIDI_CONTROLS  # noqa: E402
from speechmatics_test.text import normalize_text  # noqa: E402


# ------------------------------------------------------------ environment


def _git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=10,
        )
        return out.stdout.strip() or None
    except Exception:  # pragma: no cover - git may be unavailable
        return None


def _package_versions() -> dict:
    versions: dict[str, str | None] = {}
    for name in ("pyahocorasick", "pytest", "speechmatics-rt"):
        try:
            from importlib.metadata import version
            versions[name] = version(name)
        except Exception:
            versions[name] = None
    return versions


# --------------------------------------------------------------- pipeline


def run_pipeline(matcher: MedicalMatcher, spoken: str) -> tuple[str, list, PolishReport]:
    """The exact production post-processing chain, start to finish."""
    report = PolishReport()
    normalized = normalize_text(spoken)
    # ASR stutter is removed BEFORE canonicalization: a duplicated word can
    # otherwise combine with its neighbour into a dictionary phrase the
    # speaker never said (see prepolish_asr_artifacts).
    normalized = prepolish_asr_artifacts(
        normalized, report, protected=matcher.repetition_safe_forms)
    canonical, hits = matcher.canonicalize(normalized)
    polished = polish_document(canonical, report)
    return polished, hits, report


# ---------------------------------------------------------------- scoring


def _term_metrics(case: Case, produced: str) -> dict:
    """Medical-term presence against the case's declared expectations."""
    expected = set(case.expected_terms)
    if not expected:
        return {"tp": 0, "fn": 0, "missing": []}
    missing = [term for term in sorted(expected) if term not in produced]
    return {
        "tp": len(expected) - len(missing),
        "fn": len(missing),
        "missing": missing,
    }


def _number_metrics(case: Case, produced: str) -> dict:
    """Numeric fidelity: every declared value must survive verbatim.

    Also counts numerals the pipeline produced that the reference does not
    contain (``false_numbers``) - the fabrication case that a recall-only
    metric cannot see.
    """
    expected = list(case.expected_numbers)
    produced_numbers = numbers(produced)
    reference_numbers = numbers(case.expected)

    missing = [n for n in expected if n not in produced]
    remaining = list(reference_numbers)
    false_numbers = []
    for value in produced_numbers:
        if value in remaining:
            remaining.remove(value)
        else:
            false_numbers.append(value)
    return {
        "expected_declared": expected,
        "missing_declared": missing,
        "reference_numbers": reference_numbers,
        "produced_numbers": produced_numbers,
        "false_numbers": false_numbers,
        "dropped_numbers": remaining,
    }


def _prf(tp: int, fp: int, fn: int) -> dict:
    precision = tp / (tp + fp) if (tp + fp) else None
    recall = tp / (tp + fn) if (tp + fn) else None
    if precision and recall:
        f1 = 2 * precision * recall / (precision + recall)
    elif precision is None and recall is None:
        f1 = None
    else:
        f1 = 0.0
    return {
        "precision": round(precision, 4) if precision is not None else None,
        "recall": round(recall, 4) if recall is not None else None,
        "f1": round(f1, 4) if f1 is not None else None,
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
    }


def evaluate_cases(matcher: MedicalMatcher) -> dict:
    """Run every fixture and collect per-case + aggregate results."""
    results = []
    for case in ALL_CASES:
        produced, hits, report = run_pipeline(matcher, case.spoken)
        expected = case.expected
        # A paragraph reference ends with the sentence-final period that
        # polish_document adds; short fixtures are compared as-is plus the
        # same terminal-period tolerance, so the fixture author never has
        # to hand-maintain punctuation that the rule generates.
        exact = produced == expected or produced.rstrip(".") == expected.rstrip(".")

        term_metrics = _term_metrics(case, produced)
        number_metrics = _number_metrics(case, produced)
        has_warning = bool(report.warnings)

        results.append({
            "id": case.id,
            "stage": case.stage,
            "group": case.group,
            "spoken": case.spoken,
            "expected": expected,
            "produced": produced,
            "exact_match": exact,
            "medical_hits": [h["canonical"] for h in hits],
            "terms": term_metrics,
            "numbers": number_metrics,
            "warnings": report.warnings,
            "warning_expected": case.expect_warning,
            "warning_behaviour_ok": has_warning == case.expect_warning,
            "bidi_controls_in_output": sorted(
                {ch for ch in produced if ch in BIDI_CONTROLS}
            ),
            "note": case.note,
        })

    return {"cases": results, "aggregates": _aggregate(results)}


def _aggregate(results: list[dict]) -> dict:
    total = len(results)
    exact = sum(1 for r in results if r["exact_match"])

    term_tp = sum(r["terms"]["tp"] for r in results)
    term_fn = sum(r["terms"]["fn"] for r in results)
    # A false positive here is a medical rewrite in a case that declared no
    # expected terms and whose output diverged from the reference: the
    # matcher changed clinical wording it should have left alone.
    term_fp = sum(
        len(r["medical_hits"]) for r in results
        if not r["terms"]["tp"] and not r["terms"]["fn"] and not r["exact_match"]
    )

    number_ref = sum(len(r["numbers"]["reference_numbers"]) for r in results)
    number_false = sum(len(r["numbers"]["false_numbers"]) for r in results)
    number_dropped = sum(len(r["numbers"]["dropped_numbers"]) for r in results)
    number_tp = number_ref - number_dropped

    by_stage: dict[str, dict] = {}
    for stage, cases in cases_by_stage().items():
        ids = {c.id for c in cases}
        subset = [r for r in results if r["id"] in ids]
        if not subset:
            continue
        by_stage[stage] = {
            "cases": len(subset),
            "exact_match": sum(1 for r in subset if r["exact_match"]),
            "accuracy": round(
                sum(1 for r in subset if r["exact_match"]) / len(subset), 4
            ),
            "failures": [r["id"] for r in subset if not r["exact_match"]],
        }

    by_group: dict[str, dict] = {}
    for group, cases in sorted(cases_by_group().items()):
        ids = {c.id for c in cases}
        subset = [r for r in results if r["id"] in ids]
        by_group[group] = {
            "cases": len(subset),
            "exact_match": sum(1 for r in subset if r["exact_match"]),
            "accuracy": round(
                sum(1 for r in subset if r["exact_match"]) / len(subset), 4
            ),
            "failures": [r["id"] for r in subset if not r["exact_match"]],
        }

    return {
        "total_cases": total,
        "exact_match": exact,
        "exact_match_accuracy": round(exact / total, 4) if total else None,
        "terminology": _prf(term_tp, term_fp, term_fn),
        "numbers": {
            **_prf(number_tp, number_false, number_dropped),
            "reference_numbers": number_ref,
            "false_number_rate": round(
                number_false / number_ref, 4) if number_ref else None,
            "dropped_number_rate": round(
                number_dropped / number_ref, 4) if number_ref else None,
        },
        "warning_behaviour_ok": sum(
            1 for r in results if r["warning_behaviour_ok"]),
        "bidi_clean": all(
            not r["bidi_controls_in_output"] for r in results),
        "by_stage": by_stage,
        "by_group": by_group,
    }


# ------------------------------------------------------------ performance


def measure_performance(matcher: MedicalMatcher, repeats: int) -> dict:
    """Build latency, match latency and peak memory for the REAL dictionary."""
    build_times = []
    kept = []
    for _ in range(3):
        start = time.perf_counter()
        kept.append(MedicalMatcher(ROOT))
        build_times.append(time.perf_counter() - start)
    kept.clear()

    tracemalloc.start()
    memory_matcher = MedicalMatcher(ROOT)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    sentences = [normalize_text(c.spoken) for c in ALL_CASES]
    for sentence in sentences:  # warmup
        memory_matcher.canonicalize(sentence)

    match_times: list[float] = []
    full_times: list[float] = []
    for sentence in sentences:
        for _ in range(repeats):
            start = time.perf_counter()
            memory_matcher.canonicalize(sentence)
            match_times.append(time.perf_counter() - start)
        for _ in range(repeats):
            start = time.perf_counter()
            canonical, _ = memory_matcher.canonicalize(sentence)
            polish_document(canonical, PolishReport())
            full_times.append(time.perf_counter() - start)

    mean_chars = statistics.mean(len(s) for s in sentences)
    longest = max(sentences, key=len)
    long_times = []
    long_paragraph = " ".join(sentences)  # worst realistic paragraph
    for _ in range(max(10, repeats // 5)):
        start = time.perf_counter()
        canonical, _ = memory_matcher.canonicalize(long_paragraph)
        polish_document(canonical, PolishReport())
        long_times.append(time.perf_counter() - start)

    return {
        "repeats_per_sentence": repeats,
        "sentences": len(sentences),
        "build_time_ms": {
            "min": round(min(build_times) * 1000, 3),
            "median": round(statistics.median(build_times) * 1000, 3),
        },
        "build_peak_traced_memory_mb": round(peak / 1_048_576, 3),
        "matcher_latency_us": {
            "mean": round(statistics.mean(match_times) * 1e6, 2),
            "p50": round(statistics.median(match_times) * 1e6, 2),
            "max": round(max(match_times) * 1e6, 2),
        },
        "full_pipeline_latency_us": {
            "mean": round(statistics.mean(full_times) * 1e6, 2),
            "p50": round(statistics.median(full_times) * 1e6, 2),
            "max": round(max(full_times) * 1e6, 2),
        },
        "mean_latency_us_per_character": round(
            statistics.mean(full_times) * 1e6 / mean_chars, 4),
        "longest_sentence_characters": len(longest),
        "long_paragraph": {
            "characters": len(long_paragraph),
            "mean_ms": round(statistics.mean(long_times) * 1000, 3),
            "max_ms": round(max(long_times) * 1000, 3),
        },
    }


# -------------------------------------------------------------- reporting


def build_payload(label: str, repeats: int) -> dict:
    matcher = MedicalMatcher(ROOT)
    vocab_path = ROOT / "medical_knowledge" / "speechmatics_additional_vocab.json"
    evaluation = evaluate_cases(matcher)
    performance = measure_performance(matcher, repeats)
    return {
        "label": label,
        "git_commit": _git_sha(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "packages": _package_versions(),
        "matcher_backend": matcher.engine,
        "uses_ahocorasick": matcher.uses_ahocorasick,
        "dictionary_terms": len(matcher.terms),
        "matcher_rules": len(matcher.rules),
        "vocabulary_entries": (
            len(json.loads(vocab_path.read_text(encoding="utf-8")))
            if vocab_path.exists() else None
        ),
        "dataset": dataset_summary(),
        "performance": performance,
        "aggregates": evaluation["aggregates"],
        "cases": evaluation["cases"],
    }


def render_markdown(payload: dict) -> str:
    agg = payload["aggregates"]
    perf = payload["performance"]
    lines = [
        "# SwiftMedics benchmark results",
        "",
        "Generated by `python scripts/swiftmedics_tools.py benchmark`. "
        "Every number below is measured by that run - nothing is hardcoded.",
        "",
        "## Environment",
        "",
        f"* commit: `{payload['git_commit']}`",
        f"* python: {payload['python_version']}",
        f"* platform: {payload['platform']}",
        f"* matcher backend: {payload['matcher_backend']}",
        f"* dictionary terms: {payload['dictionary_terms']}",
        f"* compiled matcher rules: {payload['matcher_rules']}",
        f"* Speechmatics vocabulary entries: {payload['vocabulary_entries']}",
        f"* dataset version: {payload['dataset']['dataset_version']} "
        f"({payload['dataset']['case_count']} cases)",
        "",
        "## Accuracy",
        "",
        f"* exact-match accuracy: **{agg['exact_match']}/{agg['total_cases']}"
        f" ({agg['exact_match_accuracy']:.1%})**",
        f"* terminology F1: {agg['terminology']['f1']} "
        f"(P {agg['terminology']['precision']}, "
        f"R {agg['terminology']['recall']}, "
        f"FP {agg['terminology']['false_positives']}, "
        f"FN {agg['terminology']['false_negatives']})",
        f"* number F1: {agg['numbers']['f1']} "
        f"(P {agg['numbers']['precision']}, R {agg['numbers']['recall']})",
        f"* false-number rate: {agg['numbers']['false_number_rate']} "
        f"| dropped-number rate: {agg['numbers']['dropped_number_rate']}",
        f"* ambiguity warnings behaving as specified: "
        f"{agg['warning_behaviour_ok']}/{agg['total_cases']}",
        f"* canonical output free of BiDi controls: {agg['bidi_clean']}",
        "",
        "### By pipeline stage",
        "",
        "Stages are reported separately on purpose: a lexical "
        "canonicalization, a numeric normalization and a formatting fix are "
        "different kinds of change and must not be averaged into one claim.",
        "",
        "| stage | cases | exact | accuracy |",
        "| --- | --- | --- | --- |",
    ]
    for stage, data in agg["by_stage"].items():
        lines.append(
            f"| {stage} | {data['cases']} | {data['exact_match']} | "
            f"{data['accuracy']:.1%} |"
        )
    lines += [
        "",
        "### By terminology group",
        "",
        "| group | cases | exact | accuracy |",
        "| --- | --- | --- | --- |",
    ]
    for group, data in agg["by_group"].items():
        lines.append(
            f"| {group} | {data['cases']} | {data['exact_match']} | "
            f"{data['accuracy']:.1%} |"
        )
    lines += [
        "",
        "## Performance (real dictionary)",
        "",
        f"* dictionary build (load + validate + automaton): "
        f"{perf['build_time_ms']['min']} ms min, "
        f"{perf['build_time_ms']['median']} ms median",
        f"* build peak traced memory: "
        f"{perf['build_peak_traced_memory_mb']} MB",
        f"* matcher-only latency: {perf['matcher_latency_us']['mean']} us mean, "
        f"{perf['matcher_latency_us']['p50']} us p50",
        f"* full pipeline (matcher + polish): "
        f"{perf['full_pipeline_latency_us']['mean']} us mean, "
        f"{perf['full_pipeline_latency_us']['p50']} us p50",
        f"* per-character: {perf['mean_latency_us_per_character']} us",
        f"* long paragraph ({perf['long_paragraph']['characters']} chars): "
        f"{perf['long_paragraph']['mean_ms']} ms mean, "
        f"{perf['long_paragraph']['max_ms']} ms max",
        "",
    ]
    comparison = ROOT / "benchmark" / "results_comparison.json"
    if comparison.exists():
        try:
            data = json.loads(comparison.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if data and data.get("dataset_version") == \
                payload["dataset"]["dataset_version"]:
            base, cur = data["baseline"], data["current"]
            lines += [
                "## Baseline comparison",
                "",
                data["comparison_note"],
                "",
                "| metric | baseline | current |",
                "| --- | --- | --- |",
                f"| exact-match | {base['exact_match']}/"
                f"{data['dataset_cases']} "
                f"({base['exact_match_accuracy']:.1%}) | "
                f"{cur['exact_match']}/{data['dataset_cases']} "
                f"({cur['exact_match_accuracy']:.1%}) |",
                f"| terminology F1 | {base['terminology']['f1']} | "
                f"{cur['terminology']['f1']} |",
                f"| number F1 | {base['numbers']['f1']} | "
                f"{cur['numbers']['f1']} |",
                f"| dictionary terms | {base['dictionary_terms']} | "
                f"{cur['dictionary_terms']} |",
                f"| compiled rules | {base['matcher_rules']} | "
                f"{cur['matcher_rules']} |",
                f"| build time (ms, min) | "
                f"{base['performance']['build_time_ms_min']} | "
                f"{cur['performance']['build_time_ms_min']} |",
                f"| build peak memory (MB) | "
                f"{base['performance']['build_peak_traced_memory_mb']} | "
                f"{cur['performance']['build_peak_traced_memory_mb']} |",
                f"| matcher latency p50 (us) | "
                f"{base['performance']['matcher_latency_us_p50']} | "
                f"{cur['performance']['matcher_latency_us_p50']} |",
                f"| full pipeline p50 (us) | "
                f"{base['performance']['full_pipeline_latency_us_p50']} | "
                f"{cur['performance']['full_pipeline_latency_us_p50']} |",
                "",
                "Per stage (exact-match accuracy):",
                "",
                "| stage | baseline | current |",
                "| --- | --- | --- |",
            ]
            for stage, value in cur["by_stage"].items():
                lines.append(
                    f"| {stage} | {base['by_stage'][stage]:.1%} | "
                    f"{value:.1%} |"
                )
            lines.append("")

    failures = [c for c in payload["cases"] if not c["exact_match"]]
    lines += [
        f"## Open failures ({len(failures)})",
        "",
    ]
    if not failures:
        lines.append("None.")
    else:
        for case in failures:
            lines += [
                f"### `{case['id']}` ({case['stage']} / {case['group']})",
                "",
                f"* spoken   : `{case['spoken']}`",
                f"* expected : `{case['expected']}`",
                f"* produced : `{case['produced']}`",
                "",
            ]
    return "\n".join(lines) + "\n"


def main(out: Path | None = None, repeats: int = 50,
         label: str = "current") -> int:
    out = Path(out) if out else ROOT / "benchmark" / "results_current.json"
    payload = build_payload(label, repeats)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    agg = payload["aggregates"]
    print(f"dataset            : {payload['dataset']['dataset_version']} "
          f"({payload['dataset']['case_count']} cases)")
    print(f"matcher backend    : {payload['matcher_backend']}")
    print(f"dictionary terms   : {payload['dictionary_terms']} "
          f"({payload['matcher_rules']} compiled rules)")
    print(f"exact match        : {agg['exact_match']}/{agg['total_cases']} "
          f"({agg['exact_match_accuracy']:.1%})")
    print(f"terminology F1     : {agg['terminology']['f1']}")
    print(f"number F1          : {agg['numbers']['f1']} "
          f"(false-number rate {agg['numbers']['false_number_rate']})")
    print(f"bidi clean         : {agg['bidi_clean']}")
    print(f"full pipeline p50  : "
          f"{payload['performance']['full_pipeline_latency_us']['p50']} us")
    print()
    for stage, data in agg["by_stage"].items():
        print(f"  {stage:26} {data['exact_match']:>3}/{data['cases']:<3} "
              f"{data['accuracy']:.1%}")
    print()
    print(f"written: {out}")

    if label == "current":
        readme = ROOT / "benchmark" / "README.md"
        readme.write_text(render_markdown(payload), encoding="utf-8")
        print(f"written: {readme}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--repeats", type=int, default=50)
    parser.add_argument("--label", default="current")
    args = parser.parse_args()
    raise SystemExit(main(args.out, args.repeats, args.label))
