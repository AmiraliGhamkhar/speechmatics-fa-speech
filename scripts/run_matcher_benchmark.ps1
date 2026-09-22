# Matcher performance benchmark (dictionary build + matching latency at
# ~100/500/1000/2000 terms). Compare runs before/after matcher changes.
# Compatibility wrapper -> scripts/swiftmedics_tools.py benchmark-matcher
param(
    [int]$Repeats = 300,
    [int[]]$Sizes = @(100, 500, 1000, 2000)
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)
& .\.venv\Scripts\python.exe scripts\swiftmedics_tools.py benchmark-matcher --repeats $Repeats --sizes $Sizes @args
