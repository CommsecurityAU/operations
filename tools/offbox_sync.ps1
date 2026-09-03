<#
.SYNOPSIS
Off-box backup for the development machine (CS-OP-ARCH-002 section 12).

.DESCRIPTION
The counterpart to `offbox_sync.sh`, which runs on the VM. This runs on the
Windows laptop, where the only copy of the platform currently lives.

ONLY `backups\` and `documents\` are copied. NEVER the live `ops.db`: a WAL
database copied mid-transaction yields a `.db` and a `-wal` that disagree,
and the copy fails only at restore, on the day you need it. The snapshots in
`backups\` come from `VACUUM INTO` and are atomic and consistent by
construction; blobs in `documents\` are content-addressed and immutable. Both
are safe to copy while the app is running.

Backups living on the disk they protect are not backups. THIS is the backup;
`data\backups` is a convenience.

.PARAMETER Destination
Where to copy to. A second physical disk, a network share, or a synced
folder — anywhere that is not this disk.

.PARAMETER KeepDays
Snapshots older than this are removed FROM THE DESTINATION. Default 90.

.EXAMPLE
  .\tools\offbox_sync.ps1 -Destination "D:\backup\cs-ops"
  .\tools\offbox_sync.ps1 -Destination "\\nas\backup\cs-ops" -KeepDays 180
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Destination,
    [string]$Source,
    [int]$KeepDays = 90
)

$ErrorActionPreference = "Stop"

# Resolved from THIS SCRIPT's location, not from the working directory.
#
# A scheduled task runs from `system32`, so a default of `data` resolved to
# `C:\Windows\system32\data`, which does not exist -- and the task would
# have failed silently every hour while looking registered and Ready. The
# backup that never runs is worse than no backup, because it is believed.
if (-not $Source) {
    $Source = Join-Path (Split-Path -Parent $PSScriptRoot) "data"
}
$stamp = (Get-Date).ToString("s")

function Fail($message) {
    Write-Error "$stamp FATAL: $message"
    exit 1
}

if (-not (Test-Path (Join-Path $Source "backups"))) {
    Fail "$Source\backups does not exist. Start the server once: it takes a snapshot at boot."
}
Write-Host "$stamp source $Source"

# The destination must not be on the same volume as the source. A backup on
# the disk it protects survives a deleted file and nothing else -- not the
# failure that actually takes a laptop, which is the disk or the laptop.
$srcRoot = (Resolve-Path $Source).Path.Substring(0, 2)
if ($Destination -notmatch '^\\\\') {
    $destRoot = [System.IO.Path]::GetPathRoot($Destination).Substring(0, 2)
    if ($destRoot -eq $srcRoot) {
        Fail "$Destination is on the same volume as $Source. That is not a backup."
    }
}

New-Item -ItemType Directory -Force -Path (Join-Path $Destination "backups") | Out-Null

# Snapshots are immutable once written, so never re-copy one. `/XO` would
# overwrite on timestamp; nothing here should ever be overwritten.
$rc = robocopy (Join-Path $Source "backups") (Join-Path $Destination "backups") `
    /E /XC /XN /XO /R:2 /W:5 /NP /NDL /NJH /NJS
if ($LASTEXITCODE -ge 8) { Fail "robocopy failed with exit code $LASTEXITCODE" }

if (Test-Path (Join-Path $Source "documents")) {
    robocopy (Join-Path $Source "documents") (Join-Path $Destination "documents") `
        /E /XC /XN /XO /R:2 /W:5 /NP /NDL /NJH /NJS | Out-Null
    if ($LASTEXITCODE -ge 8) { Fail "robocopy of documents failed: $LASTEXITCODE" }
}

# Deliberately absent: any rule that would copy ops.db, ops.db-wal or
# ops.db-shm.

$newest = Get-ChildItem (Join-Path $Destination "backups") -Filter "ops-*.db" |
          Sort-Object Name -Descending | Select-Object -First 1
if (-not $newest) { Fail "no snapshot at the destination after copying" }

$ageHours = [math]::Round(((Get-Date) - $newest.LastWriteTime).TotalHours, 1)
$count = (Get-ChildItem (Join-Path $Destination "backups") -Filter "ops-*.db").Count
$size = [math]::Round($newest.Length / 1MB, 1)

Write-Host "$stamp copied. newest $($newest.Name) ${size}MB, ${ageHours}h old, $count total"

# A stale newest snapshot means the SERVER is not running, not that the copy
# failed -- and it is the more useful thing to be told.
if ($ageHours -gt 26) {
    Write-Warning "$stamp newest snapshot is ${ageHours}h old. Is the server running?"
}

# Prune the destination, oldest first. Names are UTC timestamps, so lexical
# order is chronological order.
$cutoff = (Get-Date).AddDays(-$KeepDays)
$old = Get-ChildItem (Join-Path $Destination "backups") -Filter "ops-*.db" |
       Where-Object { $_.LastWriteTime -lt $cutoff }
foreach ($file in $old) {
    Remove-Item $file.FullName -Force
    Write-Host "$stamp pruned $($file.Name)"
}
