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
import re
import platform
import statistics
import subprocess
import sys
import time
import tracemalloc
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmark.streaming import (  # noqa: E402
    evaluate_streaming,
    production_accumulator_factory,
)
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
from speechmatics_test.medical_layer import MedicalLayer  # noqa: E402
from speechmatics_test.nursing_text import (  # noqa: E402
    PolishReport,
    polish_document,
    prepolish_asr_artifacts,
)
from speechmatics_test.presentation import BIDI_CONTROLS  # noqa: E402
from speechmatics_test.text import normalize_text  # noqa: E402


# ------------------------------------------------------------ environment


def _git_sha() -> str | None:
    """The commit this run was generated from (``None`` when unavailable).

    Never copied from a previous result file: a stale SHA makes an artifact
    claim a revision that did not produce it.
    """
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=10,
        )
        sha = out.stdout.strip() or None
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=str(ROOT),
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
        return f"{sha}-dirty" if sha and dirty else sha
    except Exception:  # pragma: no cover - git may be unavailable
        return None


def _git_dirty() -> bool | None:
    """Whether tracked files differ from the recorded commit.

    A dirty tree means ``git_commit`` alone does NOT identify the code that
    produced the artifact, so the flag has to travel with it. ``None`` when
    git is unavailable - unknown is not the same as clean.
    """
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        return bool(out.stdout.strip())
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


