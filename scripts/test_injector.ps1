
# Standalone injector smoke test (cross-platform).
# Click the target text field ONCE, press ENTER here, and watch the paste.
Set-Location $PSScriptRoot\..
.\.venv\Scripts\python.exe -c "from injector import TextInjector; t=TextInjector(); print('Click the target text field.'); input('Press ENTER when ready... '); ok=t.paste_text('SwiftMedics test | فارسی | CT scan | lesion | 140/90 | IV'); print('paste result:', ok)"
