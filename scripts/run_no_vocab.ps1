
# Compatibility wrapper -> swiftmedics_tools.py run-fa --no-vocab
Set-Location $PSScriptRoot\..
& .\.venv\Scripts\python.exe scripts\swiftmedics_tools.py run-fa --no-vocab @args
