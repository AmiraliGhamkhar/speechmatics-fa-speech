"""Shell entry-point contracts (checked without executing PowerShell).

Covers the scripts that survive the consolidation:
``run.ps1``/``install.ps1`` (development), ``build_windows.ps1`` (developer
PyInstaller fallback), and ``build_hospital_demo.ps1`` (the single hospital
demo release path). The PyInstaller spec relocation is pinned as well.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _script(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_run_ps1_actually_runs_the_app():
    """Regression: run.ps1 used to install dependencies and only PRINT the
    run command, while the README described it as "install + run"."""
    script = _script("run.ps1")
    assert "app.py" in script
    assert script.rstrip().endswith("& .\\.venv\\Scripts\\python.exe app.py --language fa @args")


def test_run_ps1_passes_extra_arguments_through():
    assert "@args" in _script("run.ps1")


def test_install_and_run_scripts_both_install_dependencies():
    for name in ("install.ps1", "run.ps1"):
        assert "requirements.txt" in _script(name)


# ------------------------------------------------- PyInstaller spec location


class _SpecStub:
    """Stands in for the Analysis/PYZ/EXE/COLLECT objects PyInstaller injects."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return _SpecStub()

    def __call__(self, *args, **kwargs):
        return _SpecStub()


def test_pyinstaller_spec_lives_next_to_its_build_script():
    """The developer/fallback spec is scripts/swiftmedics-pyinstaller.spec;
    the old packaging/ location must stay gone to avoid two divergent specs."""
    assert (ROOT / "scripts" / "swiftmedics-pyinstaller.spec").is_file()
    assert not (ROOT / "packaging").exists() or not any((ROOT / "packaging").iterdir())


def test_pyinstaller_spec_locates_the_repository_root():
    """Regression: the Windows build died with "script ... not found".
    The spec resolved its root from PyInstaller's injected path globals, whose
    exact meaning has shifted between releases. The spec now walks up from
    every plausible starting point and confirms the entry point is really
    there; this exercises it from the repository root, from ``scripts/``, and
    with only some of the globals present."""
    spec = ROOT / "scripts" / "swiftmedics-pyinstaller.spec"
    source = spec.read_text(encoding="utf-8")

    def execute(cwd, injected):
        namespace = dict(injected)
        namespace.update({
            name: _SpecStub
            for name in ("Analysis", "PYZ", "EXE", "COLLECT")
        })
        previous = Path.cwd()
        os.chdir(cwd)
        try:
            exec(compile(source, str(spec), "exec"), namespace)
        finally:
            os.chdir(previous)
        return namespace["ROOT"]

    invocations = [
        (ROOT, {"SPEC": str(spec), "SPECPATH": str(spec.parent)}),
        (ROOT, {"SPECPATH": str(ROOT)}),
        (spec.parent, {"SPEC": str(spec), "SPECPATH": str(spec.parent)}),
        (ROOT, {}),
    ]
    for cwd, injected in invocations:
        assert execute(cwd, injected) == ROOT, injected


def test_pyinstaller_spec_bundles_the_runtime_data():
    """The bundled app must carry the dictionary it reads at runtime."""
    spec = (ROOT / "scripts" / "swiftmedics-pyinstaller.spec").read_text(encoding="utf-8")
    assert 'data("medical_knowledge", "medical_knowledge")' in spec
    assert 'str(ROOT / "desktop_app.py")' in spec
    # Packed binaries trip hospital antivirus and cannot be code-signed.
    assert "upx=True" not in spec


# ------------------------------------------------------- hospital demo build


def test_hospital_demo_script_is_the_single_nuitka_demo_path():
    script = _script("build_hospital_demo.ps1")
    assert "nuitka" in script.lower()
    # The stamp must be generated for the build and removed afterwards.
    assert "set_demo_expiry.py" in script
    assert "demo_build_stamp.py" in script
    assert "KeepStamp" in script
    # Medical data and the no-console GUI requirement.
    assert "medical_knowledge" in script
    assert "--windows-console-mode=disable" in script
    # The secret-safety gate must run and must never print the secret.
    assert "SPEECHMATICS_API_KEY" in script
    assert "SECRET HIT" in script
    assert "Write-Host $envKey" not in script
    assert "Write-Host $secretScan" not in script
    # Exactly ONE script may invoke Nuitka: the hospital demo path. (Prose
    # mentions in the fallback script are fine; actual calls are not.)
    nuitka_scripts = [
        p.name
        for p in sorted((ROOT / "scripts").glob("*.ps1"))
        if "python -m nuitka" in p.read_text(encoding="utf-8").lower()
    ]
    assert nuitka_scripts == ["build_hospital_demo.ps1"]


def test_build_windows_script_is_fallback_only():
    script = _script("build_windows.ps1")
    assert "swiftmedics-pyinstaller.spec" in script
    assert "build_hospital_demo" in script
