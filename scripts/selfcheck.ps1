# 项目自检：路径、脚本语法、运行环境、设备状态、产物完整性。
#
# 用法：
#   pwsh -NoProfile -File scripts\selfcheck.ps1
#
# 设计说明：
#   1. 只读检查，不修改任何东西；
#   2. 语法检查委托给 scripts\recon\check_ps1.py / check_scripts.py，
#      它们比"看字符是否 ASCII"可靠得多——ASCII 检查曾经误报：
#      PS 5.1 其实能正常加载带中文注释的 UTF-8 脚本，而当时却把它标成风险；
#   3. 已废弃组件（HTTP 代理方案）不再作为必需项，缺失只提示不失败。

param()

$ErrorActionPreference = 'Continue'
. (Join-Path $PSScriptRoot 'config.ps1')

function Section($t) { Write-Host ''; Write-Host ('=== ' + $t + ' ===') }
$fail = 0

# ---------------------------------------------------------------- 路径与工具
Section 'paths and tools'
$p = Test-ProjectPaths
$p | Format-List | Out-String | Write-Host

# 这些是运行监控所必需的
foreach ($name in 'VenvPython') {
    if (-not $p.$name) { Write-Host ('MISSING: ' + $name); $fail++ }
}
if (-not $p.PythonDepsOk) { Write-Host ('MISSING python deps: ' + $p.MissingPython); $fail++ }

# 以下为可选：仅抓包/证书相关用到
foreach ($opt in @(
    @{ n = 'NodeExe'; why = '打包 Frida payload' },
    @{ n = 'OpenSslExe'; why = '证书哈希（签名已还原，通常用不到）' },
    @{ n = 'SystemPython'; why = '创建 venv / 运行 recon 脚本' }
)) {
    if (-not $p.($opt.n)) { Write-Host ('OPTIONAL missing: ' + $opt.n + '  (' + $opt.why + ')') }
}

# ---------------------------------------------------------------- 脚本语法
Section 'script syntax (all engines)'
$checkPs1 = Join-Path $PSScriptRoot 'recon\check_ps1.py'
$checkPy = Join-Path $PSScriptRoot 'recon\check_scripts.py'
foreach ($c in @(@{ f = $checkPs1; py = $SystemPython; label = 'PowerShell (PS7 + PS5.1)' },
                 @{ f = $checkPy; py = $VenvPython; label = 'Python (compile + import + CLI)' })) {
    if (-not (Test-Path $c.f)) { Write-Host ('MISSING checker: ' + $c.f); $fail++; continue }
    # check_scripts.py 需要 import requests / frida，必须用 venv 的解释器；
    # 用系统 python 会因缺 frida 而误报失败。
    if (-not $c.py) { Write-Host ('MISSING interpreter for ' + $c.f); $fail++; continue }
    Write-Host ('--- ' + $c.label + ' ---')
    $out = & $c.py $c.f 2>&1
    $tail = @($out) | Select-Object -Last 6
    foreach ($l in $tail) { Write-Host ('  ' + $l) }
    if (-not ($out -match 'ALL CHECKS PASSED')) { Write-Host ('  CHECKER FAILED: ' + $c.f); $fail++ }
}

# ---------------------------------------------------------------- 运行时
Section 'runtime'
$v = & $VenvPython -V 2>&1
Write-Host ('venv python : ' + ($v -join ' '))
$mods = & $VenvPython -c "import requests, frida; print('requests+frida ok')" 2>&1
Write-Host ('venv modules: ' + ($mods -join ' '))

# ---------------------------------------------------------------- 侦察产物
Section 'recon assets'
$reconAssets = @(
    (Join-Path $ReconDir 'index.android.bundle'),
    (Join-Path $ReconDir 'strings.txt'),
    (Join-Path $ReconDir 'analysis.txt'),
    (Join-Path $ReconDir 'cjk_all.txt')
)
foreach ($f in $reconAssets) {
    if (Test-Path $f) {
        $kb = [math]::Round((Get-Item $f).Length / 1KB, 1)
        Write-Host ('OK      ' + $f.Replace($ProjectRoot + '\', '') + '  (' + $kb + ' KB)')
    } else {
        Write-Host ('MISSING ' + $f)
    }
}

# ---------------------------------------------------------------- 运行状态
Section 'running state'
Write-Host 'Legacy poller retired. Use deploy/verify.sh for agent state and the agent watchdog for scheduler progress.'

# ---------------------------------------------------------------- 设备状态
Section 'emulator state (read-only)'
if ($AdbExe) {
    foreach ($d in @(@{ n = 'sve '; s = $SveSerial; must = $false },
                     @{ n = 'sve2'; s = $Sve2Serial; must = $false },
                     @{ n = 'pjsk'; s = $PjskSerial; must = $false })) {
        $online = Test-DeviceOnline -Serial $d.s
        if (-not $online) {
            $note = if ($d.must) { '   <-- REQUIRED for monitoring' } else { '   (optional / not ours)' }
            Write-Host ($d.n + '  : offline' + $note)
            if ($d.must) { $fail++ }
            continue
        }
        $model = (& $AdbExe -s $d.s shell 'getprop ro.product.model' 2>&1) -join ''
        $proxy = (& $AdbExe -s $d.s shell 'settings get global http_proxy' 2>&1) -join ''
        $proxy = $proxy.Trim()
        if (-not $proxy -or $proxy -eq 'null') { $proxy = ':0' }
        Write-Host ($d.n + '  : online  model=' + $model.Trim() + '  proxy=' + $proxy)
        if ($proxy -ne ':0') {
            Write-Host ('        WARNING: proxy is not :0 on ' + $d.n + ' -- the app should stay direct')
            if ($d.n -eq 'sve ') { $fail++ }
        }
    }
} else {
    Write-Host 'adb not found; skipped'
}

# ---------------------------------------------------------------- 结果
Section 'result'
if ($fail -eq 0) { Write-Host 'ALL CHECKS PASSED' } else { Write-Host ('FAILURES: ' + $fail) }
exit $fail
