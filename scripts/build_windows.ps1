<#
Build the SwiftMedics Windows desktop folder bundle.

Run from the repository root on Windows PowerShell:

    Set-ExecutionPolicy -Scope Process Bypass
    .\scripts\build_windows.ps1

Output:

    dist\SwiftMedics\SwiftMedics.exe

The runtime API key is NOT embedded in the exe.  Create:

    %APPDATA%\SwiftMedics\config.json

or press Start once after launching the app to let it create a template.
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
    Write-Host "Installing PyInstaller..."
    & $Python -m pip install --upgrade pyinstaller
}

Write-Host "Building SwiftMedics desktop bundle..."
& $Python -m PyInstaller --clean --noconfirm packaging\swiftmedics.spec

$Exe = Join-Path $RepoRoot "dist\SwiftMedics\SwiftMedics.exe"
if (-not (Test-Path $Exe)) {
    throw "Build finished but $Exe was not found. Check the PyInstaller output above."
}

Write-Host ""
Write-Host "Build complete:" -ForegroundColor Green
Write-Host "  $Exe"
Write-Host ""
Write-Host "Before running on a user machine, configure:"
Write-Host "  %APPDATA%\SwiftMedics\config.json"
Write-Host "The app can create a template if you launch it and press Start once."
