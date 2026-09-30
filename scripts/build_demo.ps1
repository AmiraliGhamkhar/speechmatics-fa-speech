<#
Build the SwiftMedics DEMO Windows bundle: compiled-in expiry + token broker.

Run from the repository root on Windows PowerShell:

    Set-ExecutionPolicy -Scope Process Bypass
    .\scripts\build_demo.ps1 -ExpiryDate 2026-11-30 -BrokerUrl https://your-app.vercel.app/api/token

What this does that the regular build does not:

  1. Writes speechmatics_test\demo_build_stamp.py (expiry + broker URL).
     That file is git-ignored and never committed.
  2. Builds the one-folder bundle with PyInstaller (same spec as production).
  3. Deletes the stamp afterwards so the repository returns to a non-demo
     state (use -KeepStamp to keep it for rebuilds).

The demo exe holds NO Speechmatics API key. At Start it fetches a short-lived
JWT from -BrokerUrl; the long-lived key lives only on the broker host. Rotate
or revoke demo access by changing DEMO_TOKEN on the broker.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ExpiryDate,

    [Parameter(Mandatory = $true)]
    [string]$BrokerUrl,

    [string]$BuildId = "",

    [string]$DemoToken = "",

    [switch]$SkipInstall,

    [switch]$KeepStamp
)

$ErrorActionPreference = "Stop"

if (-not ($BrokerUrl -like "https://*")) {
    throw "-BrokerUrl must be an https URL (the demo machine must not use plain http)."
}

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "Creating virtual environment..."
    py -3 -m venv .venv
}
if (-not (Test-Path $Python)) {
    throw "Could not find .venv\Scripts\python.exe. Install Python 3 and retry."
}

if (-not $SkipInstall) {
    Write-Host "Installing runtime requirements..."
    & $Python -m pip install --upgrade pip
    & $Python -m pip install -r requirements.txt
    & $Python -m pip install --upgrade pyinstaller
}

# 1. Compile the demo stamp in (expiry + broker URL).
$stampArgs = @("scripts\set_demo_expiry.py", "--date", $ExpiryDate, "--broker-url", $BrokerUrl)
if ($BuildId) { $stampArgs += @("--build-id", $BuildId) }
& $Python @stampArgs
if ($LASTEXITCODE -ne 0) { throw "set_demo_expiry.py failed." }

try {
    # 2. Build the folder bundle (same spec as the production build).
    Write-Host "Building SwiftMedics DEMO bundle..."
    & $Python -m PyInstaller --clean --noconfirm packaging\swiftmedics.spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed." }

    $Exe = Join-Path $RepoRoot "dist\SwiftMedics\SwiftMedics.exe"
    if (-not (Test-Path $Exe)) {
        throw "Build finished but $Exe was not found."
    }

    # 3. Ship a ready-to-edit config template alongside the exe.
    $configTemplate = Join-Path $RepoRoot "dist\SwiftMedics\config-demo-template.json"
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
        entity_guard         = $true
        device_index         = $null
        save_report          = $false
    } | ConvertTo-Json | Set-Content -Path $configTemplate -Encoding UTF8

    Write-Host ""
    Write-Host "DEMO build complete:" -ForegroundColor Green
    Write-Host "  $Exe"
    Write-Host ""
    Write-Host "Demo expiry      : $ExpiryDate"
    Write-Host "Token broker     : $BrokerUrl"
    if ($DemoToken) { Write-Host "Demo token       : set in the config template" }
    Write-Host ""
    Write-Host "Copy the dist\SwiftMedics folder to the demo machine. No API key is inside the bundle;"
    Write-Host "the exe fetches short-lived tokens from the broker at each Start."
    Write-Host "Optional: copy config-demo-template.json to %APPDATA%\SwiftMedics\config.json to override defaults."
}
finally {
    if (-not $KeepStamp) {
        $stamp = Join-Path $RepoRoot "speechmatics_test\demo_build_stamp.py"
        if (Test-Path $stamp) {
            Remove-Item $stamp -Force
            Write-Host "Demo stamp removed from the repository (rebuild with -KeepStamp to keep it)."
        }
    }
}
