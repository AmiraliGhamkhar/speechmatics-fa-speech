
# Standalone injector smoke test (cross-platform).
# Click the target text field ONCE, press ENTER here, and watch the paste.
# Compatibility wrapper -> scripts/swiftmedics_tools.py test-injector
Set-Location $PSScriptRoot\..
& .\.venv\Scripts\python.exe scripts\swiftmedics_tools.py test-injector
