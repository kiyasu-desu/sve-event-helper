# Set / clear the emulator global HTTP proxy so traffic goes through mitmproxy.
# ASCII only: PowerShell 5.1 parses script files as ANSI.
#
# Defaults to the "sve" VM (127.0.0.1:16384). Never pass the pjsk serial unless
# you really mean it: pjsk is an unrelated instance that this project must not touch.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\device\set_proxy.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\device\set_proxy.ps1 -HostAddress 192.0.2.10
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\device\set_proxy.ps1 -Off
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\device\set_proxy.ps1 -Status
param(
    [string]$Serial      = '',
    [string]$Adb         = '',
    [string]$HostAddress = '10.0.2.2',
    [int]$Port           = 0,
    [switch]$Off,
    [switch]$Status
)

$ErrorActionPreference = 'Continue'

# Shared settings (paths, ports, adb/mitmdump locations) live in scripts\config.ps1.
. (Join-Path $PSScriptRoot '..\config.ps1')

if (-not $Serial) { $Serial = $SveSerial }
if (-not $Adb)    { $Adb    = $AdbExe }
if ($Port -le 0)  { $Port   = $MitmPort }

function Say($m) { Write-Host "[proxy] $m" }
function Sh([string]$c) { (& $Adb -s $Serial shell $c 2>&1) }

function ShowStatus {
    Say ('http_proxy        = ' + ((Sh 'settings get global http_proxy') -join ' ').Trim())
    Say ('proxy host        = ' + ((Sh 'settings get global global_http_proxy_host') -join ' ').Trim())
    Say ('proxy port        = ' + ((Sh 'settings get global global_http_proxy_port') -join ' ').Trim())
    Say ('tun/ppp ifaces    = ' + (((Sh 'ip -o link show') | Where-Object { $_ -match 'tun|ppp' }) -join ' ; ').Trim())
}

if ($Status) { ShowStatus; exit 0 }

if ($Off) {
    Sh 'settings put global http_proxy :0'
    Sh 'settings delete global global_http_proxy_host'
    Sh 'settings delete global global_http_proxy_port'
    Say 'proxy cleared'
    ShowStatus
    exit 0
}

# Set all three forms: different Android versions and ROMs read different keys.
Sh ('settings put global http_proxy ' + $HostAddress + ':' + $Port)
Sh ('settings put global global_http_proxy_host ' + $HostAddress)
Sh ('settings put global global_http_proxy_port ' + $Port)
Say ('proxy set -> ' + $HostAddress + ':' + $Port)
ShowStatus
