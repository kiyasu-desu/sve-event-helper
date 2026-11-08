# 名额轮询器看护进程：定期检查轮询器是否还在采样。
# ASCII only (PS 5.1 parses scripts as ANSI).
#
# 为什么需要：轮询器一旦静默死掉，你会以为"一直没空位" —— 这是最危险的失败模式。
# 本脚本定期读取 capture/state.json 的 last_ok_at_local，
# 超过 StaleMinutes 分钟没有成功采样就发 PushPlus + 邮件告警。
#
# 用法：
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\poller\watchdog.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\poller\watchdog.ps1 -CheckSeconds 600 -StaleMinutes 10
param(
    [int]$CheckSeconds = 600,
    [int]$StaleMinutes = 10,
    [switch]$Once
)

$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$state = Join-Path $root 'capture\state.json'
$settings = Join-Path $root 'config\settings.json'
$logfile = Join-Path $root 'capture\watchdog.log'

function Say($m) {
    $line = '[' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '] ' + $m
    Write-Host $line
    Add-Content -Path $logfile -Value $line -Encoding UTF8
}

function Get-Config {
    if (-not (Test-Path $settings)) { return $null }
    try { return (Get-Content $settings -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { return $null }
}

function Send-PushPlus($cfg, $title, $content) {
    if (-not $cfg.channels.pushplus.enabled) { return }
    $token = $cfg.channels.pushplus.token
    if (-not $token) { Say 'pushplus token 未配置，跳过'; return }
    $body = @{ token = $token; title = $title; content = $content; template = 'html' } | ConvertTo-Json -Compress
    try {
        $resp = Invoke-RestMethod -Uri 'https://www.pushplus.plus/send' -Method Post `
                -ContentType 'application/json; charset=utf-8' `
                -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -TimeoutSec 20
        if ($resp.code -eq 200) { Say '  pushplus 告警已发送' } else { Say ('  pushplus 失败: ' + $resp.msg) }
    } catch { Say ('  pushplus 异常: ' + $_) }
}

function Send-Mail($cfg, $subject, $content) {
    $em = $cfg.channels.email
    if (-not $em.enabled) { return }
    if (-not ($em.smtp_host -and $em.username -and $em.authcode)) { Say 'email 配置不完整，跳过'; return }
    try {
        $msg = New-Object System.Net.Mail.MailMessage
        $msg.From = New-Object System.Net.Mail.MailAddress($em.from_addr)
        foreach ($to in $em.to_addrs) { $msg.To.Add($to) }
        $msg.Subject = $subject
        $msg.SubjectEncoding = [System.Text.Encoding]::UTF8
        $msg.Body = $content
        $msg.BodyEncoding = [System.Text.Encoding]::UTF8
        $msg.IsBodyHtml = $true
        $client = New-Object System.Net.Mail.SmtpClient($em.smtp_host, [int]$em.smtp_port)
        $client.EnableSsl = [bool]$em.use_ssl
        $client.Credentials = New-Object System.Net.NetworkCredential($em.username, $em.authcode)
        $client.Send($msg)
        $msg.Dispose()
        Say '  email 告警已发送'
    } catch { Say ('  email 异常: ' + $_) }
}

function Send-Alert($headline, $detail) {
    $cfg = Get-Config
    if (-not $cfg) { Say '配置读取失败，无法发送告警'; return }
    $html = '<h3>' + $headline + '</h3>' +
            '<p><b>赛事：</b>' + $cfg.tournament_name + '（id=' + $cfg.tournament_id + '）</p>' +
            '<p><b>时间：</b>' + (Get-Date -Format 'yyyy-MM-dd HH:mm:ss') + '</p>' +
            '<p><b>详情：</b>' + $detail + '</p>'
    $title = '【SVE名额监控】' + $headline
    Send-PushPlus $cfg $title $html
    Send-Mail $cfg $title $html
}

function Test-InActiveWindow($cfg, $now) {
    # 判断 $now 是否落在报名时段内。所有比较都基于完整 DateTime，
    # 避免"当天时刻"与跨零点窗口混淆（曾因此误报）。
    if (-not ($cfg -and $cfg.active_start -and $cfg.active_end)) { return $true }
    $st = $null; $en = $null
    foreach ($fmt in @('HH:mm:ss', 'HH:mm')) {
        if (-not $st) { try { $st = [datetime]::ParseExact($cfg.active_start, $fmt, $null) } catch { } }
        if (-not $en) { try { $en = [datetime]::ParseExact($cfg.active_end, $fmt, $null) } catch { } }
    }
    if (-not $st -or -not $en) { return $true }

    if ($st -le $en) {
        $t = $now.Date.AddHours($st.Hour).AddMinutes($st.Minute).AddSeconds($st.Second)
        $u = $now.Date.AddHours($en.Hour).AddMinutes($en.Minute).AddSeconds($en.Second)
        return ($now -ge $t -and $now -le $u)
    }

    # 跨零点窗口（如 23:00~02:00）可能命中两个区间：
    #   今天 23:00 ~ 明天 02:00，以及 昨天 23:00 ~ 今天 02:00。
    # 只判断前者会漏掉凌晨那一段（单元测试抓出过此问题）。
    foreach ($base in @($now.Date, $now.Date.AddDays(-1))) {
        $t = $base.AddHours($st.Hour).AddMinutes($st.Minute).AddSeconds($st.Second)
        $u = $base.AddHours($en.Hour).AddMinutes($en.Minute).AddSeconds($en.Second).AddDays(1)
        if ($now -ge $t -and $now -le $u) { return $true }
    }
    return $false
}

function Run-Check {
    # 任务已完成（报名稳定确认）时轮询器会正常退出，此时【不应】告警，
    # 否则会和"任务完成"的通知同时到达，破坏告警可信度。
    $completion = Join-Path $root 'capture\completed.json'
    if (Test-Path $completion) {
        try {
            $c = Get-Content $completion -Raw -Encoding UTF8 | ConvertFrom-Json
            $pollerRunning = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
                Where-Object { $_.CommandLine -like '*poll.py*' }).Count -gt 0
            Say ('任务已完成（' + $c.finished_at + '，赛事 ' + $c.tournament_name +
                 '，二次确认=' + $c.second_confirm_status + '）。轮询器运行中=' + $pollerRunning + ' -> 不告警')
            return $false
        } catch {
            Say ('完成标记读取失败: ' + $_)
        }
    }

    if (-not (Test-Path $state)) { Say ('state.json 不存在: ' + $state); return $false }
    try {
        $s = Get-Content $state -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch { Say ('读取 state.json 失败: ' + $_); return $false }

    $stamp = $s.last_ok_at_local
    if (-not $stamp) { Say 'state.json 缺少 last_ok_at_local（轮询器可能是旧版本）'; return $false }

    try { $last = [datetime]::ParseExact($stamp, 'yyyy-MM-dd HH:mm:ss', $null) }
    catch { Say ('时间解析失败: ' + $stamp); return $false }

    $ageMin = [math]::Round(((Get-Date) - $last).TotalMinutes, 1)
    $pollerAlive = @(Get-Process -Name python -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -like '*venv314*' }).Count -gt 0

    # 关键：轮询器在非报名时段会按设计休眠（state 不再更新），这不是故障。
    # 若不判断时段，每天从 24:00 到次日 16:00 会一直误报"监控已中断"。
    $cfg = Get-Config
    $inWindow = Test-InActiveWindow $cfg (Get-Date)

    if (-not $inWindow) {
        if (-not $pollerAlive) {
            Say "非报名时段且轮询器进程不在（可能被关闭），如需持续值守请重启轮询器"
            Send-Alert '轮询器已停止（非报名时段）' ("当前不在报名时段（$($cfg.active_start)~$($cfg.active_end)），" +
                "但轮询器进程不存在。若这是你有意关闭的可忽略；否则请在 16:00 前启动，以免错过名额。")
            return $true
        }
        Say ("非报名时段，轮询器静默属正常（最近采样 $ageMin 分钟前，进程存活=True）")
        return $false
    }

    if ($ageMin -gt $StaleMinutes) {
        Say ("!! 已 $ageMin 分钟没有成功采样（阈值 $StaleMinutes），进程存活=$pollerAlive")
        Send-Alert '监控可能已中断' ("已 $ageMin 分钟没有成功采样，监控进程存活=$pollerAlive。请检查轮询器、网络与会话是否失效。")
        return $true
    }

    Say ("正常：最近一次成功采样 $ageMin 分钟前，player_num=" + $s.player_num + '/' + $s.max_player_number +
         " available=" + $s.available + " session_ok=" + $s.session_ok)
    return $false
}

Say '=== 看护进程启动 ==='
if ($Once) { Run-Check | Out-Null; exit 0 }

$alerted = $false
while ($true) {
    $stale = Run-Check
    if ($stale -and -not $alerted) { $alerted = $true }
    elseif (-not $stale -and $alerted) { Say '采样已恢复'; $alerted = $false }
    Start-Sleep -Seconds $CheckSeconds
}
