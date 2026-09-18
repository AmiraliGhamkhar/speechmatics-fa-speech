"""Matcher performance benchmark.

For synthetic medical dictionaries of ~100 / 500 / 1000 / 2000 terms this
script measures:

- startup build time: JSON load + validation + dedup/conflict resolution +
  Aho-Corasick automaton build (the app does exactly this once per run);
- matching latency: per-sentence and average, over realistic mixed
  Persian/English clinical sentences (both no-match scans, which are the
  dictionary-size-sensitive path, and match-heavy sentences);
- peak traced memory during one build (tracemalloc).

``benchmark/results_before.json`` / ``results_after.json`` record the
consolidation-refactor comparison (before = pre-consolidation code at commit
a4a7e07, measured with the same loop against the legacy multi-file loader).

Usage:
    python benchmark/benchmark_matcher.py --out benchmark/results.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from speechmatics_test.matcher import MedicalFST  # stable public class (§0)
from speechmatics_test.text import normalize_text

ROOT = Path(__file__).resolve().parent

#: Realistic mixed Persian/English clinical sentences (no-match heavy path:
#: they must scan the whole automaton without producing hits).
NO_MATCH_SENTENCES = [
    "بیمار مرد شصت ساله با درد قفسه سینه از ساعت سه بامداد مراجعه کرده است",
    "در معاینه شکم حساسیت در ربع بالایی راست وجود دارد و صدای روده کاهش یافته است",
    "The patient was admitted for observation overnight and reassessed in the morning",
    "سابقه آلرژی دارویی و جراحی قبلی در پرونده ثبت شده و مورد بررسی قرار گرفت",
    "نام بیمار و شماره پرونده و تاریخ پذیرش در سیستم ثبت شد",
    "Family history is significant for coronary artery disease in a first degree relative",
    "بیمار توضیح داد که علائم از هفته گذشته شروع شده و به تدریج بدتر شده است",
    "Vital signs were stable and the patient was comfortable at rest without distress",
]

#: Prefixes used to synthesise realistic multi-token Persian medical forms.
_FORM_STEMS = [
    "سندرم", "بیماری", "ناهنجاری", "تزریق", "بخش", "درمان",
    "بررسی", "پیگیری", "گزارش", "اندازه گیری", "پایش", "مشاوره",
]
_FORM_TAILS = [
    "خاص", "مزمن", "حاد", "پیشرفته", "اولیه", "ثانویه",
    "شایع", "نادر", "موضعی", "گسترده", "سریع", "آهسته",
]

SYNTH_TIERS = ("abbreviation", "phrase", "validated_term")
SYNTH_CANONICAL_TEMPLATE = "Synth-{stem}-{tail}-{n}"


def _synthetic_terms(count: int) -> list[dict]:
    """Deterministic synthetic dictionary of ``count`` terms.

    Each term has a Persian multi-token form, mirroring the shape of the
    real medical dictionary (multi-word Persian phrases). Terms never
    collide with real vocabulary.
    """
    terms: list[dict] = []
    for n in range(count):
        stem = _FORM_STEMS[n % len(_FORM_STEMS)]
        tail = _FORM_TAILS[(n // len(_FORM_STEMS)) % len(_FORM_TAILS)]
        seq = n // (len(_FORM_STEMS) * len(_FORM_TAILS))
        # "سندرم خاص" forms are plain Persian phrases with the token shapes
        # the real dictionary uses; padding keeps them unique per n.
        form = f"{stem} {tail}" + (f" نوع {seq}" if seq else "")
        terms.append({
            "id": f"synth-{n}",
            "canonical": SYNTH_CANONICAL_TEMPLATE.format(stem=stem, tail=tail, n=n),
            "type": "term",
            "tier": SYNTH_TIERS[n % len(SYNTH_TIERS)],
            "forms": [form],
        })
    return terms


def _match_sentences(terms: list[dict]) -> list[str]:
    """Sentences that embed synthetic forms (match-heavy path)."""
    sentences = []
    for group in range(0, min(len(terms), 24), 4):
        chunk = terms[group:group + 4]
        forms = " و ".join(t["forms"][0] for t in chunk)
        sentences.append(f"بیمار دارای {forms} ارزیابی شد")
        sentences.append(f"patient shows {chunk[0]['canonical']} under review")
    return sentences or ["بیمار بررسی شد"]


def write_dictionary(root: Path, terms: list[dict]) -> None:
    """Write the synthetic dictionary (consolidated layout) into ``root``."""
    knowledge = root / "medical_knowledge"
    knowledge.mkdir(parents=True, exist_ok=True)
    (knowledge / "medical_dictionary.json").write_text(json.dumps({
        "version": 1,
        "terms": terms,
    }, ensure_ascii=False), encoding="utf-8")


def _build(root: Path) -> MedicalFST:
    return MedicalFST(root)


def benchmark_size(size: int, workdir: Path, repeats: int) -> dict:
    import tempfile
    tmp = tempfile.mkdtemp(prefix=f"matcher-bench-{size}-", dir=str(workdir))
    root = Path(tmp)
    terms = _synthetic_terms(size)
    write_dictionary(root, terms)

    # --- startup build time (3 builds, cold each: JSON parse + automaton).
    # Built matchers are kept alive so deallocating one cannot pollute the
    # next timed build.
    build_times = []
    built = []
    for _ in range(3):
        start = time.perf_counter()
        built.append(_build(root))
        build_times.append(time.perf_counter() - start)
    built.clear()

    # --- peak traced memory for one build
    tracemalloc.start()
    memory_matcher = _build(root)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    sentences = [normalize_text(s) for s in NO_MATCH_SENTENCES]
    sentences += [normalize_text(s) for s in _match_sentences(terms)]

    # Warmup (first call may lazily touch automaton internals).
    for sentence in sentences:
        memory_matcher.canonicalize(sentence)

    per_sentence: dict[str, list[float]] = {}
    for sentence in sentences:
        timings: list[float] = []
        for _ in range(repeats):
            start = time.perf_counter()
            memory_matcher.canonicalize(sentence)
            timings.append(time.perf_counter() - start)
        per_sentence[sentence] = timings

    all_timings = [t for ts in per_sentence.values() for t in ts]
    return {
        "terms": size,
        "rules_loaded": len(memory_matcher.rules),
        "engine": memory_matcher.engine,
        "build_time_seconds": {
            "min": round(min(build_times), 6),
            "median": round(statistics.median(build_times), 6),
        },
        "build_peak_traced_memory_mb": round(peak / 1_048_576, 3),
        "match_repeats_per_sentence": repeats,
        "latency_us_per_sentence": {
            sentence: {
                "mean": round(statistics.mean(ts) * 1e6, 2),
                "min": round(min(ts) * 1e6, 2),
                "max": round(max(ts) * 1e6, 2),
            }
            for sentence, ts in per_sentence.items()
        },
        "latency_us_summary": {
            "mean": round(statistics.mean(all_timings) * 1e6, 2),
            "p50": round(statistics.median(all_timings) * 1e6, 2),
            "max": round(max(all_timings) * 1e6, 2),
            "mean_per_character": round(
                statistics.mean(all_timings) * 1e6
                / statistics.mean(len(s) for s in sentences), 3
            ),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=ROOT / "results.json")
    parser.add_argument("--repeats", type=int, default=100,
                        help="canonicalize calls per sentence (default 100)")
    parser.add_argument("--sizes", type=int, nargs="+",
                        default=[100, 500, 1000, 2000])
    args = parser.parse_args()

    import tempfile
    workdir = Path(tempfile.mkdtemp(prefix="matcher-bench-root-"))

    results = []
    for size in args.sizes:
        result = benchmark_size(size, workdir, args.repeats)
        results.append(result)
        summary = result["latency_us_summary"]
        print(
            f"{result['terms']:>5} terms | {result['rules_loaded']:>5} rules | "
            f"build min {result['build_time_seconds']['min']*1000:7.2f} ms | "
            f"match mean {summary['mean']:8.2f} us | "
            f"p50 {summary['p50']:8.2f} us | "
            f"peak mem {result['build_peak_traced_memory_mb']:.2f} MB"
        )

    payload = {
        "engine": results[0]["engine"] if results else None,
        "sizes": args.sizes,
        "repeats_per_sentence": args.repeats,
        "results": results,
    }
    args.out.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nwritten: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
