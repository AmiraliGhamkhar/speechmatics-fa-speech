"""Entry-point contracts for the consolidated tooling.

The repository used to carry one shell script per task, each with its own
copy of the logic. Everything now lives in ``scripts/swiftmedics_tools.py``
and the legacy ``.ps1``/``.sh`` files are thin forwarding wrappers. These
tests check both halves of that contract:

1. every legacy entry point still exists and still does its old job;
2. no wrapper re-implements logic (they must forward, not duplicate).
"""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
TOOL = SCRIPTS / "swiftmedics_tools.py"

#: legacy wrapper -> the subcommand it must forward to
LEGACY_WRAPPERS = {
    "install.ps1": "install",
    "run.ps1": "run-fa",
    "run_fa.ps1": "run-fa",
    "run_en.ps1": "run-en",
    "run_no_vocab.ps1": "run-fa",
    "run_benchmark.ps1": "benchmark",
    "run_matcher_benchmark.ps1": "benchmark-matcher",
    "test_injector.ps1": "test-injector",
    "run.sh": "run-fa",
}


# ---------------------------------------------------- consolidated tool

def test_single_tool_exists():
    assert TOOL.exists()


def test_tool_help_works_standalone():
    """--help must work without any optional dependency."""
    result = subprocess.run(
        [sys.executable, str(TOOL), "--help"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    for command in ("install", "run", "run-fa", "run-en", "benchmark",
                    "benchmark-matcher", "export-vocab", "check-vocab",
                    "test-injector"):
        assert command in result.stdout, command


def test_tool_exposes_every_documented_subcommand():
    sys.path.insert(0, str(ROOT))
    from scripts.swiftmedics_tools import build_parser

    parser = build_parser()
    actions = [
        a for a in parser._actions if hasattr(a, "choices") and a.choices
    ]
    commands = set(actions[0].choices)
    assert {
        "install", "run", "run-fa", "run-en", "benchmark",
        "benchmark-matcher", "export-vocab", "check-vocab",
        "audit-dictionary", "test-injector",
    } <= commands


def test_check_vocab_subcommand_passes():
    result = subprocess.run(
        [sys.executable, str(TOOL), "check-vocab"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CHECK: OK" in result.stdout


def test_audit_dictionary_subcommand_passes():
    result = subprocess.run(
        [sys.executable, str(TOOL), "audit-dictionary"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr


# ------------------------------------------------------ legacy wrappers

@pytest.mark.parametrize("name", sorted(LEGACY_WRAPPERS))
def test_legacy_wrapper_still_exists(name):
    assert (SCRIPTS / name).exists(), f"{name} must stay for compatibility"


@pytest.mark.parametrize("name,subcommand", sorted(LEGACY_WRAPPERS.items()))
def test_legacy_wrapper_forwards_to_the_single_tool(name, subcommand):
    script = (SCRIPTS / name).read_text(encoding="utf-8")
    assert "swiftmedics_tools.py" in script, f"{name} must forward"
    assert subcommand in script, f"{name} must forward to {subcommand}"


@pytest.mark.parametrize("name", sorted(LEGACY_WRAPPERS))
def test_legacy_wrapper_does_not_duplicate_logic(name):
    """A wrapper must not re-implement what the tool does.

    Regression guard for the old layout, where run.ps1/install.ps1 each
    carried their own venv + pip + .env bootstrap and drifted apart.
    """
    script = (SCRIPTS / name).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    )
    assert "pip install" not in body, f"{name} must not install directly"
    assert "-m venv" not in body, f"{name} must not create the venv directly"


def test_install_and_run_wrappers_still_install_dependencies():
    """Historical contract: both entry points bootstrap the environment.

    They now do it through the tool's `install` subcommand (which is what
    reads requirements.txt) instead of each running pip themselves.
    """
    for name in ("install.ps1", "run.ps1"):
        script = (SCRIPTS / name).read_text(encoding="utf-8")
        assert "swiftmedics_tools.py install" in script


def test_run_ps1_actually_runs_the_app():
    """Regression: run.ps1 used to install dependencies and only PRINT the
    run command, while the README described it as "install + run".

    It must still end by actually launching the app - now via the tool's
    run-fa subcommand rather than a hardcoded `app.py --language fa`.
    """
    script = (SCRIPTS / "run.ps1").read_text(encoding="utf-8")
    assert script.rstrip().endswith(
        "& .\\.venv\\Scripts\\python.exe scripts\\swiftmedics_tools.py "
        "run-fa @args"
    )


def test_run_ps1_passes_extra_arguments_through():
    script = (SCRIPTS / "run.ps1").read_text(encoding="utf-8")
    assert "@args" in script


def test_export_additional_vocab_shim_still_works():
    """The historical python entry point keeps working, including --check."""
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "export_additional_vocab.py"), "--check"],
        capture_output=True, text=True, cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CHECK: OK" in result.stdout


# --------------------------------- case-insensitive canonical duplicates

def test_audit_detects_only_the_intentional_case_duplicate():
    """The real dictionary keeps exactly the intentional ``Mg`` (magnesium)
    / ``mg`` (milligram) pair whose case is clinically load-bearing; nothing
    else may differ from another canonical by capitalization only."""
    from scripts.swiftmedics_tools import audit_dictionary

    report = audit_dictionary()
    assert report["casefold_duplicate_canonicals"] == [["Mg", "mg"]]
    assert report["unexpected_casefold_duplicates"] == []


def test_audit_blocks_a_synthetic_case_only_duplicate(tmp_path):
    """A new case-only canonical duplicate ("BP" vs "bp") is a data bug:
    the matcher is case-insensitive, so their forms silently compete - the
    audit must make it blocking, not decorative."""
    import json as json_module

    from scripts.swiftmedics_tools import audit_dictionary, cmd_audit_dictionary

    dictionary = {
        "version": 1,
        "metadata": {"entry_count": 2},
        "terms": [
            {"id": "t1", "canonical": "BP", "type": "abbreviation",
             "tier": "abbreviation", "forms": ["فشار خون"]},
            {"id": "t2", "canonical": "bp", "type": "abbreviation",
             "tier": "abbreviation", "forms": ["بی پی"]},
        ],
    }
    path = tmp_path / "medical_dictionary.json"
    path.write_text(json_module.dumps(dictionary, ensure_ascii=False),
                    encoding="utf-8")

    report = audit_dictionary(path)
    assert report["unexpected_casefold_duplicates"] == [["BP", "bp"]]

    # thread it through the CLI: the subcommand must fail on the unexpected
    # group (a stub argparse namespace is enough for the human-readable path)
    import argparse
    import scripts.swiftmedics_tools as tools

    original = tools.audit_dictionary
    tools.audit_dictionary = lambda path=tools.DICTIONARY_PATH: report
    try:
        code = cmd_audit_dictionary(argparse.Namespace(json=False))
    finally:
        tools.audit_dictionary = original
    assert code == 1
