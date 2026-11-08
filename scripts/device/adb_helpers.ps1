# Small adb helpers for this project (ASCII only: PS 5.1 parses scripts as ANSI).
#
# Defaults to the "sve" VM (127.0.0.1:16384). Do not point it at pjsk (16416).
#
# Usage:
#   . scripts\device\adb_helpers.ps1
#   AdbShell 'id'
#   AdbStatus
#   AdbFetch 'https://api-sve.nie.netease.com/sve/api/v1/server_list'   # needs curl on the device
param(
    [string]$Serial = '',
    [string]$Adb    = ''
)

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '..\config.ps1')
if (-not $Serial) { $Serial = $SveSerial }
if (-not $Adb)    { $Adb    = $AdbExe }

function AdbShell([string]$cmd) { & $Adb -s $Serial shell $cmd 2>&1 }

# Note: stock MuMu images have no curl; this only works if the guest ships one.
function AdbFetch([string]$url) { AdbShell "curl -sS -m 20 '$url'" }

function AdbStatus {
    Write-Host ("device   : " + ((AdbShell 'id') -join ' ').Trim())
    Write-Host ("proxy    : " + ((AdbShell 'settings get global http_proxy') -join ' ').Trim())
    Write-Host ("mounts   : " + (((AdbShell 'cat /proc/mounts') | Where-Object { $_ -match '\s/\s' }) -join ' ').Trim())
}

Write-Host "[adb] helpers loaded (serial=$Serial)"
