# 报名时段判定的单元测试（不发送任何通知）。
# ASCII only (PS 5.1 parses scripts as ANSI).
#
# 背景：曾因跨零点判断写错，把凌晨 00:32 判成"在 16:00~23:59 窗口内"，
# 导致推送了一条假的"监控可能已中断"。这里把边界固定下来回归测试。
#
# 用法:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\poller\test_window.ps1

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

# 只抽取函数定义，避免执行脚本主体的看护循环
$src = Get-Content (Join-Path $PSScriptRoot 'watchdog.ps1') -Raw -Encoding UTF8
$m = [regex]::Match($src, '(?s)function Test-InActiveWindow.*?\n\}')
if (-not $m.Success) { Write-Host 'FAIL: 未找到 Test-InActiveWindow 函数'; exit 1 }
Invoke-Expression $m.Value

# 两个场景：正常窗口 16:00~23:59:59；跨零点窗口 23:00~02:00
$cfgNormal = [pscustomobject]@{ active_start = '16:00'; active_end = '23:59:59' }
$cfgWrap   = [pscustomobject]@{ active_start = '23:00'; active_end = '02:00' }

$cases = @(
    @{ name = '凌晨 00:01（应不在窗口）';   cfg = $cfgNormal; t = '2026-10-10 00:01:00'; want = $false },
    @{ name = '凌晨 00:32（误报过）';       cfg = $cfgNormal; t = '2026-10-10 00:32:19'; want = $false },
    @{ name = '上午 10:00（应不在窗口）';   cfg = $cfgNormal; t = '2026-10-10 10:00:00'; want = $false },
    @{ name = '15:59（窗口前一刻）';        cfg = $cfgNormal; t = '2026-10-10 15:59:00'; want = $false },
    @{ name = '16:00（窗口起点）';          cfg = $cfgNormal; t = '2026-10-10 16:00:00'; want = $true  },
    @{ name = '18:30（窗口内）';            cfg = $cfgNormal; t = '2026-10-10 18:30:00'; want = $true  },
    @{ name = '23:59（窗口末刻）';          cfg = $cfgNormal; t = '2026-10-10 23:59:00'; want = $true  },
    @{ name = '跨零点 22:59（未开始）';     cfg = $cfgWrap;   t = '2026-10-10 22:59:00'; want = $false },
    @{ name = '跨零点 23:00（起点）';       cfg = $cfgWrap;   t = '2026-10-10 23:00:00'; want = $true  },
    @{ name = '跨零点 00:30（窗口内）';     cfg = $cfgWrap;   t = '2026-10-10 00:30:00'; want = $true  },
    @{ name = '跨零点 02:00（末刻）';       cfg = $cfgWrap;   t = '2026-10-10 02:00:00'; want = $true  },
    @{ name = '跨零点 02:01（已过）';       cfg = $cfgWrap;   t = '2026-10-10 02:01:00'; want = $false },
    @{ name = '跨零点 12:00（不在）';       cfg = $cfgWrap;   t = '2026-10-10 12:00:00'; want = $false }
)

$fail = 0
foreach ($c in $cases) {
    $got = Test-InActiveWindow $c.cfg ([datetime]::ParseExact($c.t, 'yyyy-MM-dd HH:mm:ss', $null))
    $ok = ($got -eq $c.want)
    if (-not $ok) { $fail++ }
    Write-Host ("{0} {1,-28} 期望={2,-5} 实际={3}" -f $(if ($ok) { '[OK]  ' } else { '[FAIL]' }), $c.name, $c.want, $got)
}

Write-Host ''
if ($fail -eq 0) { Write-Host '全部通过' } else { Write-Host ("$fail 项失败") }
exit $fail
