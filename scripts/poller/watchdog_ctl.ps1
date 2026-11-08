# 启动看护进程并写入 pid 文件，便于可靠地判断它是否真的在运行。
# ASCII only (PS 5.1 parses scripts as ANSI).
#
# 为什么需要：
#   之前用后台作业(background job)启动看护进程，作业被取消或会话清理时
#   会把子进程一起杀掉，导致"以为在看护、其实早就死了"。
#   这里用 Start-Process 独立启动并把 pid 落盘，判断存活只看 pid 文件，
#   不要用命令行匹配 —— 曾经把自己的诊断命令误判成看护进程。
#
# 用法:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\poller\watchdog_ctl.ps1 -Start
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\poller\watchdog_ctl.ps1 -Status
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\poller\watchdog_ctl.ps1 -Stop
param(
    [switch]$Start,
    [switch]$Stop,
    [switch]$Status,
    [int]$CheckSeconds = 600,
    [int]$StaleMinutes = 10
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$script = Join-Path $PSScriptRoot 'watchdog.ps1'
$pidFile = Join-Path $root 'capture\watchdog.pid'
$outLog = Join-Path $root 'capture\watchdog_stdout.log'
$errLog = Join-Path $root 'capture\watchdog_stderr.log'

function Say($m) { Write-Host "[wdctl] $m" }

function Get-WatchdogPid {
    if (-not (Test-Path $pidFile)) { return 0 }
    $v = (Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    if (-not $v) { return 0 }
    $proc = Get-Process -Id ([int]$v) -ErrorAction SilentlyContinue
    if ($proc) { return [int]$v }
    return 0
}

if ($Status) {
    $alive = Get-WatchdogPid
    if ($alive) {
        Say ("watchdog 运行中 pid=$alive  启动于 " + (Get-Process -Id $alive).StartTime)
    } else {
        Say 'watchdog 未运行（pid 文件无效或进程已退出）'
    }
    $log = Join-Path $root 'capture\watchdog.log'
    if (Test-Path $log) {
        Say ("日志最后写入: " + (Get-Item $log).LastWriteTime)
    }
    exit 0
}

if ($Stop) {
    $alive = Get-WatchdogPid
    if ($alive) { Stop-Process -Id $alive -Force -ErrorAction SilentlyContinue; Say "已停止 pid=$alive" }
    else { Say '未运行' }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

if (-not $Start) { Say 'pass -Start, -Stop or -Status'; exit 1 }

$existing = Get-WatchdogPid
if ($existing) { Say "已在运行 pid=$existing（如需重启先 -Stop）"; exit 0 }

# 优先用 PowerShell 7（pwsh）：UTF-8 无 BOM、无需 ExecutionPolicy Bypass、空值检查更严格。
# 注意：环境变量无法把 powershell.exe "指向" PS7（两者文件名不同），只能显式调用 pwsh。
# 找不到 pwsh 时回退到 Windows PowerShell 5.1，保证旧机器仍可用。
$shellExe = 'powershell.exe'
$pwshCmd = Get-Command pwsh.exe -ErrorAction SilentlyContinue
if (-not $pwshCmd) {
    $cand = 'C:\Program Files\PowerShell\7\pwsh.exe'
    if (Test-Path $cand) { $pwshCmd = [pscustomobject]@{ Source = $cand } }
}
if ($pwshCmd) {
    $shellExe = $pwshCmd.Source
    Say ('使用 PowerShell 7: ' + $shellExe)
} else {
    Say '未找到 pwsh，回退到 Windows PowerShell 5.1'
}

$argLine = '-NoProfile -ExecutionPolicy Bypass -File "' + $script + '" -CheckSeconds ' +
           $CheckSeconds + ' -StaleMinutes ' + $StaleMinutes
$p = Start-Process -FilePath $shellExe -ArgumentList $argLine `
        -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
        -WindowStyle Hidden -PassThru
Set-Content -Path $pidFile -Value $p.Id
Start-Sleep -Seconds 3
$alive = Get-WatchdogPid
if ($alive) { Say "已启动 pid=$alive（检查间隔 ${CheckSeconds}s，阈值 ${StaleMinutes}min）" }
else { Say '启动失败，请查看 capture/watchdog_stderr.log'; exit 1 }
