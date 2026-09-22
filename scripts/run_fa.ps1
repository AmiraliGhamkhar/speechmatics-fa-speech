
# Persian dictation with automatic injection (no hotkeys: click the target
# field once, then dictate - every finalized segment is pasted at the cursor).
# Compatibility wrapper -> scripts/swiftmedics_tools.py run-fa
Set-Location $PSScriptRoot\..
& .\.venv\Scripts\python.exe scripts\swiftmedics_tools.py run-fa @args
