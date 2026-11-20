<#
.SYNOPSIS
    查看 drone-agent001 四个服务的状态（端口 / 会话 / 计划任务 / 探针 / 日志时间）。

.DESCRIPTION
    只读，不改动任何东西。判断"服务是不是真的健康"看三样，缺一不可：

      1. 端口是否 LISTENING —— 只看这个会被骗：进程活着但内部依赖挂了也监听
      2. 探针是否通过 —— 8000 看 /v1/health 的 code=2000，8080 看 /v1/version
      3. **SessionId 是否为 0** —— 0 = S4U 非交互（无窗口，不会被误关）；
         1 = 当前交互桌面会话（弹了可见 cmd 窗口，窗口被关整棵进程树就死）。
         历史上"四服务跑得好好的突然全没"就是这个原因，必须核这一列。

.PARAMETER LogTail
    额外打印每个服务日志的最后 N 行（默认 0 = 不打）。

.PARAMETER Json
    输出 JSON（给监控 / CI 用）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\scripts\status.ps1
    powershell -ExecutionPolicy Bypass -File deploy\scripts\status.ps1 -LogTail 10
#>
[CmdletBinding()]
param(
    [int]$LogTail = 0,
    [switch]$Json
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_services.ps1')

$rows = @()
$problems = @()

foreach ($svc in $AgtServices) {
    $pid8080 = Get-AgtListenerPid -Port $svc.Port
    $sess = if ($pid8080 -ne 0) { Get-AgtSessionId -ProcessId $pid8080 } else { $null }
    $task = Get-ScheduledTask -TaskName $svc.TaskName -ErrorAction SilentlyContinue
    $taskState = if ($task) { [string]$task.State } else { 'MISSING' }

    $probeText = '-'
    $probeOk = $null
    if ($svc.ProbeUrl -and $pid8080 -ne 0) {
        $p = Invoke-AgtProbe -Url $svc.ProbeUrl -Needle $svc.ProbeNeedle -TimeoutSec 5
        $probeText = $p.Info
        $probeOk = $p.Ok
    } elseif ($pid8080 -ne 0) {
        $probeText = 'TCP ok'
        $probeOk = $true
    }

    $logTime = if (Test-Path $svc.Log) { (Get-Item $svc.Log).LastWriteTime.ToString('MM-dd HH:mm:ss') } else { '-' }

    $state = 'UP'
    if ($pid8080 -eq 0) {
        $state = if ($svc.Optional) { 'DOWN(optional)' } else { 'DOWN' }
    } elseif ($probeOk -eq $false) {
        $state = 'UNHEALTHY'
    }

    # --- 结论与告警 ---
    if ($pid8080 -eq 0) {
        if (-not $svc.Optional) {
            $problems += "$($svc.Name) ($($svc.Port)) 未运行 —— $($svc.FixHint)"
        } else {
            $problems += "[低] $($svc.Name) ($($svc.Port)) 未运行（外部系统，可后起）"
        }
    } else {
        if ($sess -ne 0) {
            $problems += ("$($svc.Name) ($($svc.Port)) 跑在交互会话 SessionId=$sess" +
                          " —— 有可见窗口，窗口被关会连带进程树一起死；请用 install-tasks.ps1 以 S4U 重建后重启")
        }
        if ($probeOk -eq $false) {
            $problems += "$($svc.Name) ($($svc.Port)) 端口在监听但探针失败（$probeText）"
        }
        if ($taskState -ne 'Running') {
            $problems += "[低] $($svc.Name) ($($svc.Port)) 进程在跑但计划任务状态=$taskState（可能由 IDEA / 手工启动）"
        }
    }

    $rows += [pscustomobject]@{
        Port      = $svc.Port
        Name      = $svc.Name
        Role      = $svc.Role
        State     = $state
        Pid       = $pid8080
        SessionId = $sess
        Task      = $taskState
        Probe     = $probeText
        LogTime   = $logTime
    }
}

if ($Json) {
    [pscustomobject]@{
        checkedAt = (Get-Date).ToString('s')
        root      = $AgtRoot
        services  = $rows
        problems  = $problems
    } | ConvertTo-Json -Depth 5
    return
}

Write-Host ''
Write-Host '=== drone-agent001  服务状态 ===' -ForegroundColor Cyan
Write-Host ("  {0}" -f (Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))
Write-Host ''
Write-Host ("  {0,-6} {1,-12} {2,-14} {3,-6} {4,-10} {5,-8} {6}" -f '端口', '服务', '状态', 'pid', 'SessionId', '任务', '探针')
Write-Host ('  ' + ('-' * 76))

foreach ($r in $rows | Sort-Object Port) {
    $color = switch ($r.State) {
        'UP'             { 'Green' }
        'UNHEALTHY'      { 'Red' }
        'DOWN'           { 'Red' }
        default          { 'DarkGray' }
    }
    $sessText = if ($null -eq $r.SessionId) { '-' } else { $r.SessionId }
    Write-Host ("  {0,-6} {1,-12} {2,-14} {3,-6} {4,-10} {5,-8} {6}" -f `
        $r.Port, $r.Name, $r.State, $r.Pid, $sessText, $r.Task, $r.Probe) -ForegroundColor $color
    Write-Host ("         {0}   日志最后写入 {1}" -f $r.Role, $r.LogTime) -ForegroundColor DarkGray
}

if ($LogTail -gt 0) {
    foreach ($svc in $AgtServices) {
        if (Test-Path $svc.Log) {
            Write-Host ''
            Write-Host ("  ---- $($svc.Name) 日志尾部 $LogTail 行 ----") -ForegroundColor DarkGray
            Get-Content -Path $svc.Log -Tail $LogTail -ErrorAction SilentlyContinue |
                ForEach-Object { Write-Host "  | $_" -ForegroundColor DarkGray }
        }
    }
}

Write-Host ''
$up = @($rows | Where-Object { $_.State -eq 'UP' }).Count
if ($problems.Count -eq 0) {
    Write-Host "  >>> 结论：四服务全部就绪（$up/4），且都跑在 session 0（无窗口，不会被误关）。" -ForegroundColor Green
} else {
    Write-Host "  >>> 结论：$up/4 就绪。有 $($problems.Count) 条待处理：" -ForegroundColor Yellow
    foreach ($p in $problems) { Write-Host "      * $p" -ForegroundColor Yellow }
    Write-Host "      故障对照表：$AgtRoot\docs\部署手册.md §7" -ForegroundColor Cyan
}
Write-Host ''
