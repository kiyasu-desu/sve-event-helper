# 本项目的共享配置：所有 PowerShell 脚本都从这里读取路径与设备信息。
#
# 用法（按脚本所在层级 dot-source）：
#   . (Join-Path $PSScriptRoot '..\config.ps1')      # scripts\device、scripts\capture 等
#   . (Join-Path $PSScriptRoot '..\..\config.ps1')   # 更深一层
#
# 设计原则：
#   1. 任何路径都可以用环境变量覆盖（便于换机器、迁移服务器）；
#   2. 找不到覆盖值时按"常见位置 → PATH"的顺序自动发现，不写死单一位置；
#   3. 本文件只读环境，不写任何东西。
#
# 语法上保持 Windows PowerShell 5.1 兼容（不使用 ?? / 三元 / && ），
# 这样在 PS5.1 与 PS7 下都能加载。

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path

# ------------------------------------------------------------------ 工具发现
function script:Get-ToolPath {
    <#
      按 环境变量 → 候选路径 → PATH 的顺序解析一个可执行文件。
      -EnvVar   : 优先读取的环境变量名
      -Names    : 可执行文件名（PATH 查找用）
      -Paths    : 候选绝对路径
    #>
    param(
        [string]$EnvVar,
        [string[]]$Names = @(),
        [string[]]$Paths = @()
    )
    if ($EnvVar) {
        $v = [Environment]::GetEnvironmentVariable($EnvVar)
        if ($v -and (Test-Path $v)) { return (Resolve-Path $v).Path }
    }
    foreach ($p in $Paths) {
        if ($p -and (Test-Path $p)) { return (Resolve-Path $p).Path }
    }
    foreach ($n in $Names) {
        $cmd = Get-Command $n -ErrorAction SilentlyContinue
        if ($cmd -and $cmd.Source) { return $cmd.Source }
    }
    return $null
}

# ------------------------------------------------------------------ 模拟器实例
# 实例名来自各实例的 configs\extra_config.json 里的 playerName：
#   sve  -> adb 16384  监控用（大奖赛报名监控就跑在这个实例上）
#   sve2 -> adb 16448  第二账号：用于验证登录流程，不影响 sve 的会话
#   pjsk -> adb 16416  其它用途，本项目【不要碰】
$SveSerial     = '127.0.0.1:16384'
$Sve2Serial    = '127.0.0.1:16448'
$PjskSerial    = '127.0.0.1:16416'
$DefaultSerial = $SveSerial

# adb：MuMu 自带；换机器/换模拟器时用 SVE_ADB 覆盖
$AdbExe = script:Get-ToolPath -EnvVar 'SVE_ADB' -Names @('adb.exe', 'adb') -Paths @(
    'E:\MuMuPlayer\nx_main\adb.exe',
    'C:\Program Files\Netease\MuMuPlayer-12.0\shell\adb.exe',
    (Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe')
)

# ------------------------------------------------------------------ Python / 依赖
# 事件驱动工具（poll/notify/action/session_manager）所需的第三方库。
# 用于自检提示，避免"跑起来才发现缺包"。
$RequiredPythonModules = @('requests')

# ------------------------------------------------------------------ 私有虚拟环境
$VenvDir    = Join-Path $ProjectRoot 'vendor\venv314'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'      # Windows
if (-not (Test-Path $VenvPython)) {
    $alt = Join-Path $VenvDir 'bin/python'                # Linux 布局，便于日后迁移
    if (Test-Path $alt) { $VenvPython = $alt }
}

# 系统 Python：仅用于"创建 venv"和"运行 scripts\recon 下的纯标准库脚本"
$SystemPython = script:Get-ToolPath -Names @('python.exe', 'python', 'python3')

# node：仅用于打包 Frida payload（esbuild）
$NodeExe = script:Get-ToolPath -Names @('node.exe', 'node')

# openssl：仅用于计算证书哈希（签名算法已还原，通常用不到）
$OpenSslExe = script:Get-ToolPath -Names @('openssl.exe', 'openssl') -Paths @(
    'D:\miniconda\Library\bin\openssl.exe',
    'C:\Program Files\Git\usr\bin\openssl.exe'
)

# ------------------------------------------------------------------ 目录与产物
$CaptureDir = Join-Path $ProjectRoot 'capture'
$ReconDir   = Join-Path $ProjectRoot 'recon'
$ApkPath    = Join-Path $ProjectRoot 'apk\sve-1.2.0.apk'

# 轮询器运行时产物
$StateFile      = Join-Path $CaptureDir 'state.json'
$SamplesFile    = Join-Path $CaptureDir 'samples.jsonl'
$PollLog        = Join-Path $CaptureDir 'poll.log'
$SessionFile    = Join-Path $CaptureDir 'session.json'
$CompletionFile = Join-Path $CaptureDir 'completed.json'

# 看护进程
$WatchdogScript = Join-Path $ProjectRoot 'scripts\poller\watchdog.ps1'
$WatchdogPid    = Join-Path $CaptureDir 'watchdog.pid'
$WatchdogLog    = Join-Path $CaptureDir 'watchdog.log'

# ------------------------------------------------------------------ HTTP 代理方案
# 已废弃（见 README「抓包方案」一节）：代理会让 APP 在 hook 丢失后所有 HTTPS 失败，
# 导致页面卡死。现在改用 Frida 在 APP 内部 hook。以下变量仅为保留旧脚本可用。
$ConfDir      = Join-Path $CaptureDir 'confdir'
$CaptureFlow  = Join-Path $CaptureDir 'capture.flow'
$CaptureJsonl = Join-Path $CaptureDir 'sve_requests.jsonl'
$MitmLogOut   = Join-Path $CaptureDir 'mitm_out.log'
$MitmLogErr   = Join-Path $CaptureDir 'mitm_err.log'
$MitmPidFile  = Join-Path $CaptureDir 'mitm.pid'
$CaptureAddon = Join-Path $ProjectRoot 'scripts\capture\sve_capture.py'
$MitmPort     = 8080
$CaCertPem    = Join-Path $ConfDir 'mitmproxy-ca-cert.pem'

# ------------------------------------------------------------------ 自检
function Test-ProjectPaths {
    $pythonOk = $false
    $missing = @()
    if ($VenvPython -and (Test-Path $VenvPython)) {
        try {
            & $VenvPython -c "import requests" 2>$null
            $pythonOk = ($LASTEXITCODE -eq 0)
        } catch { $pythonOk = $false }
        if (-not $pythonOk) { $missing += $RequiredPythonModules }
    }
    [pscustomobject]@{
        ProjectRoot   = $ProjectRoot
        AdbExe        = $AdbExe
        VenvPython    = $VenvPython
        SystemPython  = $SystemPython
        NodeExe       = $NodeExe
        OpenSslExe    = $OpenSslExe
        CaptureDir    = (Test-Path $CaptureDir)
        SessionFile   = (Test-Path $SessionFile)
        StateFile     = (Test-Path $StateFile)
        CompletionFile= (Test-Path $CompletionFile)
        ApkPath       = (Test-Path $ApkPath)
        PythonDepsOk  = $pythonOk
        MissingPython = ($missing -join ',')
    }
}

function Test-DeviceOnline {
    param([string]$Serial = $DefaultSerial)
    if (-not $AdbExe) { return $false }
    try {
        $out = & $AdbExe -s $Serial shell 'echo ok' 2>&1
        return (($out -join '') -match 'ok')
    } catch { return $false }
}
