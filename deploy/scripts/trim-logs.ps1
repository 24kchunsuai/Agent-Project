# =====================================================================
#  deploy\logs 下「控制台捕获日志」的收口（企业级落地规约 E-6）
#
#  为什么需要它
#  ------------
#  deploy\logs\*.log 不是应用写的，而是计划任务命令行
#  （deploy\scripts\_services.ps1 的 Get-AgtTaskArguments）里的
#      cmd /c ""<exe>" <args> > "<log>" 2>&1"
#  重定向产生的。cmd 的重定向没有任何轮转能力 —— 不加干预它会一直长下去。
#
#  应用自己的结构化日志（*.jsonl）由应用轮转（Python RotatingFileHandler /
#  Java logback RollingFileAppender，50MB × 10），**不归本脚本管**。
#  两者分工见 docs\日志规范.md §4。
#
#  三条安全约束（都是"差点出事"总结出来的）
#  ----------------------------------------
#  1. 只动 -LogDir 目录**直接子级**的 .log / .log.N 文件，绝不递归、绝不删目录；
#  2. **被占用就跳过**：正在运行的服务对 .log 持有句柄。此时改名/截断都会失败，
#     而"截断一个 append 中的文件"在 Windows 上还有更坏的表现 ——
#     写者的文件偏移不会回退，后续写入会从旧偏移开始，中间填满 NUL 字节（稀疏文件）。
#     所以宁可不处理，也不做半截动作。
#  3. 绝不影响启动：任何异常都只告警，退出码恒 0。start-all.ps1 会在启动前调它。
#
#  用法
#  ----
#    powershell -File deploy\scripts\trim-logs.ps1 -DryRun          # 只看会做什么
#    powershell -File deploy\scripts\trim-logs.ps1                  # 默认 20MB / 14 天
#    powershell -File deploy\scripts\trim-logs.ps1 -MaxMB 5 -KeepDays 7
#
#  本文件落盘规矩：UTF-8 with BOM + CRLF（PowerShell 5.1 读无 BOM 的 UTF-8
#  会按 ANSI 解码 → 中文乱码）。改完请确认这两条没被破坏。
# =====================================================================

[CmdletBinding()]
param(
    # 单个 .log 超过该体积就轮转（改名成 .1，旧的 .1 被覆盖）
    [int]$MaxMB = 20,
    # 超过该天数没有写入的 .log 直接删除
    [int]$KeepDays = 14,
    # 只报告不动作
    [switch]$DryRun,
    # 日志目录；默认 <仓库根>\deploy\logs
    [string]$LogDir
)

$ErrorActionPreference = 'Continue'

if (-not $LogDir -or $LogDir.Trim() -eq '') {
    $LogDir = Join-Path (Split-Path -Parent $PSScriptRoot) 'logs'
}

if (-not (Test-Path $LogDir)) {
    Write-Host "  [skip] 日志目录不存在: $LogDir"
    exit 0
}

$verb = if ($DryRun) { '将' } else { '已' }
$cutoff = (Get-Date).AddDays(-1 * [Math]::Abs($KeepDays))
$maxBytes = [int64]$MaxMB * 1MB
$rotated = 0
$deleted = 0
$skipped = 0

Write-Host "== 收口控制台捕获日志 =="
Write-Host "   目录   $LogDir"
Write-Host "   规则   超过 ${MaxMB}MB 轮转为 .1；超过 ${KeepDays} 天未写入则删除"
if ($DryRun) { Write-Host "   模式   演练（不会改动任何文件）" -ForegroundColor Yellow }
Write-Host ''

# 判断文件是否被占用：以"独占方式"打开，失败即视为被占用
function Test-FileLocked {
    param([Parameter(Mandatory)][string]$Path)
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open,
            [System.IO.FileAccess]::Read, [System.IO.FileShare]::None)
        $fs.Close()
        return $false
    } catch {
        return $true
    }
}

# 只取直接子级：不递归，避免误伤将来可能的子目录（如 backups\）
$files = @(Get-ChildItem -LiteralPath $LogDir -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like '*.log' -or $_.Name -like '*.log.*' })

if ($files.Count -eq 0) {
    Write-Host '   （没有 .log 文件，无需处理）'
    exit 0
}

foreach ($f in $files) {
    # --- 规则一：过期删除 ---
    if ($f.LastWriteTime -lt $cutoff) {
        if (Test-FileLocked -Path $f.FullName) {
            Write-Host ("   [占用] {0,-28} {1,8:N1}MB 超过 {2} 天，但正被服务写入，跳过" -f `
                $f.Name, ($f.Length / 1MB), $KeepDays) -ForegroundColor DarkYellow
            $skipped++
            continue
        }
        if ($DryRun) {
            Write-Host ("   [将删] {0,-28} {1,8:N1}MB 最后写入 {2}" -f `
                $f.Name, ($f.Length / 1MB), $f.LastWriteTime.ToString('yyyy-MM-dd HH:mm'))
        } else {
            Remove-Item -LiteralPath $f.FullName -Force
            Write-Host ("   [已删] {0,-28} {1,8:N1}MB 最后写入 {2}" -f `
                $f.Name, ($f.Length / 1MB), $f.LastWriteTime.ToString('yyyy-MM-dd HH:mm'))
        }
        $deleted++
        continue
    }

    # --- 规则二：超体积轮转（只对基础 .log 做；.log.N 等它自然过期）---
    if ($f.Name -notlike '*.log') { continue }
    if ($f.Length -le $maxBytes) { continue }

    if (Test-FileLocked -Path $f.FullName) {
        Write-Host ("   [占用] {0,-28} {1,8:N1}MB 超过 {2}MB，但正被服务写入，跳过" -f `
            $f.Name, ($f.Length / 1MB), $MaxMB) -ForegroundColor DarkYellow
        Write-Host '          停服后重跑本脚本即可收口（或在 start-all.ps1 启动前自动收口）'
        $skipped++
        continue
    }

    if ($DryRun) {
        Write-Host ("   [将轮转] {0,-26} {1,8:N1}MB -> {0}.1" -f $f.Name, ($f.Length / 1MB))
    } else {
        # 覆盖旧的 .1：总量被钳在 ~2×MaxMB，不会无限增长
        Move-Item -LiteralPath $f.FullName -Destination ($f.FullName + '.1') -Force
        Write-Host ("   [已轮转] {0,-26} {1,8:N1}MB -> {0}.1" -f $f.Name, ($f.Length / 1MB))
    }
    $rotated++
}

Write-Host ''
Write-Host ("   小结：扫描 {0} 个，轮转 {1} 个，删除 {2} 个，跳过（被占用）{3} 个" -f `
    $files.Count, $rotated, $deleted, $skipped)
exit 0
