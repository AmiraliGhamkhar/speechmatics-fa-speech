# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-folder build for the SwiftMedics desktop app.

Build from the repository root on Windows with:
    pyinstaller --clean --noconfirm packaging\swiftmedics.spec
"""

from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent.parent


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
