
# Compatibility wrapper -> scripts/swiftmedics_tools.py install
# The implementation lives in ONE place (the Python tool); this file only
# forwards, so behavior can never drift between the two.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot\..
# Bootstrap: the venv may not exist yet, so use the system interpreter.
# requirements.txt is installed by the tool's install subcommand.
py -3.11 scripts\swiftmedics_tools.py install @args