def _stream_splits(text: str) -> list[list[str]]:
    """Deterministic final-segment strategies, including a one-final control."""
    tokens = text.split()
    if len(tokens) < 2:
        return [[text]]
    cuts = {1, len(tokens) - 1, len(tokens) // 2}
    return [[text]] + [
        [" ".join(tokens[:cut]), " ".join(tokens[cut:])]
        for cut in sorted(cuts) if 0 < cut < len(tokens)
    ]


def evaluate_streaming_cases() -> dict:
    """Exercise fixtures through the production FinalStreamCanonicalizer."""
    # Local import avoids making app startup part of matcher-only imports.
    from app import FinalStreamCanonicalizer

    variants = []
    for case in ALL_CASES:
        for segments in _stream_splits(case.spoken):
            accumulator = FinalStreamCanonicalizer(MedicalLayer(ROOT))
            emissions = [
                accumulator.add(normalize_text(segment), [])
                for segment in segments
            ]
            emissions.append(accumulator.flush())
            produced = " ".join(part for part in emissions if part).strip()
            exact = produced == case.expected or \
                produced.rstrip(".") == case.expected.rstrip(".")
            variants.append({
                "id": case.id,
                "segments": segments,
                "produced": produced,
                "expected": case.expected,
                "exact_match": exact,
            })
    exact_count = sum(item["exact_match"] for item in variants)
    return {
        "case_count": len(ALL_CASES),
        "variant_count": len(variants),
        "exact_match": exact_count,
        "exact_match_accuracy": round(exact_count / len(variants), 4),
        "failures": [item for item in variants if not item["exact_match"]],
    }


# ---------------------------------------------------------------- scoring


def _count_occurrences(text: str, term: str) -> int:
    """Non-overlapping occurrences of a canonical term inside ``text``.

    Whole-token comparison, so ``US`` is not counted inside ``ultrasound``
    and ``mg`` is not counted inside ``mgH``.
    """
    if not term:
        return 0
    return len(re.findall(rf"(?<!\w){re.escape(term)}(?!\w)", text))


def _term_multiset(text: str, vocabulary: set[str]) -> Counter:
    """Multiset of the canonical terms from ``vocabulary`` present in ``text``."""
    found: Counter = Counter()
    for term in vocabulary:
        count = _count_occurrences(text, term)
        if count:
            found[term] = count
    return found


def _term_metrics(case: Case, produced: str, hits: list) -> dict:
    """Terminology metrics over the COMPLETE produced vs expected term sets.

    Metric definition (per case, multiset / duplicate-aware):

    * the term vocabulary considered is the union of the case's declared
      ``expected_terms`` and every canonical the matcher actually emitted;
    * ``expected`` = occurrences of those terms in the case's reference
      transcript (``case.expected``) - the reference is the authority on
      which medical terms belong in the output, not the declared subset,
      which only lists the terms the fixture was written to prove;
    * ``produced`` = occurrences of those terms in the pipeline output;
    * ``TP  = sum(min(produced[t], expected[t]))``
    * ``FP  = sum(produced - expected)``  (an extra/duplicated medical term)
    * ``FN  = sum(expected - produced)``  (a missing medical term)

    The old implementation only counted declared terms and therefore scored
    ``expected=HTN, produced="HTN COPD"`` as ``TP=1, FP=0``: a fabricated
    medical term was invisible whenever the case also got its expected term
    right. ``missing`` keeps listing the DECLARED terms that are absent, so
    the existing per-case audit trail is unchanged.
    """
    vocabulary = set(case.expected_terms) | {h["canonical"] for h in hits}
    produced_terms = _term_multiset(produced, vocabulary)
    expected_terms = _term_multiset(case.expected, vocabulary)

    tp = sum((produced_terms & expected_terms).values())
    fp = sum((produced_terms - expected_terms).values())
    fn = sum((expected_terms - produced_terms).values())
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "expected_terms": dict(sorted(expected_terms.items())),
        "produced_terms": dict(sorted(produced_terms.items())),
        "extra": sorted((produced_terms - expected_terms).elements()),
        "missing": sorted(
            term for term in set(case.expected_terms)
            if not _count_occurrences(produced, term)
        ),
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

        term_metrics = _term_metrics(case, produced, hits)
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

    # Terminology TP/FP/FN are summed from the per-case multiset comparison
    # of PRODUCED vs EXPECTED canonical terms (see _term_metrics): an extra
    # medical term is a false positive even when the case also produced its
    # expected term correctly.
    term_tp = sum(r["terms"]["tp"] for r in results)
    term_fn = sum(r["terms"]["fn"] for r in results)
    term_fp = sum(r["terms"]["fp"] for r in results)

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
    # The SAME fixtures, replayed through the REAL production streaming
    # accumulator (see benchmark/streaming.py). Reported separately: it is a
    # different pipeline shape, not a different dataset.
    streaming_start = time.perf_counter()
    streaming = evaluate_streaming(production_accumulator_factory())
    streaming_seconds = time.perf_counter() - streaming_start
    performance = measure_performance(matcher, repeats)
    return {
        "label": label,
        "git_commit": _git_sha(),
        "git_dirty": _git_dirty(),
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
        "streaming": {
            **streaming["aggregates"],
            "runtime_seconds": round(streaming_seconds, 3),
        },
        "cases": evaluation["cases"],
        "streaming_runs": streaming["runs"],
    }


def render_markdown(payload: dict) -> str:
    agg = payload["aggregates"]
    stream = payload["streaming"]
    perf = payload["performance"]
    lines = [
        "# SwiftMedics benchmark results",
        "",
        "Do not edit: this file is regenerated by the benchmark run.",
        "",
        "```powershell",
        ".\\.venv\\Scripts\\python.exe scripts\\swiftmedics_tools.py benchmark",
        "```",
        "",
        "Every number below is measured by that run - nothing is hardcoded.",
        "",
        "Both scores below measure DETERMINISTIC POST-PROCESSING of already-",
        "transcribed text. Neither is a Speechmatics recognition-accuracy",
        "number: no audio is involved anywhere in this benchmark.",
        "",
        "## Environment",
        "",
        f"* commit: `{payload['git_commit']}`"
        + ("  **(working tree dirty - this commit alone does not identify "
           "the code that produced these numbers)**"
           if payload.get("git_dirty") else
           ("  (git unavailable: provenance unverified)"
            if payload.get("git_dirty") is None else "")),
        f"* python: {payload['python_version']}",
        f"* platform: {payload['platform']}",
        f"* matcher backend: {payload['matcher_backend']}",
        f"* dictionary terms: {payload['dictionary_terms']}",
        f"* compiled matcher rules: {payload['matcher_rules']}",
        f"* Speechmatics vocabulary entries: {payload['vocabulary_entries']}",
        f"* dataset version: {payload['dataset']['dataset_version']} "
        f"({payload['dataset']['case_count']} cases)",
        "",
        "## Post-processing accuracy (not ASR accuracy)",
        "",
        "These fixtures contain text, not audio. Whole-text and streaming "
        "scores measure deterministic post-processing only.",
        "",
        f"* whole-text exact-match accuracy: **{agg['exact_match']}/{agg['total_cases']}"
        f" ({agg['exact_match_accuracy']:.1%})**",
        f"* streaming-boundary exact-match: **"
        f"{payload['streaming']['exact_match']}/"
        f"{payload['streaming']['variant_count']} "
        f"({payload['streaming']['exact_match_accuracy']:.1%})** "
        f"across {payload['streaming']['case_count']} fixtures",
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
        "### Streaming-boundary post-processing",
        "",
        "The same fixtures replayed through the REAL production streaming",
        "accumulator (`app.FinalStreamCanonicalizer` + `MedicalLayer`), split",
        "into synthetic Speechmatics final segments. This is the shape the",
        "application actually runs in; the whole-text score above cannot see",
        "final-segment boundary bugs at all.",
        "",
        f"* streaming exact-match: **{stream['exact_match']}/"
        f"{stream['total_runs']} ({stream['exact_match_accuracy']:.1%})** "
        f"across {len(stream['strategies'])} split strategies",
        f"* streaming runtime: {stream['runtime_seconds']} s",
        "",
        "| split strategy | runs | exact | accuracy |",
        "| --- | --- | --- | --- |",
    ]
    for strategy, data in stream["by_strategy"].items():
        lines.append(
            f"| {strategy} | {data['runs']} | {data['exact_match']} | "
            f"{data['accuracy']:.1%} |"
        )
    if stream["failures"]:
        lines += [
            "",
            "Open streaming failures (`case:strategy`): "
            + ", ".join(f"`{name}`" for name in stream["failures"]),
        ]
    lines += [
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
    print(f"streaming exact    : {payload['streaming']['exact_match']}/"
          f"{payload['streaming']['total_runs']} "
          f"({payload['streaming']['exact_match_accuracy']:.1%})")
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
