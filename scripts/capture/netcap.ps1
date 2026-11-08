# Start/stop the in-app capture (frida-server + adb forward + netcap attach).
#
# Why capture by hooking instead of a proxy: the mitmproxy approach broke the app
# whenever it restarted without hooks (every HTTPS request failed certificate
# validation) and dragged NTP/overseas traffic through the proxy. Hooking OkHttp
# inside the app needs no proxy and leaves the app on a direct connection.
#
# Usage (defaults to the sve instance):
#   powershell -NoProfile -File scripts\capture\netcap.ps1 -Start
#   powershell -NoProfile -File scripts\capture\netcap.ps1 -Status
#   powershell -NoProfile -File scripts\capture\netcap.ps1 -Stop
#   powershell -NoProfile -File scripts\capture\netcap.ps1 -Start -Target sve2
#
# Each target keeps its own host port, pid file and jsonl, so several instances
# can be captured at the same time without interfering with each other.
param(
    [switch]$Start,
    [switch]$Stop,
    [switch]$Status,
    [ValidateSet('sve', 'sve2')]
    [string]$Target = 'sve'
)

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '..\config.ps1')

# --- per-target settings ------------------------------------------------------
switch ($Target) {
    'sve'  { $Serial = $SveSerial;  $FridaPort = 27043 }
    'sve2' { $Serial = $Sve2Serial; $FridaPort = 27044 }
}

$FridaRemote = '/data/local/tmp/frida-server'
$FridaBin    = Join-Path $ProjectRoot 'vendor\frida\frida-server'
$Bundle      = Join-Path $CaptureDir 'netcap.bundle.js'
$Jsonl       = Join-Path $CaptureDir ("net_capture_${Target}.jsonl")
$OutLog      = Join-Path $CaptureDir ("netcap_${Target}_out.log")
$ErrLog      = Join-Path $CaptureDir ("netcap_${Target}_err.log")
$PidFile     = Join-Path $CaptureDir ("netcap_${Target}.pid")
$BuildJs     = Join-Path $PSScriptRoot 'build_payload.js'
$Runner      = Join-Path $PSScriptRoot 'netcap_run.py'

function Say($m) { Write-Host "[netcap:$Target] $m" }

function Sh([string]$c) {
    $out = & $AdbExe -s $Serial shell $c 2>&1
    if ($null -eq $out) { return , @() }
    return $out
}

function Get-FridaPid { ((Sh 'pidof frida-server') -join '').Trim() }

function Get-ForwardLine {
    # @() wrapper keeps this safe when adb prints nothing (PS7 is strict about
    # calling methods on $null, PS5.1 silently tolerated it).
    @((& $AdbExe -s $Serial forward --list 2>&1) | Where-Object { $_ -match "tcp:$FridaPort" })
}

function Get-RecordCount {
    if (-not (Test-Path $Jsonl)) { return 0 }
    return (Get-Content $Jsonl -ErrorAction SilentlyContinue | Measure-Object).Count
}

# --- status ------------------------------------------------------------------
if ($Status) {
    $frida = Get-FridaPid
    if (-not $frida) { $frida = 'not running' }
    $fwd = (@(Get-ForwardLine) -join ' ').Trim()
    if (-not $fwd) { $fwd = 'none' }
    $appPid = ((Sh 'pidof com.shadowversecard') -join '').Trim()
    if (-not $appPid) { $appPid = 'not running' }
    $proxy = ((Sh 'settings get global http_proxy') -join '').Trim()
    if (-not $proxy -or $proxy -eq 'null') { $proxy = ':0' }
    if ($proxy -eq ':0') { $proxy = ':0  (clean)' }

    Say ('target       : ' + $Target + '  (' + $Serial + ')')
    Say ('device online: ' + (Test-DeviceOnline -Serial $Serial))
    Say ('frida-server : ' + $frida)
    Say ('adb forward  : ' + $fwd)
    Say ('app pid      : ' + $appPid)
    Say ('net records  : ' + (Get-RecordCount) + '  (' + (Split-Path $Jsonl -Leaf) + ')')
    Say ('device proxy : ' + $proxy + '   (must stay :0 - the app should stay direct)')
    exit 0
}

