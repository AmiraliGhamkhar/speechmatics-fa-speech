# Matcher performance benchmark (dictionary build + matching latency at
# ~100/500/1000/2000 terms). Writes benchmark/results.json; compare runs
# before/after matcher changes. See benchmark/benchmark_matcher.py.
param(
    [int]$Repeats = 300,
    [int[]]$Sizes = @(100, 500, 1000, 2000)
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& .\.venv\Scripts\python.exe benchmark\benchmark_matcher.py --repeats $Repeats --sizes $Sizes @args
