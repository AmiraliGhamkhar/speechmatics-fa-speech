"""SwiftMedics consolidated developer tool.

ONE implementation for every repository task. The legacy ``scripts/*.ps1``
and ``scripts/run.sh`` entry points are kept as thin forwarding wrappers
around this file, so no business logic is duplicated anywhere.

Subcommands::

    install             create .venv, install requirements, seed .env
    run                 run app.py (extra args are forwarded)
    run-fa              run app.py --language fa
    run-en              run app.py --language en
    benchmark           run the full nursing normalization benchmark suite
    benchmark-matcher   matcher build/latency/memory benchmark
    export-vocab        regenerate speechmatics_additional_vocab.json
    check-vocab         verify the vocabulary artifact is in sync
    audit-dictionary    report dictionary health (duplicates, conflicts, ...)
    test-injector       standalone injector smoke test

Every subcommand returns a POSIX exit code; ``--help`` works without any
optional dependency installed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

VOCAB_PATH = ROOT / "medical_knowledge" / "speechmatics_additional_vocab.json"
DICTIONARY_PATH = ROOT / "medical_knowledge" / "medical_dictionary.json"


# --------------------------------------------------------------- helpers


def _venv_python() -> Path:
    """The interpreter the repository scripts use, if a .venv exists."""
    candidates = (
        ROOT / ".venv" / "Scripts" / "python.exe",   # Windows
        ROOT / ".venv" / "bin" / "python",           # POSIX
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def _run(cmd: list[str]) -> int:
    print("+ " + " ".join(str(c) for c in cmd))
    return subprocess.call([str(c) for c in cmd], cwd=str(ROOT))


# -------------------------------------------------------------- install


def cmd_install(args: argparse.Namespace) -> int:
    """Create the virtualenv, install requirements and seed ``.env``."""
    venv = ROOT / ".venv"
    if not venv.exists():
        code = _run([sys.executable, "-m", "venv", str(venv)])
        if code:
            return code
    python = _venv_python()
    code = _run([python, "-m", "pip", "install", "--upgrade", "pip"])
    if code:
        return code
    code = _run([python, "-m", "pip", "install", "-r",
                 str(ROOT / "requirements.txt")])
    if code:
        return code
    env, example = ROOT / ".env", ROOT / ".env.example"
    if not env.exists() and example.exists():
        env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"created {env}")
    print("Installed. Edit .env and set SPEECHMATICS_API_KEY.")
    return 0


# ------------------------------------------------------------------- run


def _run_app(language: str | None, extra: list[str]) -> int:
    cmd: list[str] = [_venv_python(), str(ROOT / "app.py")]
    if language and "--language" not in extra:
        cmd += ["--language", language]
    cmd += extra
    return _run(cmd)


def cmd_run(args: argparse.Namespace) -> int:
    return _run_app(None, args.extra)


def cmd_run_fa(args: argparse.Namespace) -> int:
    return _run_app("fa", args.extra)


def cmd_run_en(args: argparse.Namespace) -> int:
    return _run_app("en", args.extra)


# ------------------------------------------------------------ vocabulary


def _derive_vocab():
    from speechmatics_test.matcher import MedicalMatcher
    matcher = MedicalMatcher(ROOT)
    return matcher, matcher.additional_vocab


def cmd_export_vocab(args: argparse.Namespace) -> int:
    """Regenerate the committed Speechmatics vocabulary artifact."""
    matcher, vocab = _derive_vocab()
    print(f"dictionary terms           : {len(matcher.terms)}")
    print(f"speechmatics-eligible vocab: {len(vocab)} (speechmatics: true only)")
    VOCAB_PATH.write_text(
        json.dumps(vocab, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote: {VOCAB_PATH}")
    return 0


def cmd_check_vocab(args: argparse.Namespace) -> int:
    """Verify the committed artifact still matches the dictionary."""
    matcher, vocab = _derive_vocab()
    print(f"dictionary terms           : {len(matcher.terms)}")
    print(f"speechmatics-eligible vocab: {len(vocab)} (speechmatics: true only)")
    current = json.loads(VOCAB_PATH.read_text(encoding="utf-8"))
    if current == vocab:
        print("CHECK: OK (speechmatics_additional_vocab.json is in sync)")
        return 0
    print("CHECK: FAILED (artifact differs from the derived vocabulary; "
          "run scripts/swiftmedics_tools.py export-vocab to regenerate)")
    return 1


# ------------------------------------------------------ dictionary audit


def audit_dictionary(path: Path = DICTIONARY_PATH) -> dict:
    """Structural health report for the medical dictionary.

    Pure data in, pure data out: the same report backs the CLI subcommand,
    the benchmark metadata and the regression tests.
    """
    import collections
    from speechmatics_test.matcher import (
        TERM_TYPES, TIER_RANK, casefold_preserving,
    )
    from speechmatics_test.text import normalize_text

    data = json.loads(path.read_text(encoding="utf-8"))
    terms = data.get("terms", [])

    ids = collections.Counter(t.get("id") for t in terms)
    canonicals = collections.Counter(
        (t.get("canonical") or "").strip() for t in terms
    )

    by_form: dict[str, set[str]] = collections.defaultdict(set)
    empty_forms = []
    for term in terms:
        for form in term.get("forms", []):
            folded = casefold_preserving(normalize_text(form))
            if not folded:
                empty_forms.append((term.get("id"), form))
                continue
            by_form[folded].add(term.get("canonical"))

    conflicting = sorted(
        (form, sorted(targets))
        for form, targets in by_form.items() if len(targets) > 1
    )

    # Case-only duplicate canonicals: "chest X-ray" vs "chest x-ray" are one
    # concept spelled two ways, so the same Persian form resolves to a
    # different output depending on tier order. This is a DEFECT, unlike an
    # ordinary duplicate canonical (which is exact and already reported).
    # The documented exceptions in scripts/_dictionary_fixes.py are pairs
    # where case genuinely distinguishes two concepts (Mg vs mg).
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _dictionary_fixes import DISTINCT_CASE_CANONICALS

    case_groups: dict[str, set[str]] = collections.defaultdict(set)
    for term in terms:
        canonical = (term.get("canonical") or "").strip()
        if canonical:
            case_groups[canonical.casefold()].add(canonical)
    case_only_duplicates = sorted(
        (folded, sorted(spellings))
        for folded, spellings in case_groups.items()
        if len(spellings) > 1 and folded not in DISTINCT_CASE_CANONICALS
    )
    intentional = sorted(
        folded for folded in DISTINCT_CASE_CANONICALS
        if len(case_groups.get(folded, ())) > 1
    )

    # A raw "conflicting alias forms" count is not actionable: most of these
    # collisions are BENIGN - an abbreviation term and its expansion term
    # listing each other ("ECG" <-> "electrocardiogram"), where whichever
    # canonical wins is a spelling choice, not a meaning change. The ones
    # that matter are CROSS-CONCEPT: one form claimed by terms that are not
    # linked by canonical/alias at all ("pe" = physical examination vs
    # pulmonary embolism), where arbitration silently picks a diagnosis.
    folded_forms = {
        term.get("canonical"): {
            casefold_preserving(normalize_text(f)) for f in term.get("forms", [])
        }
        for term in terms
    }

    def _same_concept(left: str, right: str) -> bool:
        """True when each canonical appears among the other's forms."""
        left_folded = casefold_preserving(normalize_text(left))
        right_folded = casefold_preserving(normalize_text(right))
        return (
            left_folded == right_folded
            or left_folded in folded_forms.get(right, ())
            or right_folded in folded_forms.get(left, ())
        )

    cross_concept = [
        (form, targets) for form, targets in conflicting
        if not all(
            _same_concept(a, b)
            for i, a in enumerate(targets) for b in targets[i + 1:]
        )
    ]

    invalid = []
    for term in terms:
        if term.get("type") not in TERM_TYPES:
            invalid.append((term.get("id"), f"type={term.get('type')!r}"))
        if term.get("tier") not in TIER_RANK:
            invalid.append((term.get("id"), f"tier={term.get('tier')!r}"))
        if not term.get("forms") and not term.get("speechmatics"):
            invalid.append((term.get("id"), "empty forms on a non-vocab term"))

    metadata_count = (data.get("metadata") or {}).get("entry_count")
    speechmatics_count = sum(1 for t in terms if t.get("speechmatics"))
    vocab_count = (
        len(json.loads(VOCAB_PATH.read_text(encoding="utf-8")))
        if VOCAB_PATH.exists() else None
    )

    return {
        "actual_terms": len(terms),
        "metadata_entry_count": metadata_count,
        "metadata_matches_actual": metadata_count == len(terms),
        "duplicate_ids": sorted(k for k, v in ids.items() if v > 1),
        "duplicate_canonicals": sorted(k for k, v in canonicals.items() if v > 1),
        "case_only_duplicate_canonicals": case_only_duplicates,
        "intentional_case_distinctions": intentional,
        "conflicting_alias_forms": len(conflicting),
        "conflicting_alias_examples": conflicting[:20],
        "same_concept_alias_forms": len(conflicting) - len(cross_concept),
        "cross_concept_alias_forms": len(cross_concept),
        "cross_concept_alias_examples": cross_concept[:20],
        "empty_forms": empty_forms,
        "invalid_entries": invalid,
        "speechmatics_entries": speechmatics_count,
        "vocabulary_entries": vocab_count,
        "vocabulary_matches_speechmatics_flag":
            vocab_count == speechmatics_count,
    }


