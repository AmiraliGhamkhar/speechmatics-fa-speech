<#
Build the SwiftMedics HOSPITAL DEMO: Nuitka-compiled, compiled-in expiry,
compiled-in broker URL, NO Speechmatics API key anywhere in the bundle.

This is THE one recommended command for producing the hospital demo:

    Set-ExecutionPolicy -Scope Process Bypass
    .\scripts\build_hospital_demo.ps1 `
        -ExpiryDate 2026-11-30 `
        -BrokerUrl https://YOUR-BROKER.vercel.app/api/token `
        -BuildId hospital-x-demo

What it does, in order:

  1. Verifies Windows + Python, creates .venv when missing.
  2. Installs runtime requirements, Nuitka and ordered-set (skip: -SkipInstall).
  3. Validates the broker URL with the app's own rules
     (speechmatics_test/broker_client.py: https, no credentials/query/fragment).
  4. Generates speechmatics_test\demo_build_stamp.py (git-ignored; expiry +
     build ID + broker URL). The stamp is COMPILED INTO the build by Nuitka;
     no .py stamp ships beside the exe.
  5. Builds desktop_app.py with Nuitka standalone (no console), including the
     medical_knowledge data files and all runtime packages.
  6. Collects the dist folder into dist\SwiftMedics-hospital-demo\ and writes
     config-demo-template.json.
  7. Runs secret-safety checks: the long-lived API key must NOT appear in the
     bundle, and no .py source/.pyc bytecode of the app may ship.
  8. Removes the temporary stamp (keep with -KeepStamp).

The demo exe authenticates through the token broker (api/token.py on Vercel)
and receives a short-lived JWT per session; the long-lived key lives only on
the broker server. Nuitka compiles the Python program to C and machine code:
the bundle contains no ordinary Python bytecode of the application, which
removes easy decompilation and raises reverse-engineering difficulty - it is
not protection against determined native-code reverse engineering.

First build takes many minutes (full C compile); later builds reuse Nuitka's
ccache object cache. Code-sign the exe before hospital distribution.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpiryDate,

    [Parameter(Mandatory = $true)]
    [string]$BrokerUrl,

    [string]$BuildId = "",

    [string]$DemoToken = "",

    [string]$ProductVersion = "1.0.0",

    [switch]$SkipInstall,

    [switch]$KeepStamp
)

$ErrorActionPreference = "Stop"

# ------------------------------------------------------------------ checks

if ($PSVersionTable.PSVersion.Major -lt 5) {
    throw "Windows PowerShell 5.1 or newer is required."
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

if (-not $IsWindows -and $env:OS -ne "Windows_NT") {
    throw "This build must run on Windows: Nuitka cannot cross-compile the Windows exe."
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "Creating virtual environment (.venv)..."
    py -3 -m venv .venv
}
if (-not (Test-Path $Python)) {
    throw "Could not find .venv\Scripts\python.exe. Install Python 3 from python.org and retry."
}

if (-not $SkipInstall) {
    Write-Host "Installing runtime requirements..."
    & $Python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed." }
    & $Python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Installing requirements.txt failed." }
    Write-Host "Installing Nuitka (compiler) and ordered-set (faster compiles)..."
    & $Python -m pip install --upgrade nuitka ordered-set
    if ($LASTEXITCODE -ne 0) { throw "Installing Nuitka failed." }
} else {
    & $Python -c "import nuitka" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "Nuitka is not installed in .venv; rerun without -SkipInstall."
    }
}

# Validate the broker URL with the exact rules the desktop app applies at
# runtime, so a URL that would be rejected on the demo machine fails here.
$checkCode = "from speechmatics_test.broker_client import validate_broker_url; validate_broker_url(r'''$BrokerUrl'''); print('broker-url-ok')"
& $Python -c $checkCode
if ($LASTEXITCODE -ne 0) {
    throw "BrokerUrl failed validation (must be https, no credentials/query/fragment, default port)."
}

# ------------------------------------------------------------ demo stamp

Write-Host "Generating the temporary demo build stamp..."
$stampArgs = @("scripts\set_demo_expiry.py", "--date", $ExpiryDate, "--broker-url", $BrokerUrl)
if ($BuildId) { $stampArgs += @("--build-id", $BuildId) }
& $Python @stampArgs
if ($LASTEXITCODE -ne 0) { throw "set_demo_expiry.py failed." }
$stampPath = Join-Path $RepoRoot "speechmatics_test\demo_build_stamp.py"
if (-not (Test-Path $stampPath)) { throw "Demo stamp was not generated at $stampPath." }

$distDir = Join-Path $RepoRoot "dist\SwiftMedics-hospital-demo"

