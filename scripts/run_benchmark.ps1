
# Compatibility wrapper -> swiftmedics_tools.py benchmark
# (was: app.py --language fa --test-id mix_ct_lesion, which needed a live
# microphone and API key; the offline nursing benchmark is the useful one.
# To reproduce the old behavior: .\scripts\run_fa.ps1 --test-id mix_ct_lesion)
Set-Location $PSScriptRoot\..
& .\.venv\Scripts\python.exe scripts\swiftmedics_tools.py benchmark @args
