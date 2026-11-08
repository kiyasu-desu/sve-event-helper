# Start / stop / check the mitmproxy capture proxy for this project.
# ASCII only (PS 5.1 parses scripts as ANSI).
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\capture\mitm_ctl.ps1 -Start
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\capture\mitm_ctl.ps1 -Stop
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\capture\mitm_ctl.ps1 -Status
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\capture\mitm_ctl.ps1 -Log
param(
    [switch]$Start,
    [switch]$Stop,
    [switch]$Status,
    [switch]$Log,
    [int]$Port = 0
)

$ErrorActionPreference = 'Continue'

# Shared settings (paths, ports, adb/mitmdump locations) live in scripts\config.ps1.
. (Join-Path $PSScriptRoot '..\config.ps1')

if ($Port -le 0) { $Port = $MitmPort }
$venvPython = $VenvPython
$confdir  = $ConfDir
$logout   = $MitmLogOut
$logerr   = $MitmLogErr
$flow     = $CaptureFlow
$addon    = $CaptureAddon
$pidFile  = $MitmPidFile

function Say($m) { Write-Host "[mitm] $m" }

function Get-Listener {
    Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
}

function Do-Stop {
    $owners = Get-Listener
    if (-not $owners) { Say "nothing listening on $Port" }
    foreach ($procId in $owners) {
        $p = Get-Process -Id $procId -ErrorAction SilentlyContinue
        if ($p) { Say ("stopping pid=" + $procId + " (" + $p.ProcessName + ")"); Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue }
    }
    Get-Process -Name 'mitmdump' -ErrorAction SilentlyContinue | ForEach-Object {
        Say ("stopping leftover mitmdump pid=" + $_.Id); Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 2
    if (Test-Path $pidFile) { Remove-Item $pidFile -Force -ErrorAction SilentlyContinue }
    if (Get-Listener) { Say "WARNING: $Port still in use" } else { Say "$Port released" }
}

if ($Stop)  { Do-Stop; exit 0 }

if ($Status) {
    $owners = Get-Listener
    if ($owners) { Say ("listening on " + $Port + " pid=" + ($owners -join ',')) } else { Say "not listening" }
    Say ("sve_requests.jsonl exists: " + (Test-Path $CaptureJsonl))
    Say ("flow exists: " + (Test-Path $flow))
    exit 0
}

if ($Log) {
    Say "--- stdout tail ---"
    if (Test-Path $logout) { Get-Content $logout -Tail 40 }
    Say "--- stderr tail ---"
    if (Test-Path $logerr) { Get-Content $logerr -Tail 40 }
    exit 0
}

if ($Start) {
    if (Get-Listener) { Say "already listening on $Port; stop it first (-Stop)"; exit 1 }
    if (-not (Test-Path $venvPython)) { Say "venv python not found: $venvPython"; exit 1 }
    New-Item -ItemType Directory -Force -Path (Split-Path $logout) | Out-Null
    # Launch via 'python -c' instead of the venv's mitmdump.exe shim: those shims
    # embed absolute paths and break whenever the venv folder moves.
    # Everything is passed as ONE command line, because Start-Process re-quotes
    # array elements and breaks '--set key=value' style options.
    $launcher = 'import sys; from mitmproxy.tools import main; sys.argv=[''mitmdump'']+sys.argv[1:]; main.mitmdump()'
    $argLine = '-c "' + $launcher + '" --set confdir="' + $confdir + '" -p ' + $Port + ' -s "' + $addon + '" -w "' + $flow + '"'
    Say ("starting: " + $venvPython + " " + $argLine)
    $p = Start-Process -FilePath $venvPython -ArgumentList $argLine `
            -RedirectStandardOutput $logout -RedirectStandardError $logerr `
            -WindowStyle Hidden -PassThru
    Set-Content -Path $pidFile -Value $p.Id
    Start-Sleep -Seconds 6
    $owners = Get-Listener
    if ($owners) { Say ("started pid=" + ($owners -join ',') + " listening on " + $Port) }
    else { Say "FAILED to bind; see log:"; if (Test-Path $logerr) { Get-Content $logerr -Tail 20 }; exit 1 }
    exit 0
}

Say 'nothing to do: pass -Start, -Stop, -Status or -Log'
