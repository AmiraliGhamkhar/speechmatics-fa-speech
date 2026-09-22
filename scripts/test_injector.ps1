# Standalone injector smoke test (cross-platform).
# Click the target text field: on Windows it is detected and armed
# automatically (same target selection as the app). On other platforms,
# press ENTER here after clicking, then watch the paste.
# Compatibility wrapper -> scripts/swiftmedics_tools.py test-injector
Set-Location $PSScriptRoot\..
& .\.venv\Scripts\python.exe scripts\swiftmedics_tools.py test-injector
