# Install the mitmproxy CA into the emulator's SYSTEM certificate store.
#
# Why: since Android 7 an app only trusts system CAs, not user CAs.
#      The "sve" VM is Android 15, so a system CA is required.
#      The file name must be OpenSSL subject_hash_old (the OLD algorithm),
#      not the new subject_hash.
#
# STATUS / KNOWN LIMITATION (measured 2026-10-09 on MuMu 6.6.4.0 / Android 15):
#   The root filesystem is read-only under dm-verity. This MuMu build has no
#   'adb remount' and no 'adb disable-verity' (both are stripped: exit 127), and
#   'mount -o rw,remount /' is refused ('/dev/block/sda6' is read-only).
#   So the copy step below CANNOT succeed yet -- the script detects that and
#   exits with code 2 instead of writing anything. -Check is safe and useful.
#   Do not expect a successful install until the write path problem is solved.
#
# Usage (PowerShell 5.1):
#   ... -File scripts\device\install_ca.ps1 -Check        # inspect only, safe
#   ... -File scripts\device\install_ca.ps1               # attempt install
#   ... -File scripts\device\install_ca.ps1 -Uninstall    # remove if present
#
# Defaults to the "sve" VM (127.0.0.1:16384). Only the emulator's system cert
# dir is touched; the host trust store is never modified.
param(
    [string]$Serial   = '',
    [string]$Adb      = '',
    [string]$Cert     = '',
    [string]$OpenSsl  = '',
    [switch]$Uninstall,
    [switch]$Check,
    [switch]$NoReboot
)

$ErrorActionPreference = 'Continue'   # native tools write warnings to stderr; do not treat those as fatal

# Shared settings (paths, ports, adb/mitmdump locations) live in scripts\config.ps1.
. (Join-Path $PSScriptRoot '..\config.ps1')

if (-not $Serial) { $Serial = $SveSerial }
if (-not $Adb)    { $Adb    = $AdbExe }
if (-not $Cert)   { $Cert   = $CaCertPem }

function Say($m) { Write-Host "[CA] $m" }
function Sh([string]$c) {
    $out = & $Adb -s $Serial shell $c 2>&1
    return $out
}
function ShQuiet([string]$c) { & $Adb -s $Serial shell $c 2>&1 | Out-Null }
function RootMountLine {
    # Read mounts raw and filter on the host side: avoids all adb/PS quote marshalling pain.
    $lines = Sh 'cat /proc/mounts'
    $picked = $lines | Where-Object { $_ -match '\s/\s' -or $_ -match '\s/system\s' }
    ($picked -join ' ; ').Trim()
}

if (-not (Test-Path $Adb))  { throw "adb not found: $Adb" }
if (-not (Test-Path $Cert)) { throw "mitmproxy cert not found: $Cert (run mitmdump once first)" }

# ---- locate openssl.exe (authoritative subject_hash_old) ----
if (-not $OpenSsl) { $OpenSsl = Get-OpenSslPath }
if (-not $OpenSsl) { throw "openssl.exe not found; cannot compute the system CA file name" }
Say "openssl: $OpenSsl"

$info = & $OpenSsl x509 -in $Cert -noout -subject -subject_hash -subject_hash_old 2>&1
Say ($info -join ' | ')
$hashOld = ($info | Where-Object { $_ -match '^[0-9a-f]{8}$' } | Select-Object -Last 1)
if (-not $hashOld) { throw "cannot parse subject_hash_old from: $($info -join ' ')" }
$hashOld = $hashOld.Trim()
$fileName = $hashOld + '.0'
Say "system CA file name = $fileName"

$der = [System.IO.Path]::ChangeExtension($Cert, '.der')
& $OpenSsl x509 -in $Cert -outform der -out $der 2>&1 | Out-Null

if ($Check) {
    Say 'check mode: device is not modified'
    Say ('device id = ' + (Sh 'id').Trim())
    Say ('root mounts = ' + (RootMountLine))
    Say ('existing file = ' + ((Sh "ls -l /system/etc/security/cacerts/$fileName 2>/dev/null || echo none") -join ' ').Trim())
    Say ('proxy = ' + ((Sh 'settings get global http_proxy') -join ' ').Trim())
    exit 0
}

# ---- make sure adbd is root ----
$id = (Sh 'id').Trim()
Say "device id: $id"
if ($id -notmatch 'uid=0') {
    Say 'adbd is not root, trying adb root ...'
    & $Adb -s $Serial root | Out-Null
    Start-Sleep -Seconds 3
    $id = (Sh 'id').Trim()
    if ($id -notmatch 'uid=0') { throw 'cannot obtain root (adb root failed)' }
    Say "device id: $id"
}

$targets = @(
    '/system/etc/security/cacerts',
    '/apex/com.android.conscrypt/cacerts'
)

if ($Uninstall) {
    foreach ($t in $targets) {
        Sh "rm -f $t/$fileName" | Out-Null
        $r = Sh "ls $t/$fileName 2>/dev/null"
        if ($r) { Say "uninstall $t -> STILL THERE" } else { Say "uninstall $t -> removed" }
    }
    Say "reboot to take effect: & '$Adb' -s $Serial reboot"
    exit 0
}

# ---- remount root writable (Android 10+: /system is a dir under /) ----
Say 'trying to remount root read-write ...'
ShQuiet 'mount -o rw,remount /'
ShQuiet 'mount -o rw,remount /system'
$rootLine = RootMountLine
Say "root mount: $rootLine"
if ($rootLine -match ' ro,') {
    Say 'still read-only, trying adb remount ...'
    & $Adb -s $Serial remount 2>&1 | ForEach-Object { Say ('  ' + $_) }
    $rootLine = RootMountLine
    Say "root mount after adb remount: $rootLine"
}
if ($rootLine -match ' ro,') {
    Say 'root is still read-only. Attempting adb disable-verity + reboot (needs root) ...'
    & $Adb -s $Serial disable-verity 2>&1 | ForEach-Object { Say ('  ' + $_) }
    Say 'verity disabled (if it reported success). Reboot the emulator, then rerun this script.'
    exit 2
}

# ---- push cert and copy into the system stores ----
& $Adb -s $Serial push $der /data/local/tmp/sve_ca.der | Out-Null
Sh 'chmod 644 /data/local/tmp/sve_ca.der' | Out-Null
foreach ($t in $targets) {
    $exists = Sh "ls -d $t 2>/dev/null"
    if (-not $exists) { Say "skip (missing): $t"; continue }
    Sh "cp /data/local/tmp/sve_ca.der $t/$fileName" | Out-Null
    Sh "chmod 644 $t/$fileName" | Out-Null
    Sh "chown root:root $t/$fileName" | Out-Null
    Sh "restorecon $t/$fileName 2>/dev/null" | Out-Null
    $r = (Sh "ls -l $t/$fileName 2>/dev/null") -join ' '
    Say "installed $t/$fileName -> $($r.Trim())"
}

# ---- verify ----
$ok = $true
foreach ($t in $targets) {
    $r = (Sh "ls -l $t/$fileName 2>/dev/null") -join ' '
    if (-not $r) { $ok = $false; Say "VERIFY FAILED: $t/$fileName missing" }
    else { Say "verified $t/$fileName" }
}
if (-not $ok) { throw 'certificate verification failed' }

# ---- make sure the guest still trusts it after reboot: copy is persistent in the image ----
if (-not $NoReboot) {
    Say 'rebooting emulator so the system CA store is reloaded ...'
    & $Adb -s $Serial reboot | Out-Null
    Say 'reboot sent. Reconnect afterwards with: adb connect 127.0.0.1:16416'
}
