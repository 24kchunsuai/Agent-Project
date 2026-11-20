<#
.SYNOPSIS
    一键启动 drone-agent001 的四个服务（先自检，再按依赖顺序启动，最后等就绪）。

.DESCRIPTION
    顺序 8060 -> 8000 -> 8080 -> 9090：8080（业务后端）既代理 8060 的登录，
    又转发 8000 的 agent，所以它必须最后起、且依赖前两者已经就绪。

    行为
      * 启动前先跑 deploy\scripts\preflight.py。自检不通过就**拒绝启动**（fail-fast），
        除非显式加 -SkipPreflight（不建议：那等于带着残缺配置跑）。
      * 已在监听的端口视为"已在运行"，跳过（幂等，可反复执行）。
      * 每启动一个就等它真正就绪（探针通过）再起下一个；超时则打印日志尾部并给出排查线索。
      * 只启动任务，**不注册任务**；任务不存在时提示先跑 install-tasks.ps1。

.PARAMETER SkipPreflight
    跳过部署前自检（默认不跳过）。

.PARAMETER Restart
    先把已在运行的服务停掉再启动（改了代码想让它生效时用）。

.PARAMETER TimeoutScale
    就绪等待时间的倍率，慢机器上加大（默认 1）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File deploy\scripts\start-all.ps1
    powershell -ExecutionPolicy Bypass -File deploy\scripts\start-all.ps1 -Restart
#>
[CmdletBinding()]
param(
    [switch]$SkipPreflight,
    [switch]$Restart,
    [double]$TimeoutScale = 1.0
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '_services.ps1')

function Wait-AgtReady {
    param([Parameter(Mandatory)][object]$Service, [int]$TimeoutSec)
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($Service.ProbeUrl) {
            $probe = Invoke-AgtProbe -Url $Service.ProbeUrl -Needle $Service.ProbeNeedle -TimeoutSec 4
            if ($probe.Ok) { return $probe.Info }
        } else {
            if ((Get-AgtListenerPid -Port $Service.Port) -ne 0) { return '端口已监听' }
        }
        Start-Sleep -Seconds 2
    }
    return $null
}

function Show-AgtLogTail {
    param([Parameter(Mandatory)][object]$Service, [int]$Lines = 20)
    if (Test-Path $Service.Log) {
        Write-Host "      ---- 日志尾部 $($Service.Log) ----" -ForegroundColor DarkGray
        Get-Content -Path $Service.Log -Tail $Lines -ErrorAction SilentlyContinue |
            ForEach-Object { Write-Host "      | $_" -ForegroundColor DarkGray }
    } else {
        Write-Host "      （日志还没生成：$($Service.Log)）" -ForegroundColor DarkGray
    }
}

Write-Host ''
Write-Host '=== drone-agent001  一键启动 ===' -ForegroundColor Cyan
Write-Host "  $((Get-Date).ToString('yyyy-MM-dd HH:mm:ss'))   项目根 $AgtRoot"
Write-Host ''

# ---------------------------------------------------------------- [1/4] 自检
if (-not $SkipPreflight) {
    Write-Host '[1/4] 部署前自检（preflight.py）' -ForegroundColor Cyan
    $py = if ($AgtPython -and (Test-Path $AgtPython)) { $AgtPython } else { 'python' }
    $preflight = Join-Path $PSScriptRoot 'preflight.py'
    if (-not (Test-Path $preflight)) {
        Write-Host "  [FAIL] 找不到 $preflight" -ForegroundColor Red
        exit 2
    }
    & $py $preflight
    if ($LASTEXITCODE -ne 0) {
        Write-Host ''
        Write-Host '  >>> 自检未通过，**拒绝启动**（fail-fast）。' -ForegroundColor Red
        Write-Host '      按上面标 FAIL 的项修好再跑。确要强行启动（不推荐）：' -ForegroundColor Red
        Write-Host '      start-all.ps1 -SkipPreflight' -ForegroundColor Red
        Write-Host ''
        exit 1
    }
    Write-Host ''
} else {
    Write-Host '[1/4] 已跳过部署前自检（-SkipPreflight）' -ForegroundColor Yellow
    Write-Host ''
}

# ------------------------------------------------------------ [2/4] 任务检查
Write-Host '[2/4] 检查计划任务是否已注册' -ForegroundColor Cyan
$missing = @()
foreach ($svc in $AgtServices) {
    if (-not (Get-ScheduledTask -TaskName $svc.TaskName -ErrorAction SilentlyContinue)) {
        $missing += $svc.TaskName
    }
}
if ($missing.Count -gt 0) {
    Write-Host '  [FAIL] 以下任务未注册：' -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "         $_" -ForegroundColor Red }
    Write-Host '  先注册：powershell -ExecutionPolicy Bypass -File deploy\scripts\install-tasks.ps1' -ForegroundColor Red
    Write-Host ''
    exit 1
}
Write-Host "  OK：四个任务都在（$($AgtServices[0].TaskName) … $($AgtServices[-1].TaskName)）"
Write-Host ''

