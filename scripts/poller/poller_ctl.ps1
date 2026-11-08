# 轮询器启停控制：以**脱离会话的独立进程**方式启动 poll.py。
#
# 为什么需要这个脚本：
#   直接用后台作业（background job）启动轮询器时，它的父进程是作业包装进程；
#   作业被取消或会话清理时，轮询器会被**连带杀掉**——曾因此出现过
#   "以为在监控、其实早就停了"。本项目要长时间值守（赛事窗口跨天），
#   所以必须用 Start-Process 独立启动，并用 pid 文件判定存活。
#
# 与 poll.py 自身的关系：
#   poll.py 内部还有一层单实例锁（capture/poller.pid），两者互补：
#   本脚本负责"以正确方式拉起进程"，poll.py 负责"拒绝重复实例"。
#
# 用法:
#   pwsh -NoProfile -File scripts\poller\poller_ctl.ps1 -Start
#   pwsh -NoProfile -File scripts\poller\poller_ctl.ps1 -Status
#   pwsh -NoProfile -File scripts\poller\poller_ctl.ps1 -Stop
param(
    [switch]$Start,
    [switch]$Stop,
    [switch]$Status
)

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot '..\config.ps1')

$scriptFile = Join-Path $PSScriptRoot 'poll.py'
$pidFile    = Join-Path $CaptureDir 'poller_ctl.pid'
$outLog     = Join-Path $CaptureDir 'poller_stdout.log'
$errLog     = Join-Path $CaptureDir 'poller_stderr.log'
$lockFile   = Join-Path $CaptureDir 'poller.pid'

function Say($m) { Write-Host "[pollerctl] $m" }

function Get-PollerPid {
    if (-not (Test-Path $pidFile)) { return 0 }
    $v = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $v) { return 0 }
    if (Get-Process -Id ([int]$v) -ErrorAction SilentlyContinue) { return [int]$v }
    return 0
}

if ($Status) {
    $alive = Get-PollerPid
    if ($alive) {
        Say ("poll.py 运行中 pid=$alive  启动于 " + (Get-Process -Id $alive).StartTime)
    } else {
        Say 'poll.py 未运行（本脚本启动的实例）'
    }
    # poll.py 自己的锁文件反映的是"守护实例"，两者可能差一个包装进程
    if (Test-Path $lockFile) {
        $lp = Get-Content $lockFile -ErrorAction SilentlyContinue | Select-Object -First 1
        $ok = $lp -and (Get-Process -Id ([int]$lp) -ErrorAction SilentlyContinue)
        Say ("内部单实例锁: pid=$lp " + $(if ($ok) { '(存活)' } else { '(已失效)' }))
    } else {
        Say '内部单实例锁: 无'
    }
    $log = Join-Path $CaptureDir 'poll.log'
    if (Test-Path $log) { Say ('poll.log 最后写入: ' + (Get-Item $log).LastWriteTime) }
    exit 0
}

if ($Stop) {
    $alive = Get-PollerPid
    if ($alive) { Stop-Process -Id $alive -Force -ErrorAction SilentlyContinue; Say "已停止 pid=$alive" }
    else { Say '未运行' }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    exit 0
}

if (-not $Start) { Say 'pass -Start, -Stop or -Status'; exit 1 }

$existing = Get-PollerPid
if ($existing) { Say "已在运行 pid=$existing（如需重启先 -Stop）"; exit 0 }

if (-not (Test-Path $scriptFile)) { Say "缺少 $scriptFile"; exit 1 }
if (-not (Test-Path $VenvPython)) { Say "缺少 venv python: $VenvPython"; exit 1 }

# 独立启动：不挂在当前会话的作业下，会话清理不会把它带走
$p = Start-Process -FilePath $VenvPython `
        -ArgumentList @('-u', $scriptFile) `
        -WorkingDirectory $ProjectRoot `
        -RedirectStandardOutput $outLog `
        -RedirectStandardError $errLog `
        -WindowStyle Hidden -PassThru
Set-Content -Path $pidFile -Value $p.Id
Start-Sleep -Seconds 6

if (Get-Process -Id $p.Id -ErrorAction SilentlyContinue) {
    Say "已启动 pid=$($p.Id)"
    Say ("  标准输出: " + (Split-Path $outLog -Leaf))
    Say ("  日志    : capture\poll.log")
} else {
    Say '启动失败，请查看 capture/poller_stderr.log'
    Get-Content $errLog -ErrorAction SilentlyContinue | Select-Object -Last 5 | ForEach-Object { Say ('  ' + $_) }
    exit 1
}
