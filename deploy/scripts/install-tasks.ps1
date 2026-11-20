<#
.SYNOPSIS
    注册 drone-agent001 的四个 Windows 计划任务（幂等，可反复执行）。

.DESCRIPTION
    为什么用计划任务：本机的长驻服务不能用 nohup / Start-Process 起——命令一返回
    进程就被回收了。计划任务跑在 OS 层，不受调用生命周期影响。

    为什么必须是 S4U 非交互主体：默认的 Interactive 主体会给每个服务弹出一个可见的
    cmd 窗口；窗口被关掉（点 X / Ctrl+C）→ cmd 收到 CTRL_CLOSE → 整棵进程树一起死。
    2026-09-24 实测四服务同时以 0xC000013A 退出，就是这个原因。S4U 主体让进程跑在
    session 0，无窗口，用户注销也不受影响。

.PARAMETER StartAtLogon
    额外加一个"用户登录时启动"触发器。默认不加（手动 start-all.ps1 起）。

.PARAMETER Uninstall
    删除这四个计划任务（不删代码、不删数据、不删日志）。重新 install 即可恢复。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\scripts\install-tasks.ps1
    powershell -ExecutionPolicy Bypass -File deploy\scripts\install-tasks.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$StartAtLogon,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_services.ps1')

Write-Host ''
Write-Host '=== drone-agent001  计划任务注册 ===' -ForegroundColor Cyan
Write-Host "  项目根  $AgtRoot"
Write-Host "  日志    $AgtLogDir"
Write-Host ''

# ---------------------------------------------------------------- 卸载分支
if ($Uninstall) {
    foreach ($svc in $AgtServices) {
        $task = Get-ScheduledTask -TaskName $svc.TaskName -ErrorAction SilentlyContinue
        if ($task) {
            Unregister-ScheduledTask -TaskName $svc.TaskName -Confirm:$false
            Write-Host "  已删除任务  $($svc.TaskName)" -ForegroundColor Yellow
        } else {
            Write-Host "  跳过（不存在）  $($svc.TaskName)" -ForegroundColor DarkGray
        }
    }
    Write-Host ''
    Write-Host '  注意：只删了任务定义。**正在运行的进程不会被停止**，' -ForegroundColor Yellow
    Write-Host '        要停服务请用 stop-all.ps1（需在删除前执行）。' -ForegroundColor Yellow
    Write-Host ''
    exit 0
}

# ---------------------------------------------------------------- 前置检查
$bad = @()
foreach ($svc in $AgtServices) {
    if (-not $svc.Exe -or -not (Test-Path $svc.Exe)) {
        $bad += "  [解释器缺失] $($svc.Name) -> $($svc.Exe)"
    }
    if (-not (Test-Path $svc.WorkingDir)) {
        Write-Host "  [WARN] 工作目录不存在：$($svc.WorkingDir)" -ForegroundColor Yellow
        Write-Host "         （$($svc.Name) 启动会失败；$($svc.FixHint)）" -ForegroundColor DarkGray
    }
}
if ($bad.Count -gt 0) {
    Write-Host ''
    Write-Host '  [FAIL] 找不到可执行文件，无法注册：' -ForegroundColor Red
    $bad | ForEach-Object { Write-Host $_ -ForegroundColor Red }
    Write-Host '         设置环境变量 AGT_PYTHON / JAVA_HOME / JSDSTU3_ROOT 后重试。' -ForegroundColor Red
    Write-Host ''
    exit 2
}

if (-not (Test-Path $AgtLogDir)) {
    New-Item -ItemType Directory -Path $AgtLogDir -Force | Out-Null
}

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
                                        -LogonType S4U -RunLevel Limited
Write-Host "  运行主体  $($principal.UserId)   LogonType=$($principal.LogonType)（session 0，无窗口）" -ForegroundColor Green
Write-Host ''

# ---------------------------------------------------------------- 注册（幂等）
foreach ($svc in $AgtServices) {
    $action = New-ScheduledTaskAction -Execute 'cmd.exe' `
                                      -Argument (Get-AgtTaskArguments -Service $svc) `
                                      -WorkingDirectory $svc.WorkingDir

    # ExecutionTimeLimit=0 → 不限时（默认 72 小时到点会被强杀）
    # MultipleInstances=IgnoreNew → 已在跑就不再起第二个（防端口冲突）
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
                                             -DontStopIfGoingOnBatteries `
                                             -StartWhenAvailable `
                                             -MultipleInstances IgnoreNew `
                                             -ExecutionTimeLimit ([TimeSpan]::Zero)

    $description = "drone-agent001 $($svc.Name) ($($svc.Role)) - port $($svc.Port). " +
                   'Registered by deploy\scripts\install-tasks.ps1. Log: ' + $svc.Log

    if ($StartAtLogon) {
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
        Register-ScheduledTask -TaskName $svc.TaskName -Action $action -Principal $principal `
                               -Settings $settings -Trigger $trigger -Description $description -Force | Out-Null
    } else {
        Register-ScheduledTask -TaskName $svc.TaskName -Action $action -Principal $principal `
                               -Settings $settings -Description $description -Force | Out-Null
    }
    Write-Host ("  [OK] {0,-24} port {1}  {2}" -f $svc.TaskName, $svc.Port, $svc.Role) -ForegroundColor Green
}

# ---------------------------------------------------------------- 回读校验
Write-Host ''
Write-Host '  回读校验：' -ForegroundColor Cyan
$running = 0
foreach ($svc in $AgtServices) {
    $t = Get-ScheduledTask -TaskName $svc.TaskName -ErrorAction SilentlyContinue
    $pid8080 = Get-AgtListenerPid -Port $svc.Port
    $sess = if ($pid8080 -ne 0) { Get-AgtSessionId -ProcessId $pid8080 } else { '-' }
    if ($pid8080 -ne 0) { $running++ }
    Write-Host ("    {0,-24} 任务={1,-8} LogonType={2,-6} 端口{3} 监听pid={4} session={5}" -f `
        $svc.TaskName, $t.State, $t.Principal.LogonType, $svc.Port, $pid8080, $sess)
}

if ($running -gt 0) {
    Write-Host ''
    Write-Host "  [注意] 检测到 $running 个端口已在监听。" -ForegroundColor Yellow
    Write-Host '         任务定义已更新，但**当前进程仍是用旧定义启动的**。' -ForegroundColor Yellow
    Write-Host '         要让新定义生效：先 stop-all.ps1，再 start-all.ps1。' -ForegroundColor Yellow
}

Write-Host ''
Write-Host '  下一步：  powershell -ExecutionPolicy Bypass -File deploy\scripts\start-all.ps1' -ForegroundColor Cyan
Write-Host '  查状态：  powershell -ExecutionPolicy Bypass -File deploy\scripts\status.ps1' -ForegroundColor Cyan
Write-Host ''
