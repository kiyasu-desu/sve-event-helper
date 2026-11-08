# Bring the whole capture environment up (or take it down) in one command.
# ASCII only (PS 5.1 parses scripts as ANSI).
#
# Steps performed by -Start, each idempotent:
#   1. adb root on the "sve" VM (never touches pjsk)
#   2. push + start frida-server if it is not running
#   3. adb forward tcp:<FridaPort> -> device 27042
#   4. start mitmdump if nothing listens on the proxy port
#   5. point the sve VM's global proxy at the host mitmproxy
#   6. spawn the app under the unpinning payload (background, logged to capture/frida.log)
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\capture\env_up.ps1 -Start
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\capture\env_up.ps1 -Status
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\capture\env_up.ps1 -Stop
param(
    [switch]$Start,
    [switch]$Stop,
    [switch]$Status,
    [switch]$NoSpawn
)

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '..\config.ps1')

$FridaPort   = 27043
$FridaServer = '/data/local/tmp/frida-server'
$FridaBin    = Join-Path $ProjectRoot 'vendor\frida\frida-server'
$Payload     = Join-Path $CaptureDir 'payload.bundle.js'
$FridaLog    = Join-Path $CaptureDir 'frida_out.log'
$FridaErr    = Join-Path $CaptureDir 'frida_err.log'
$FridaPid    = Join-Path $CaptureDir 'frida.pid'

function Say($m) { Write-Host "[env] $m" }
function Sh([string]$c) {
    $out = & $AdbExe -s $SveSerial shell $c 2>&1
    if ($null -eq $out) { return , @() }
    return $out
}

function Get-MitmListener {
    Get-NetTCPConnection -LocalPort $MitmPort -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
}

function Get-FridaForward {
    $out = & $AdbExe -s $SveSerial forward --list 2>&1
    $out | Where-Object { $_ -match "tcp:$FridaPort" }
}

function Ensure-FridaServer {
    $running = ((Sh 'pidof frida-server') -join '').Trim()
    if ($running) { Say "frida-server already running (pid $running)"; return $true }

    $has = (Sh "ls $FridaServer 2>/dev/null").Trim()
    if (-not $has) {
        if (-not (Test-Path $FridaBin)) { Say "frida-server binary missing: $FridaBin"; return $false }
        Say 'pushing frida-server ...'
        & $AdbExe -s $SveSerial push $FridaBin $FridaServer 2>&1 | Out-Null
        Sh "chmod 755 $FridaServer" | Out-Null
    }
    Say 'starting frida-server ...'
    Sh "cd /data/local/tmp && (setsid ./frida-server >/data/local/tmp/frida.log 2>&1 &)" | Out-Null
    Start-Sleep -Seconds 5
    $pid2 = ((Sh 'pidof frida-server') -join '').Trim()
    if ($pid2) { Say "frida-server started (pid $pid2)"; return $true }
    Say ('frida-server failed to start: ' + ((Sh 'cat /data/local/tmp/frida.log') -join ' '))
    return $false
}

function Ensure-Forward {
    if (Get-FridaForward) { Say "adb forward already present"; return $true }
    $null = & $AdbExe -s $SveSerial forward --remove-all 2>&1
    & $AdbExe -s $SveSerial forward "tcp:$FridaPort" tcp:27042 2>&1 | Out-Null
    Start-Sleep -Seconds 1
    if (Get-FridaForward) { Say "adb forward tcp:$FridaPort -> 27042 created"; return $true }
    Say 'adb forward failed'
    return $false
}

function Start-Mitm {
    if (Get-MitmListener) { Say "mitmproxy already listening on $MitmPort"; return $true }
    if (-not (Test-Path $VenvPython)) { Say "venv python missing: $VenvPython"; return $false }
    $launcher = 'import sys; from mitmproxy.tools import main; sys.argv=[''mitmdump'']+sys.argv[1:]; main.mitmdump()'
    $argLine = '-c "' + $launcher + '" --set confdir="' + $ConfDir + '" -p ' + $MitmPort +
               ' -s "' + $CaptureAddon + '" -w "' + $CaptureFlow + '"'
    $p = Start-Process -FilePath $VenvPython -ArgumentList $argLine `
            -RedirectStandardOutput $MitmLogOut -RedirectStandardError $MitmLogErr `
            -WindowStyle Hidden -PassThru
    Set-Content -Path $MitmPidFile -Value $p.Id
    Start-Sleep -Seconds 6
    if (Get-MitmListener) { Say "mitmproxy started (pid $($p.Id))"; return $true }
    Say 'mitmproxy failed to start'
    return $false
}

