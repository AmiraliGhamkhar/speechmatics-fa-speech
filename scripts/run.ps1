
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  py -3.11 -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
Write-Host ""
Write-Host "Set SPEECHMATICS_API_KEY in .env, then run:"
Write-Host ".\.venv\Scripts\python.exe app.py --language fa"
