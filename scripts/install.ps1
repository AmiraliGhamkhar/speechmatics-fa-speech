# Compatibility wrapper -> scripts/swiftmedics_tools.py install
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..

# Prefer the active project interpreter, then validated 3.11, then another
# supported Python (3.11+).  Each candidate is version-checked before use.
$candidates = @()
if (Test-Path ".venv\Scripts\python.exe") {
    $candidates += ,@(".venv\Scripts\python.exe")
}
$candidates += ,@("py", "-3.11")
$candidates += ,@("py", "-3")
$candidates += ,@("python")

foreach ($candidate in $candidates) {
    $exe = $candidate[0]
    $prefix = @($candidate | Select-Object -Skip 1)
    try {
        $ok = & $exe @prefix -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            & $exe @prefix scripts\swiftmedics_tools.py install @args
            exit $LASTEXITCODE
        }
    } catch {
        # Candidate is unavailable; continue to the next supported option.
    }
}

Write-Error "No compatible Python found. Install Python 3.11 or newer, or activate a compatible project virtual environment."
exit 1
