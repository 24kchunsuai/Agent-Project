<#
.SYNOPSIS
    一键停止 drone-agent001 的四个服务（进程 + 任务实例，**不删除任务定义**）。

.DESCRIPTION
    按下单顺序的倒序停：9090 -> 8080 -> 8000 -> 8060。

    两步走，缺一不可：
      1. Stop-ScheduledTask —— 停任务（它只停 cmd 包装进程）
      2. 端口上残留的监听进程 —— Stop-Process（java / python 是 cmd 的子进程，
         父进程停了子进程可能还在，端口仍占着，下次启动就冲突）
    所以脚本停完一定会**复查端口**，没释放就再杀一次，最后如实报告。

    ⚠️ 8060 若是在 IDEA 里启动的（不是本脚本的计划任务），也会被一起杀掉——
       这正是"一键停止"应该做的。不想动它请加 -SkipPorts 8060。

.PARAMETER SkipPorts
    要跳过的端口，例如 -SkipPorts 8060,9090

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\scripts\stop-all.ps1
    powershell -ExecutionPolicy Bypass -File deploy\scripts\stop-all.ps1 -SkipPorts 8060
#>
[CmdletBinding()]
param(
    [int[]]$SkipPorts = @()
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_services.ps1')

Write-Host ''
Write-Host '=== drone-agent001  一键停止 ===' -ForegroundColor Cyan
Write-Host "  $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))"
Write-Host ''

$ordered = @($AgtServices | Sort-Object -Property Port -Descending)
$results = @()

foreach ($svc in $ordered) {
    if ($SkipPorts -contains $svc.Port) {
        Write-Host ("  [跳过] {0,-12} port {1}（-SkipPorts）" -f $svc.Name, $svc.Port) -ForegroundColor DarkGray
        $results += [pscustomobject]@{ Port = $svc.Port; Name = $svc.Name; State = 'kept' }
        continue
    }

    $taskState = '-'
    $task = Get-ScheduledTask -TaskName $svc.TaskName -ErrorAction SilentlyContinue
    if ($task) {
        $taskState = $task.State
        if ($taskState -eq 'Running') {
            Stop-ScheduledTask -TaskName $svc.TaskName -ErrorAction SilentlyContinue
        }
    }
    Start-Sleep -Seconds 2

    # 兜底：cmd 的子进程可能还活着，端口还占着
    $killed = @()
    for ($i = 0; $i -lt 3; $i++) {
        $pid8080 = Get-AgtListenerPid -Port $svc.Port
        if ($pid8080 -eq 0) { break }
        $name = try { (Get-Process -Id $pid8080 -ErrorAction Stop).ProcessName } catch { '?' }
        Write-Host ("  [杀进程] {0,-12} port {1}  pid={2} ({3})" -f $svc.Name, $svc.Port, $pid8080, $name) -ForegroundColor Yellow
        Stop-Process -Id $pid8080 -Force -ErrorAction SilentlyContinue
        $killed += $pid8080
        Start-Sleep -Seconds 2
    }

    $left = Get-AgtListenerPid -Port $svc.Port
    if ($left -eq 0) {
        $state = if ($killed.Count -gt 0) { "stopped(pid $($killed -join ','))" } else { 'stopped' }
        Write-Host ("  [已停]   {0,-12} port {1}  任务原状态={2}" -f $svc.Name, $svc.Port, $taskState) -ForegroundColor Green
    } else {
        $state = "STILL LISTENING pid=$left"
        Write-Host ("  [失败]   {0,-12} port {1}  仍被 pid={2} 占用" -f $svc.Name, $svc.Port, $left) -ForegroundColor Red
        Write-Host ("           手动处理： Get-Process -Id {0} | Select-Object Id,ProcessName,Path" -f $left) -ForegroundColor Red
    }
    $results += [pscustomobject]@{ Port = $svc.Port; Name = $svc.Name; State = $state }
}

Write-Host ''
Write-Host '  结果：'
foreach ($r in $results | Sort-Object Port) {
    Write-Host ("    {0,-6} {1,-14} {2}" -f $r.Port, $r.Name, $r.State)
}

Write-Host ''
Write-Host '  任务定义**没有删除**——下次直接 start-all.ps1 即可。' -ForegroundColor Cyan
Write-Host '  要彻底移除任务（不删代码/数据/日志）：install-tasks.ps1 -Uninstall' -ForegroundColor Cyan
Write-Host ''
