
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
Write-Host "Installed. Edit .env and set SPEECHMATICS_API_KEY."