# --- stop --------------------------------------------------------------------
if ($Stop) {
    if (Test-Path $PidFile) {
        $v = Get-Content $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($v) {
            Stop-Process -Id ([int]$v) -Force -ErrorAction SilentlyContinue
            Say ('stopped attach launcher pid=' + $v)
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }
    Sh 'pkill -f frida-server' | Out-Null
    & $AdbExe -s $Serial forward --remove-all 2>&1 | Out-Null
    Say 'frida-server stopped, forward removed'
    exit 0
}

if (-not $Start) { Say 'pass -Start, -Stop or -Status'; exit 1 }

# --- start -------------------------------------------------------------------
if (-not $AdbExe) { Say 'adb not found (set SVE_ADB to override)'; exit 1 }
if (-not (Test-DeviceOnline -Serial $Serial)) {
    Say ('device not reachable: ' + $Serial + '  (target=' + $Target + ', is that emulator running?)')
    exit 1
}

Say '1/4 adb root'
& $AdbExe -s $Serial root 2>&1 | Out-Null
Start-Sleep -Seconds 2

Say '2/4 frida-server'
if (-not (Get-FridaPid)) {
    if (-not (((Sh ('ls ' + $FridaRemote + ' 2>/dev/null')) -join '').Trim())) {
        if (-not (Test-Path $FridaBin)) { Say ('missing ' + $FridaBin); exit 1 }
        & $AdbExe -s $Serial push $FridaBin $FridaRemote 2>&1 | Out-Null
        Sh ('chmod 755 ' + $FridaRemote) | Out-Null
    }
    Sh 'cd /data/local/tmp && (setsid ./frida-server >/data/local/tmp/frida.log 2>&1 &)' | Out-Null
    Start-Sleep -Seconds 5
}
$frida = Get-FridaPid
if (-not $frida) {
    Say 'frida-server failed to start; last log lines:'
    (Sh 'cat /data/local/tmp/frida.log') | Select-Object -Last 5 | ForEach-Object { Say ('   ' + $_) }
    exit 1
}
Say ('   pid ' + $frida)

Say '3/4 adb forward'
if (-not (Get-ForwardLine)) {
    & $AdbExe -s $Serial forward --remove-all 2>&1 | Out-Null
    & $AdbExe -s $Serial forward ("tcp:" + $FridaPort) tcp:27042 2>&1 | Out-Null
    Start-Sleep -Seconds 1
}
if (-not (Get-ForwardLine)) { Say 'adb forward failed'; exit 1 }
Say ('   tcp:' + $FridaPort + ' -> device 27042')

Say '4/4 build + attach'
if (-not $NodeExe) { Say 'node not found (needed to bundle the payload)'; exit 1 }
& $NodeExe $BuildJs 'netcap.js' $Bundle 2>&1 | ForEach-Object { Say ('   ' + $_) }
if (-not (Test-Path $Bundle)) { Say ('missing ' + $Bundle); exit 1 }

# --out is absolute on purpose: a relative path would resolve against the caller's cwd.
$argLine = '-u "' + $Runner + '" --addr 127.0.0.1:' + $FridaPort +
           ' --serial ' + $Serial + ' --out "' + $Jsonl + '" --watch --seconds 14400'
$p = Start-Process -FilePath $VenvPython -ArgumentList $argLine `
        -RedirectStandardOutput $OutLog -RedirectStandardError $ErrLog `
        -WindowStyle Hidden -PassThru
Set-Content -Path $PidFile -Value $p.Id

Say ('   attach launcher pid ' + $p.Id + '  (log: ' + (Split-Path $OutLog -Leaf) + ')')
Say ''
Say ('ready. records -> ' + (Split-Path $Jsonl -Leaf))
