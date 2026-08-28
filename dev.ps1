<#
.SYNOPSIS
    Local dev runner. The Windows counterpart to `make dev` / `make seed`
    / `make session`, since the Makefile's POSIX shell built-ins do not run
    here.

.EXAMPLE
    .\dev.ps1              # start the server
    .\dev.ps1 -Seed        # migrate + import the FY27 register, then exit
    .\dev.ps1 -Session     # mint a local session cookie, then exit
    .\dev.ps1 -Stale       # is the running server behind the working tree?
    .\dev.ps1 -Port 5174   # somewhere else

.NOTES
    Environment variables are set INSIDE this script, so the child python
    process inherits them and a fresh terminal needs no setup. Setting them
    by hand in one window and running the server in another is how you end
    up with a server that has none of them.
#>
[CmdletBinding()]
param(
    [int]$Port = 5173,
    [string]$Data = "$PSScriptRoot\data",
    [switch]$Seed,
    [switch]$Session,
    [switch]$Stale
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$py = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }

# ---------------------------------------------------------------- env
$env:OPS_DATA            = $Data
$env:OPS_SECRETS_PATH    = Join-Path $Data "secrets\store.json"
$env:OPS_TLS             = "off"
$env:OPS_PORT            = "$Port"
$env:OIDC_REDIRECT_URI   = "http://localhost:$Port/auth/callback"

# A real OIDC client id, if there is one. It is NOT a secret, but it is
# site-specific, so it lives in a gitignored file rather than in a script
# that is copied between machines. `dev.local.ps1` is dot-sourced, so it
# may set anything -- OPS_HOSTED_DOMAIN, a different port, whatever the
# machine needs.
if (Test-Path "$PSScriptRoot\dev.local.ps1") {
    . "$PSScriptRoot\dev.local.ps1"
}
if (-not $env:OIDC_CLIENT_ID) {
    $env:OIDC_CLIENT_ID  = "dev-client-not-registered"
}

# ------------------------------------------------------------- -Seed
if ($Seed) {
    New-Item -ItemType Directory -Force -Path $Data | Out-Null
    & $py -c "import sys;sys.path.insert(0,'.');from ops.db import Db;Db(r'$Data\ops.db','ops/migrations').migrate()"
    & $py tools\import_register.py --csv tests\fixtures\project_register_fy27.csv --db "$Data\ops.db"
    exit $LASTEXITCODE
}

# ---------------------------------------------------------- -Session
if ($Session) {
    & $py tools\dev_session.py --data $Data --port $Port
    exit $LASTEXITCODE
}

# ----------------------------------------------------------- -Stale
# Python loads a module once, so a running server can be several edits
# behind the working tree while every test passes and the browser
# disagrees. One request settles it.
if ($Stale) {
    $disk = (& $py -m ops.main --fingerprint).Trim()
    try {
        $running = (Invoke-RestMethod "http://localhost:$Port/healthz").code
    } catch {
        Write-Host "  No server answering on $Port." -ForegroundColor Yellow
        exit 1
    }
    $assets = (& $py -c "import sys; sys.path.insert(0,'.'); from ops.main import asset_fingerprint; print(asset_fingerprint())").Trim()
    Write-Host ""
    Write-Host "  running  $running"
    Write-Host "  on disk  $disk"
    Write-Host "  assets   $assets"
    if ($running -eq $disk) {
        # No backticks in a double-quoted string: PowerShell reads them as
        # escapes, and the word after one silently loses its first letter.
        Write-Host "  server is current." -ForegroundColor Green
        Write-Host "  Compare both hashes against the values quoted with the" -ForegroundColor Green
        Write-Host "  last batch. Static files are read per request, so a" -ForegroundColor Green
        Write-Host "  stale one shows in 'assets' and nowhere else." -ForegroundColor Green
        Write-Host ""
        exit 0
    }
    Write-Host "  STALE - restart the server." -ForegroundColor Red
    Write-Host ""
    exit 1
}

# ------------------------------------------------- port pre-flight
# This check exists because half an hour once went into debugging a stale
# Docker container that was answering the port. Tests passed, the browser
# 404'd, and nothing said why. Name the occupant BEFORE binding.
try {
    $held = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
} catch { $held = $null }

if ($held) {
    $owner = try { (Get-Process -Id $held[0].OwningProcess).ProcessName } catch { "unknown" }
    Write-Host ""
    Write-Host "  Port $Port is already held by: $owner (pid $($held[0].OwningProcess))" -ForegroundColor Red
    if ($owner -match "docker|wslrelay|com\.docker") {
        Write-Host "  That looks like Docker. Check:  docker ps" -ForegroundColor Yellow
        Write-Host "  Free it with:  docker rm -f `$(docker ps -q --filter publish=$Port)" -ForegroundColor Yellow
    }
    Write-Host "  Or run somewhere else:  .\dev.ps1 -Port $($Port + 1)" -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

# Windows reserves blocks of ports for Hyper-V/WSL, and a bind against one
# fails with WinError 10013 rather than anything that mentions reservations.
$excluded = & netsh interface ipv4 show excludedportrange protocol=tcp 2>$null
if ($excluded) {
    foreach ($line in $excluded) {
        if ($line -match '^\s*(\d+)\s+(\d+)') {
            if ($Port -ge [int]$Matches[1] -and $Port -le [int]$Matches[2]) {
                Write-Host ""
                Write-Host "  Port $Port sits inside a reserved range ($($Matches[1])-$($Matches[2]))." -ForegroundColor Red
                Write-Host "  Windows will refuse the bind with WinError 10013. Pick another port." -ForegroundColor Yellow
                Write-Host ""
                exit 1
            }
        }
    }
}

# Only the SERVER needs a secret store; -Stale, -Session and -Seed do not.
# Creating it in the common path meant a read-only question wrote state,
# which is a small lie about what the command does.
New-Item -ItemType Directory -Force -Path (Join-Path $Data "secrets") | Out-Null
if (-not (Test-Path $env:OPS_SECRETS_PATH)) {
    Write-Host "  creating dev secret store" -ForegroundColor DarkGray
    "dev-not-a-real-secret" | & $py -m ops.secrets set OIDC_CLIENT_SECRET | Out-Null
}

# Say which client is in play. Signing in against a placeholder fails at
# Google with a message about the client, not about this machine, so it is
# worth knowing before the browser opens.
if ($env:OIDC_CLIENT_ID -eq "dev-client-not-registered") {
    Write-Host "  OIDC: placeholder client -- sign-in will not work." -ForegroundColor DarkGray
    Write-Host "        Use  .\dev.ps1 -Session  for a dev cookie, or set" -ForegroundColor DarkGray
    Write-Host "        OIDC_CLIENT_ID in dev.local.ps1 to use a real one." -ForegroundColor DarkGray
} else {
    Write-Host "  OIDC: $($env:OIDC_CLIENT_ID)" -ForegroundColor DarkGray
}

if (-not (Test-Path "$Data\ops.db")) {
    Write-Host "  No database yet. Run  .\dev.ps1 -Seed  for the FY27 register." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "  http://localhost:$Port" -ForegroundColor Cyan
Write-Host "  no session yet?  .\dev.ps1 -Session   (in another terminal)" -ForegroundColor DarkGray
Write-Host ""

& $py -m ops.main