def cmd_audit_dictionary(args: argparse.Namespace) -> int:
    report = audit_dictionary()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print("MEDICAL DICTIONARY AUDIT")
        print("-" * 60)
        print(f"actual terms                 : {report['actual_terms']}")
        print(f"metadata entry_count         : {report['metadata_entry_count']}"
              f" ({'consistent' if report['metadata_matches_actual'] else 'MISMATCH'})")
        print(f"speechmatics vocabulary flags: {report['speechmatics_entries']}")
        print(f"vocabulary artifact entries  : {report['vocabulary_entries']}")
        print(f"duplicate IDs                : {len(report['duplicate_ids'])}")
        print(f"duplicate canonicals         : {len(report['duplicate_canonicals'])}")
        print(f"case-only duplicate canonicals: "
              f"{len(report['case_only_duplicate_canonicals'])}")
        for folded, spellings in report["case_only_duplicate_canonicals"]:
            print(f"    {folded}: {spellings}")
        if report["intentional_case_distinctions"]:
            print(f"  (allowed case distinctions : "
                  f"{report['intentional_case_distinctions']})")
        print(f"conflicting alias forms      : {report['conflicting_alias_forms']}")
        print(f"  same concept (benign)      : {report['same_concept_alias_forms']}")
        print(f"  cross concept (review)     : {report['cross_concept_alias_forms']}")
        print(f"invalid entries              : {len(report['invalid_entries'])}")
        print(f"empty forms                  : {len(report['empty_forms'])}")
    blocking = (
        report["duplicate_ids"]
        or report["duplicate_canonicals"]
        or report["case_only_duplicate_canonicals"]
        or report["invalid_entries"]
        or not report["metadata_matches_actual"]
    )
    return 1 if blocking else 0


