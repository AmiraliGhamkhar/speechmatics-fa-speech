"""Shell entry-point contracts (checked without executing PowerShell)."""

import os
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


# ------------------------------------------------------- PyInstaller spec


class _SpecStub:
    """Stands in for the Analysis/PYZ/EXE/COLLECT objects PyInstaller injects."""

    def __init__(self, *args, **kwargs):
        pass

    def __getattr__(self, name):
        return _SpecStub()

    def __call__(self, *args, **kwargs):
        return _SpecStub()


def test_pyinstaller_spec_locates_the_repository_root():
    """Regression: the Windows build died with "script ... not found".

    The spec resolved its root from PyInstaller's injected path globals, whose
    exact meaning has shifted between releases, which put the analysis one
    level off. The spec now walks up from every plausible starting point and
    confirms the entry point is really there, so this exercises it the way
    PyInstaller would: from the repository root, from ``packaging/``, and with
    only some of the globals present.
    """
    spec = ROOT / "packaging" / "swiftmedics.spec"
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
    spec = (ROOT / "packaging" / "swiftmedics.spec").read_text(encoding="utf-8")
    assert 'data("medical_knowledge", "medical_knowledge")' in spec
    assert 'str(ROOT / "desktop_app.py")' in spec
    # Packed binaries trip hospital antivirus and cannot be code-signed.
    assert "upx=True" not in spec