function Set-DeviceProxy {
    Sh "settings put global http_proxy 10.0.2.2:$MitmPort" | Out-Null
    Sh "settings put global global_http_proxy_host 10.0.2.2" | Out-Null
    Sh "settings put global global_http_proxy_port $MitmPort" | Out-Null
    Say ('device proxy -> 10.0.2.2:' + $MitmPort + '  (now ' + ((Sh 'settings get global http_proxy') -join '').Trim() + ')')
}

function Clear-DeviceProxy {
    Sh 'settings put global http_proxy :0' | Out-Null
    Sh 'settings delete global global_http_proxy_host' | Out-Null
    Sh 'settings delete global global_http_proxy_port' | Out-Null
    Say 'device proxy cleared'
}

function Spawn-App {
    if (-not (Test-Path $Payload)) { Say "payload bundle missing: $Payload (run build_payload.js)"; return $false }
    Sh 'am force-stop com.shadowversecard' | Out-Null
    Start-Sleep -Seconds 2
    $script = Join-Path $PSScriptRoot 'frida_run.py'
    $argLine = '-u "' + $script + '" --addr 127.0.0.1:' + $FridaPort + ' --seconds 7200'
    # stdout and stderr must go to different files: Start-Process rejects equal paths.
    $p = Start-Process -FilePath $VenvPython -ArgumentList $argLine `
            -RedirectStandardOutput $FridaLog -RedirectStandardError $FridaErr `
            -WindowStyle Hidden -PassThru
    Set-Content -Path $FridaPid -Value $p.Id
    Say "app spawned under frida (launcher pid $($p.Id)); payload log -> capture/frida.log"
    return $true
}

if ($Status) {
    $mitmState = 'not running'
    if (Get-MitmListener) { $mitmState = 'LISTENING on ' + $MitmPort }
    $fridaState = ((Sh 'pidof frida-server') -join '').Trim()
    if (-not $fridaState) { $fridaState = 'not running' }
    $fwd = ((Get-FridaForward) -join ' ').Trim()
    if (-not $fwd) { $fwd = 'none' }
    $devProxy = ((Sh 'settings get global http_proxy') -join '').Trim()
    $appPid = ((Sh 'pidof com.shadowversecard') -join '').Trim()
    if (-not $appPid) { $appPid = 'not running' }
    $recs = 0
    if (Test-Path $CaptureJsonl) { $recs = (Get-Content $CaptureJsonl).Count }
    Say ('mitmproxy   : ' + $mitmState)
    Say ('frida-server: ' + $fridaState)
    Say ('adb forward : ' + $fwd)
    Say ('device proxy: ' + $devProxy)
    Say ('app pid     : ' + $appPid)
    Say ('records     : ' + $recs)
    exit 0
}

if ($Stop) {
    Sh 'am force-stop com.shadowversecard' | Out-Null
    Clear-DeviceProxy
    if (Test-Path $FridaPid) {
        $fp = Get-Content $FridaPid -ErrorAction SilentlyContinue
        if ($fp) { Stop-Process -Id $fp -Force -ErrorAction SilentlyContinue; Say "stopped frida launcher pid=$fp" }
        Remove-Item $FridaPid -Force -ErrorAction SilentlyContinue
    }
    Get-MitmListener | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
    Say 'mitmproxy stopped'
    Sh 'pkill -f frida-server' | Out-Null
    Say 'frida-server stopped'
    & $AdbExe -s $SveSerial forward --remove-all 2>&1 | Out-Null
    Say 'done'
    exit 0
}

if (-not $Start) { Say 'pass -Start, -Stop or -Status'; exit 1 }

Say '=== 1/6 adb root ==='
& $AdbExe -s $SveSerial root 2>&1 | Out-Null
Start-Sleep -Seconds 3
Say ('id: ' + ((Sh 'id') -join '').Trim())

Say '=== 2/6 frida-server ==='
if (-not (Ensure-FridaServer)) { exit 1 }

Say '=== 3/6 adb forward ==='
if (-not (Ensure-Forward)) { exit 1 }

Say '=== 4/6 mitmproxy ==='
if (-not (Start-Mitm)) { exit 1 }

Say '=== 5/6 device proxy ==='
Set-DeviceProxy

if ($NoSpawn) { Say '=== 6/6 skipped (-NoSpawn) ==='; exit 0 }

Say '=== 6/6 spawn app ==='
if (-not (Spawn-App)) { exit 1 }
Say ''
Say 'ready. In the emulator tap the 赛事 tab; every request is logged to capture/sve_requests.jsonl'
