<#
Build SwiftMedics with Nuitka: Python compiled to C, then to machine code.

WHY: a PyInstaller bundle contains your .pyc bytecode, which pyinstxtractor +
a decompiler can turn back into readable Python in minutes. Nuitka TRANSPILES
the program to C and compiles it, so the distributed files contain machine
code, not bytecode - there is nothing for a Python decompiler to read.

Run from the repository root on Windows PowerShell:

    Set-ExecutionPolicy -Scope Process Bypass
    .\scripts\build_windows_nuitka.ps1

Output:

    dist\SwiftMedics-nuitka\SwiftMedics.exe   (one-folder bundle)

Notes:

* The first build takes significantly longer than PyInstaller (a full C
  compile); later builds reuse the ccache-like object cache.
* PyAudio ships a compiled extension and is picked up automatically; the
  pure-Python pyahocorasick fallback keeps the medical matcher working even
  if a C extension is missed - the parity tests prove identical output.
* The runtime API key is still NOT embedded. Demo builds additionally use
  scripts\build_demo.ps1 for the compiled-in expiry + token broker.
* Code-sign the result before distributing to a hospital (unsigned exes are
  blocked or warned about by SmartScreen and enterprise policy).
#>
[CmdletBinding()]
param(
    [switch]$SkipInstall
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "Creating virtual environment..."
    py -3 -m venv .venv
}
if (-not (Test-Path $Python)) {
    throw "Could not find .venv\Scripts\python.exe. Install Python 3 and retry."
}

if (-not $SkipInstall) {
    Write-Host "Installing runtime requirements..."
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -r requirements.txt
    Write-Host "Installing Nuitka (compiler) and ordered-set (faster compiles)..."
    & $Python -m pip install --upgrade nuitka ordered-set
}

# Exclude the test/benchmark-only modules so nothing beyond the runtime ships.
$nuitkaArgs = @(
    "--standalone",
    "--assume-yes-for-downloads",
    "--output-dir=build-nuitka",
    "--output-filename=SwiftMedics.exe",
    "--include-package=speechmatics_test",
    "--include-package-data=medical_knowledge",
    "--include-data-files=.env.example=.env.example",
    "--noinclude-default-mode=error",
    "--windows-disable-console",
    "--windows-icon=NONE",
    "--disable-ccache=NO"
)

# Nuitka follows imports from desktop_app.py automatically; only extension
# modules loaded dynamically need to be named explicitly.
foreach ($pkg in @("speechmatics.rt", "pyaudio", "pyperclip", "ahocorasick")) {
    $nuitkaArgs += "--include-package=$pkg"
}

Write-Host "Compiling SwiftMedics with Nuitka (this can take many minutes on the first run)..."
& $Python -m nuitka $nuitkaArgs desktop_app.py
if ($LASTEXITCODE -ne 0) { throw "Nuitka build failed." }

# Nuitka emits desktop_app.dist next to the build directory output; normalize
# it into dist\SwiftMedics-nuitka so the two build paths have parallel shapes.
$distDir = Join-Path $RepoRoot "dist\SwiftMedics-nuitka"
$productDir = Get-ChildItem -Path (Join-Path $RepoRoot "build-nuitka") -Directory -Filter "desktop_app.dist" |
    Select-Object -First 1
if (-not $productDir) {
    # Some Nuitka versions emit the .dist folder next to the entry script.
    $productDir = Get-ChildItem -Path $RepoRoot -Directory -Filter "desktop_app.dist" |
        Select-Object -First 1
}
if (-not $productDir) {
    throw "Nuitka finished but the desktop_app.dist folder was not found."
}

New-Item -ItemType Directory -Force -Path (Split-Path $distDir) | Out-Null
if (Test-Path $distDir) { Remove-Item $distDir -Recurse -Force }
Move-Item $productDir.FullName $distDir

$Exe = Join-Path $distDir "SwiftMedics.exe"
if (-not (Test-Path $Exe)) {
    throw "Expected executable $Exe was not found after the Nuitka build."
}

Write-Host ""
Write-Host "Nuitka build complete:" -ForegroundColor Green
Write-Host "  $Exe"
Write-Host ""
Write-Host "The bundle contains compiled machine code, not Python bytecode."
Write-Host "Before running on a user machine, configure:"
Write-Host "  %APPDATA%\SwiftMedics\config.json"
Write-Host "Sign the exe with your code-signing certificate before distribution."
