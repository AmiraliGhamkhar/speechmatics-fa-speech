
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  py -3.11 -m venv .venv
}
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env" }
# Run the app; extra arguments are passed through, e.g.:
#   .\scripts\run.ps1 --language en --no-inject
& .\.venv\Scripts\python.exe app.py --language fa @args
