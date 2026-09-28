<#
Install the SwiftMedics runtime dependencies into a local virtual environment.

Run from the repository root on Windows PowerShell:

    Set-ExecutionPolicy -Scope Process Bypass
    .\scripts\install.ps1
#>
[CmdletBinding()]
param(
    [switch]$SkipUpgrade
)

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "Creating virtual environment (.venv)..."
    py -3 -m venv .venv
}

if (-not (Test-Path $Python)) {
    throw "Could not find .venv\Scripts\python.exe. Install Python 3 and retry."
}

if (-not $SkipUpgrade) {
    & $Python -m pip install --upgrade pip
}

Write-Host "Installing requirements.txt..."
& $Python -m pip install -r requirements.txt

Write-Host ""
Write-Host "Install complete. Start dictating with:"
Write-Host "  .\scripts\run.ps1"
