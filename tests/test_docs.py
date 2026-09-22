"""The documentation must not drift away from the code it describes.

Every count and command in README.md was correct when written. Without a
test, the first dictionary edit makes it quietly wrong - which is exactly
how the previous changelog ended up claiming "942 terms, 98 vocab entries,
315 passed" against a repo that had none of those numbers.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")
KNOWLEDGE = ROOT / "medical_knowledge"


def _dictionary():
    return json.loads(
        (KNOWLEDGE / "medical_dictionary.json").read_text(encoding="utf-8"))


def _vocabulary():
    return json.loads(
        (KNOWLEDGE / "speechmatics_additional_vocab.json")
        .read_text(encoding="utf-8"))


# ----------------------------------------------------------- stated counts

def test_readme_dictionary_term_count_is_current():
    stated = int(re.search(r"\*\*(\d+) terms\*\*", README).group(1))
    assert stated == len(_dictionary()["terms"])


def test_readme_vocabulary_entry_count_is_current():
    stated = int(re.search(r"\*\*(\d+) entries\*\*", README).group(1))
    assert stated == len(_vocabulary())


def test_readme_fixture_count_is_current():
    from benchmark.dataset import ALL_CASES

    stated = int(re.search(r"(\d+) nursing fixtures", README).group(1))
    assert stated == len(ALL_CASES)


def test_readme_layout_counts_match_the_stated_counts():
    """The project-layout block repeats the same numbers; they must agree."""
    layout = re.search(r"medical_dictionary\.json\s+(\d+) terms", README)
    assert layout and int(layout.group(1)) == len(_dictionary()["terms"])
    vocab = re.search(r"(\d+) generated vocab entries", README)
    assert vocab and int(vocab.group(1)) == len(_vocabulary())


# ------------------------------------------------- stated benchmark results

def test_readme_benchmark_table_matches_the_measured_comparison():
    """The accuracy table must be the numbers the benchmark actually
    produced, not aspirational ones."""
    comparison = json.loads(
        (ROOT / "benchmark" / "results_comparison.json")
        .read_text(encoding="utf-8"))
    base, cur = comparison["baseline"], comparison["current"]
    cases = comparison["dataset_cases"]

    assert f"{base['exact_match']}/{cases}" in README
    assert f"{cur['exact_match']}/{cases}" in README

    for stage, label in [
        ("medical_canonicalization", "medical canonicalization"),
        ("number_time_unit", "number / time / unit"),
        ("grammar_format", "grammar / format"),
        ("paragraph", "full paragraphs"),
    ]:
        row = re.search(
            rf"\| {re.escape(label)} \| ([\d.]+)% \| ([\d.]+)% \|", README)
        assert row, f"missing row for {label}"
        assert abs(float(row.group(1)) - base["by_stage"][stage] * 100) < 0.1
        assert abs(float(row.group(2)) - cur["by_stage"][stage] * 100) < 0.1


# ---------------------------------------------------- documented interfaces

def _documented_subcommands():
    return set(re.findall(r"^\| `([a-z-]+)` \|", README, re.MULTILINE))


def test_every_documented_subcommand_exists():
    sys.path.insert(0, str(ROOT))
    from scripts.swiftmedics_tools import build_parser

    parser = build_parser()
    actions = [a for a in parser._actions if getattr(a, "choices", None)]
    real = set(actions[0].choices)
    documented = _documented_subcommands() & real | (
        _documented_subcommands() - set()
    )
    # Only check names that look like subcommands, not flags.
    for name in _documented_subcommands():
        if name.startswith("--"):
            continue
        assert name in real, f"README documents unknown subcommand {name!r}"


def test_every_documented_flag_exists():
    help_text = subprocess.run(
        [sys.executable, str(ROOT / "app.py"), "--help"],
        capture_output=True, text=True, cwd=ROOT,
    ).stdout
    flags = set(re.findall(r"`(--[a-z-]+)", README))
    for flag in flags:
        assert flag in help_text, f"README documents unknown flag {flag!r}"


@pytest.mark.parametrize("wrapper", [
    "install.ps1", "run_fa.ps1", "run_en.ps1", "run_matcher_benchmark.ps1",
])
def test_every_referenced_wrapper_exists(wrapper):
    assert wrapper in README
    assert (ROOT / "scripts" / wrapper).exists()


def test_readme_commands_are_powershell_flavoured():
    """The audience runs Windows PowerShell; shell snippets must use the
    venv's Windows interpreter path, not a bare posix `python`."""
    for block in re.findall(r"```powershell\n(.*?)```", README, re.DOTALL):
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            assert not line.startswith("python "), (
                f"use .\\.venv\\Scripts\\python.exe, not bare python: {line!r}"
            )
            assert "/" not in line.split("#")[0] or line.startswith("cd "), (
                f"use backslash paths in PowerShell blocks: {line!r}"
            )


# ------------------------------------------------------------- no staleness

def test_docs_do_not_quote_a_stale_test_count():
    """A hardcoded 'N passed' goes stale the moment a test is added."""
    stated = [int(n) for n in re.findall(r"\*\*(\d+) tests", README)]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--collect-only"],
        capture_output=True, text=True, cwd=ROOT,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    assert match, result.stdout[-2000:]
    actual = int(match.group(1))
    for value in stated:
        assert value == actual, f"README says {value} tests, actual {actual}"
