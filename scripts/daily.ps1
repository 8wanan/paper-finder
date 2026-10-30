# Daily digest entry point for Windows Task Scheduler.
#
# Note: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads
# .ps1 files as ANSI unless they carry a UTF-8 BOM, and a mis-decoded Chinese
# comment can break the parser (tested). ASCII sidesteps that entirely.
#
# Manual run:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\daily.ps1
#
# Register for 09:00 every day (run from the repo root, adjust paths):
#   schtasks /create /tn "PaperFinderDaily" /sc daily /st 09:00 ^
#     /tr "powershell -NoProfile -ExecutionPolicy Bypass -File %CD%\scripts\daily.ps1"
#
# Inspect / run now / delete:
#   schtasks /query  /tn "PaperFinderDaily" /v /fo LIST
#   schtasks /run    /tn "PaperFinderDaily"
#   schtasks /delete /tn "PaperFinderDaily" /f

param(
    [string]$Profile = "ai-vuln-discovery",
    [int]$Days = 3
)

# A scheduled task does not inherit interactive environment variables,
# so encoding and certificate settings are established here.
$ErrorActionPreference = "Continue"

$repo = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $repo "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "daily.log"

function Write-Log([string]$message) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $message
    Add-Content -Path $log -Value $line -Encoding UTF8
    Write-Host $line
}

$env:PYTHONIOENCODING = "utf-8"

# Certificates: some Windows Pythons ship without a usable CA bundle. The tool
# also self-heals at runtime (papers/net.py ensure_default_ca), but setting this
# early avoids one failed TLS handshake per request -- and arXiv throttles at
# 3.2s/request, so a doubled request count is expensive.
if (-not $env:SSL_CERT_FILE) {
    $pythonForCa = (Get-Command python -ErrorAction SilentlyContinue).Source
    $candidates = @(
        "C:\Program Files\Git\usr\ssl\certs\ca-bundle.crt",
        "C:\Program Files\Git\mingw64\ssl\certs\ca-bundle.crt",
        "C:\Program Files\Git\mingw64\etc\ssl\certs\ca-bundle.crt"
    )
    if ($pythonForCa) {
        $pythonRoot = Split-Path -Parent $pythonForCa
        $candidates += @(
            (Join-Path $pythonRoot "..\ssl\certs\ca-bundle.crt"),
            (Join-Path $pythonRoot "..\etc\pki\ca-trust\extracted\pem\tls-ca-bundle.pem"),
            (Join-Path $pythonRoot "..\..\etc\pki\ca-trust\extracted\pem\tls-ca-bundle.pem")
        )
    }
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            $env:SSL_CERT_FILE = (Resolve-Path $candidate).Path
            break
        }
    }
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) {
    Write-Log "FAILED: python not found on PATH"
    exit 1
}

Push-Location $repo
$code = 1
try {
    Write-Log "start: profile=$Profile days=$Days"
    # python writes progress to stderr, which PowerShell 5.1 surfaces as error
    # records; merge them and append line by line so the log stays readable.
    $output = & $python papers.py daily --profile $Profile --days $Days 2>&1 | Out-String
    $code = $LASTEXITCODE
    foreach ($line in ($output -split "`r?`n")) {
        if ($line.Trim()) {
            Add-Content -Path $log -Value ("    " + $line) -Encoding UTF8
        }
    }

    if ($code -eq 0) {
        $latest = Get-ChildItem (Join-Path $repo "briefs") -Filter "daily-*.md" -ErrorAction SilentlyContinue |
            Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($latest) {
            Write-Log "done: $($latest.FullName)"
        } else {
            Write-Log "done, but no digest file found"
        }
    } else {
        Write-Log "FAILED: python exit code $code (see $log)"
    }
} finally {
    Pop-Location
}

exit $code