# -------------------------------------------------------------- benchmark


def cmd_benchmark(args: argparse.Namespace) -> int:
    from benchmark.run_benchmark import main as run_nursing_benchmark
    return run_nursing_benchmark(
        out=args.out, repeats=args.repeats, label=args.label
    )


def cmd_benchmark_matcher(args: argparse.Namespace) -> int:
    cmd = [_venv_python(), str(ROOT / "benchmark" / "benchmark_matcher.py"),
           "--repeats", str(args.repeats)]
    if args.sizes:
        cmd += ["--sizes", *[str(s) for s in args.sizes]]
    if args.out:
        cmd += ["--out", str(args.out)]
    return _run(cmd)


# --------------------------------------------------------- test injector


def cmd_test_injector(args: argparse.Namespace) -> int:
    """Standalone injector smoke test (identical to the legacy .ps1)."""
    from injector import TextInjector
    injector = TextInjector()
    print("Click the target text field.")
    try:
        input("Press ENTER when ready... ")
    except EOFError:
        print("no interactive stdin; aborting the smoke test")
        return 1
    ok = injector.paste_text(
        "SwiftMedics test | فارسی | CT scan | lesion | 140/90 | IV"
    )
    print("paste result:", ok)
    return 0 if ok else 1


# ------------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="swiftmedics_tools.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("install", help="create .venv and install requirements"
                   ).set_defaults(func=cmd_install)

    for name, func, help_text in (
        ("run", cmd_run, "run app.py (extra args forwarded)"),
        ("run-fa", cmd_run_fa, "run app.py --language fa"),
        ("run-en", cmd_run_en, "run app.py --language en"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("extra", nargs=argparse.REMAINDER,
                       help="arguments forwarded to app.py")
        p.set_defaults(func=func)

    p = sub.add_parser("benchmark",
                       help="nursing normalization benchmark suite")
    p.add_argument("--out", type=Path,
                   default=ROOT / "benchmark" / "results_current.json")
    p.add_argument("--repeats", type=int, default=50)
    p.add_argument("--label", default="current")
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser("benchmark-matcher",
                       help="matcher build/latency/memory benchmark")
    p.add_argument("--repeats", type=int, default=100)
    p.add_argument("--sizes", type=int, nargs="+")
    p.add_argument("--out", type=Path)
    p.set_defaults(func=cmd_benchmark_matcher)

    sub.add_parser("export-vocab", help="regenerate the vocabulary artifact"
                   ).set_defaults(func=cmd_export_vocab)
    sub.add_parser("check-vocab", help="verify the vocabulary artifact"
                   ).set_defaults(func=cmd_check_vocab)

    p = sub.add_parser("audit-dictionary", help="dictionary health report")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_audit_dictionary)

    sub.add_parser("test-injector", help="standalone injector smoke test"
                   ).set_defaults(func=cmd_test_injector)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