try {
    # ------------------------------------------------------- Nuitka build
    # Flags verified against Nuitka 4.2.2:
    #   --mode=standalone            (current replacement of --standalone)
    #   --windows-console-mode=disable  (windowed app, no console)
    #   --company-name/--product-name/--product-version/--file-version/
    #   --file-description           (embedded Windows version metadata)
    #   --include-package / --include-data-dir / --include-package-data
    # Imports are followed automatically from desktop_app.py (which pulls in
    # app.py, the medical layer, the injector, the broker client and the demo
    # license gate); extension modules and data files are listed explicitly.
    $nuitkaArgs = @(
        "--mode=standalone",
        "--assume-yes-for-downloads",
        "--output-dir=build-nuitka",
        "--output-filename=SwiftMedics.exe",
        "--include-package=speechmatics_test",
        "--include-data-dir=medical_knowledge=medical_knowledge",
        "--windows-console-mode=disable",
        "--company-name=SwiftMedics",
        "--product-name=SwiftMedics",
        "--product-version=$ProductVersion",
        "--file-version=$ProductVersion",
        "--file-description=SwiftMedics medical dictation",
        # Explicit keeps for modules/data that are only reached dynamically:
        "--include-package=speechmatics.rt",
        "--include-package=pyaudio",
        "--include-package=pyperclip",
        "--include-package=ahocorasick",
        "--include-package-data=ahocorasick"
    )

    Write-Host "Compiling SwiftMedics with Nuitka (first build can take many minutes)..."
    & $Python -m nuitka $nuitkaArgs desktop_app.py
    if ($LASTEXITCODE -ne 0) { throw "Nuitka build failed." }

    $productDir = Get-ChildItem -Path (Join-Path $RepoRoot "build-nuitka") -Directory -Filter "desktop_app.dist" |
        Select-Object -First 1
    if (-not $productDir) {
        $productDir = Get-ChildItem -Path $RepoRoot -Directory -Filter "desktop_app.dist" |
            Select-Object -First 1
    }
    if (-not $productDir) {
        throw "Nuitka finished but the desktop_app.dist folder was not found."
    }

    New-Item -ItemType Directory -Force -Path (Split-Path $distDir) | Out-Null
    if (Test-Path $distDir) { Remove-Item $distDir -Recurse -Force }
    Move-Item $productDir.FullName $distDir

    $exe = Join-Path $distDir "SwiftMedics.exe"
    if (-not (Test-Path $exe)) {
        throw "Expected executable $exe was not found after the Nuitka build."
    }

    # ------------------------------------------------- demo config template
    $configTemplate = Join-Path $distDir "config-demo-template.json"
    @{
        speechmatics_api_key = ""
        token_broker_url     = $BrokerUrl
        demo_token           = $DemoToken
        language             = "fa"
        model                = "enhanced"
        max_delay            = 2.0
        max_delay_mode       = "flexible"
        domain               = "auto"
        inject               = $true
        medical_layer        = $true
        medical_vocab        = $true
        focus_guard          = $true
        entity_guard          = $true
        device_index         = $null
        save_report          = $false
    } | ConvertTo-Json | Set-Content -Path $configTemplate -Encoding UTF8

    # --------------------------------------------------- secret-safety gate
    # The build FAILS if the long-lived API key is present anywhere in the
    # bundle. The key value is never printed - only file names and counts.
    Write-Host "Running secret-safety checks on the bundle..."
    $secretScan = @'
import json
import sys
from pathlib import Path

bundle = Path(sys.argv[1])
needles = [n for n in sys.argv[2:] if n]
for needle in needles:
    if needle.lower() in ("your_speechmatics_api_key",):
        continue
    hits = []
    for path in bundle.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() in (".wav", ".mp3"):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if needle.encode("utf-8") in data:
            hits.append(str(path.relative_to(bundle)))
    if hits:
        print(f"SECRET HIT {needle[:4]}*** in {len(hits)} file(s):", file=sys.stderr)
        for hit in hits[:10]:
            print(f"  {hit}", file=sys.stderr)
        sys.exit(1)
print("secret-scan-ok")
'@
    $envKey = $env:SPEECHMATICS_API_KEY
    $scanArgs = @("-", $distDir)
    if ($envKey) { $scanArgs += $envKey }
    $secretScan | & $Python - @scanArgs
    if ($LASTEXITCODE -ne 0) {
        throw "SECRET-SAFETY CHECK FAILED: a long-lived credential was found in the bundle. Remove it and rebuild."
    }

    # No ordinary Python bytecode of the application may ship in a Nuitka build.
    $pyArtifacts = Get-ChildItem -Path $distDir -Recurse -Include *.py,*.pyc -File |
        Where-Object { $_.FullName -notmatch "site-packages|matplotlib|numpy|scipy|PIL|pytz" }
    $pyArtifacts = @($pyArtifacts | Where-Object { $_.Length -gt 0 })
    if ($pyArtifacts.Count -gt 0) {
        Write-Warning "Compiled-in source/bytecode artifacts found in the bundle:"
        foreach ($artifact in $pyArtifacts) { Write-Warning "  $($artifact.FullName)" }
        throw "The hospital bundle must not contain .py/.pyc files of the application."
    }

    Write-Host ""
    Write-Host "HOSPITAL DEMO build complete:" -ForegroundColor Green
    Write-Host "  $exe"
    Write-Host ""
    Write-Host "Demo expiry      : $ExpiryDate (23:59:59 local time)"
    Write-Host "Build ID         : $(if ($BuildId) { $BuildId } else { 'demo-' + $ExpiryDate })"
    Write-Host "Token broker     : $BrokerUrl"
    Write-Host "Bundle           : $distDir"
    Write-Host ""
    Write-Host "Ship the whole folder. No Python, no Nuitka, no API key is needed on the"
    Write-Host "hospital machine. Optional: copy config-demo-template.json to"
    Write-Host "%APPDATA%\SwiftMedics\config.json to override the compiled-in defaults."
    Write-Host "Sign the exe with your code-signing certificate before distribution."
}
finally {
    if (-not $KeepStamp -and (Test-Path (Join-Path $RepoRoot "speechmatics_test\demo_build_stamp.py"))) {
        Remove-Item (Join-Path $RepoRoot "speechmatics_test\demo_build_stamp.py") -Force
        Write-Host "Temporary demo stamp removed (rebuild with -KeepStamp to keep it)."
    }
}
