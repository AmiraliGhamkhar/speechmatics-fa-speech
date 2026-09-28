<#
Run the SwiftMedics Persian dictation CLI.

Installs the dependencies on first run, then starts the application.
Every extra argument is passed straight through, e.g.:

    .\scripts\run.ps1 --language en
    .\scripts\run.ps1 --no-inject --save-report
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "First run: installing requirements.txt..."
    py -3 -m venv .venv
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -r requirements.txt
}

& .\.venv\Scripts\python.exe app.py --language fa @args