# ---------------------------------------------------------------- [3/4] 启动
Write-Host '[3/4] 按依赖顺序启动（8060 -> 8000 -> 8080 -> 9090）' -ForegroundColor Cyan
# 启动前收口上一轮的控制台捕获日志（E-6）：此刻四个服务都没起，文件不被占用，
# 轮转/删除才真正生效（运行中会被文件句柄挡住，见 trim-logs.ps1 的说明）。
# 用独立进程调用是为了隔离它内部的 exit；失败也只告警，绝不阻塞启动。
$trimScript = Join-Path $PSScriptRoot 'trim-logs.ps1'
if (Test-Path $trimScript) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $trimScript -MaxMB 20 -KeepDays 14
    if ($LASTEXITCODE -ne 0) {
        Write-Host '  [warn] 日志收口未正常结束，继续启动' -ForegroundColor DarkYellow
    }
    Write-Host ''
}
$blocked = $false

foreach ($svc in $AgtServices) {
    $pid8080 = Get-AgtListenerPid -Port $svc.Port

    if ($pid8080 -ne 0 -and -not $Restart) {
        Write-Host ("  [跳过] {0,-12} port {1}  已在运行 (pid={2})" -f $svc.Name, $svc.Port, $pid8080) -ForegroundColor DarkGray
        continue
    }
    if ($pid8080 -ne 0 -and $Restart) {
        Write-Host ("  [重启] {0,-12} port {1}  停旧的 (pid={2})" -f $svc.Name, $svc.Port, $pid8080) -ForegroundColor Yellow
        Stop-ScheduledTask -TaskName $svc.TaskName -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 2
        $pid8080 = Get-AgtListenerPid -Port $svc.Port
        if ($pid8080 -ne 0) { Stop-Process -Id $pid8080 -Force -ErrorAction SilentlyContinue; Start-Sleep -Seconds 2 }
    }

    $timeout = [int]($svc.ReadyTimeout * $TimeoutScale)
    Write-Host ("  [启动] {0,-12} port {1}  {2}" -f $svc.Name, $svc.Port, $svc.Role) -ForegroundColor White

    $t0 = Get-Date
    Start-ScheduledTask -TaskName $svc.TaskName
    $info = Wait-AgtReady -Service $svc -TimeoutSec $timeout
    $secs = [math]::Round(((Get-Date) - $t0).TotalSeconds, 1)

    if ($info) {
        Write-Host ("         -> 就绪  {0}  ({1}s, {2})" -f $svc.Name, $secs, $info) -ForegroundColor Green
        continue
    }

    if ($svc.Optional) {
        Write-Host ("         -> 未就绪（超时 {0}s）—— 但它是外部系统，不阻塞其它服务" -f $timeout) -ForegroundColor Yellow
        Write-Host "            排查：$($svc.FixHint)" -ForegroundColor Yellow
        Show-AgtLogTail -Service $svc -Lines 10
        continue
    }

    Write-Host ("         -> 超时未就绪（{0}s）" -f $timeout) -ForegroundColor Red
    Write-Host "            排查方向：$($svc.FixHint)" -ForegroundColor Red
    Show-AgtLogTail -Service $svc -Lines 25
    $blocked = $true
    break
}

# ---------------------------------------------------------------- [4/4] 汇总
Write-Host ''
Write-Host '[4/4] 状态汇总' -ForegroundColor Cyan
& (Join-Path $PSScriptRoot 'status.ps1')

if ($blocked) {
    Write-Host '  >>> 有服务未能就绪，已中止后续启动。修好后重跑本脚本（幂等，已起的会跳过）。' -ForegroundColor Red
    exit 1
}

$notUp = @($AgtServices | Where-Object { (Get-AgtListenerPid -Port $_.Port) -eq 0 })
Write-Host ''
if ($notUp.Count -eq 0) {
    Write-Host '  >>> 启动流程结束：四服务全部就绪。' -ForegroundColor Green
} else {
    $names = ($notUp | ForEach-Object { "$($_.Name)($($_.Port))" }) -join ', '
    Write-Host "  >>> 启动流程结束：仍未就绪 -> $names" -ForegroundColor Yellow
    Write-Host "      排查线索见 deploy\logs\ 下对应日志；故障对照表：docs\部署手册.md §7" -ForegroundColor Cyan
}
Write-Host ''
exit 0
