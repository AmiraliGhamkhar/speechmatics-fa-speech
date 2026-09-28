# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller one-folder build for the SwiftMedics desktop app.

Build from the repository root on Windows with:
    pyinstaller --clean --noconfirm packaging\swiftmedics.spec
"""

from pathlib import Path

#: Marker used to confirm we really found the repository root.
_ENTRY_POINT = "desktop_app.py"


def _find_root() -> Path:
    r"""Locate the repository root however this spec was invoked.

    PyInstaller injects the path globals ``SPEC`` / ``SPECPATH`` (and sets
    ``__file__``), but their exact meaning has shifted between releases, and
    the spec may be run from the repository root or from ``packaging\``.
    Assuming one of them put the analysis one level off and surfaced only as
    ``ERROR: script ...\desktop_app.py not found``, so the root is resolved
    by walking up from every plausible starting point and confirming the
    entry point is actually there.
    """
    starts = []
    for name in ("__file__", "SPEC", "SPECPATH"):
        value = globals().get(name)
        if value:
            starts.append(Path(value).resolve())
    starts.append(Path.cwd().resolve())

    for start in starts:
        for directory in (start, *start.parents):
            if (directory / _ENTRY_POINT).is_file():
                return directory

    raise SystemExit(
        "Could not locate the SwiftMedics repository root: no %s found above %s. "
        "Run pyinstaller from the repository root."
        % (_ENTRY_POINT, ", ".join(str(start) for start in starts))
    )


ROOT = _find_root()


def data(path: str, dest: str):
    return (str(ROOT / path), dest)


a = Analysis(
    [str(ROOT / "desktop_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        data("medical_knowledge", "medical_knowledge"),
        data(".env.example", "."),
    ],
    hiddenimports=[
        "speechmatics.rt",
        "pyaudio",
        "pyperclip",
        "pyautogui",
        "dotenv",
        "ahocorasick",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest",
        "tests",
        "benchmark",
    ],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SwiftMedics",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX packing is a well-known source of antivirus false positives, and a
    # packed binary cannot be reliably code-signed. Hospitals and EMR
    # environments block unsigned/unknown exes aggressively, so the bundle
    # ships unpacked and can be signed instead.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="SwiftMedics",
)
