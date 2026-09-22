
# Compatibility wrapper -> scripts/swiftmedics_tools.py run-fa
# Installs dependencies (requirements.txt) and then runs app.py --language fa.
# Extra arguments are passed through, e.g.:
#   .\scripts\run.ps1 --no-inject
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..
py -3.11 scripts\swiftmedics_tools.py install
& .\.venv\Scripts\python.exe scripts\swiftmedics_tools.py run-fa @args
