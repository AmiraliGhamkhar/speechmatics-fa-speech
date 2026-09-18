"""Shell entry-point contracts (checked without executing PowerShell)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_run_ps1_actually_runs_the_app():
    """Regression: run.ps1 used to install dependencies and only PRINT the
    run command, while the README described it as "install + run"."""
    script = (ROOT / "scripts" / "run.ps1").read_text(encoding="utf-8")
    assert "app.py" in script
    assert script.rstrip().endswith('& .\\.venv\\Scripts\\python.exe app.py --language fa @args')


def test_run_ps1_passes_extra_arguments_through():
    script = (ROOT / "scripts" / "run.ps1").read_text(encoding="utf-8")
    assert "@args" in script


def test_install_and_run_scripts_both_install_dependencies():
    for name in ("install.ps1", "run.ps1"):
        script = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "requirements.txt" in script
