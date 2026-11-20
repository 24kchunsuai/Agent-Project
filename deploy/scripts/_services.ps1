# =====================================================================
#  drone-agent001 四服务清单 —— 单一真相来源
#
#  这是个被 dot-source 的片段（不是独立脚本），供：
#     deploy\scripts\install-tasks.ps1  注册计划任务
#     deploy\scripts\start-all.ps1      一键启动
#     deploy\scripts\stop-all.ps1       一键停止
#     deploy\scripts\status.ps1         查看状态
#  复用。改端口 / 改路径 / 改启动命令，**只改这一个文件**。
#
#  为什么要集中：历史上"启动命令"散在 README、计划任务定义、start-backend.cmd
#  三处，改一处忘一处就会出现"照文档起不来"——这正是企业级落地规约 E-2 要消除的。
#
#  两条硬约定（都是实测踩出来的，别改）
#   1. 顺序 = 依赖顺序：8060 -> 8000 -> 8080 -> 9090
#      （8080 既代理 8060 的登录，又转发 8000 的 agent）
#   2. 长驻服务必须跑在 OS 层计划任务里，且**必须是 S4U 非交互主体**：
#      默认的 Interactive 主体会给每个服务弹一个可见 cmd 窗口，窗口一关
#      （点 X / Ctrl+C）整棵进程树一起死（2026-09-24 实测 0xC000013A）。
#      S4U 跑在 session 0 → 无窗口，用户注销也不受影响。
#
#  本文件是 ASCII？不，含中文注释，落盘为 **UTF-8 with BOM + CRLF**：
#      PowerShell 5.1 读无 BOM 的 UTF-8 会按 ANSI 解码 → 中文乱码。
# =====================================================================

# 项目根：deploy\scripts\_services.ps1 -> deploy -> 仓库根
$AgtRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

# 外部系统 jsdstu-3（与 drone-agent001 同级目录；换机器时用 JSDSTU3_ROOT 覆盖）
$AgtJsDstu3 = if ($env:JSDSTU3_ROOT) { $env:JSDSTU3_ROOT } else { Join-Path (Split-Path -Parent $AgtRoot) 'jsdstu-3' }

# --- 解释器定位（环境变量优先，其次本机惯例路径，最后 PATH）---
$AgtPython = $env:AGT_PYTHON
if (-not $AgtPython -or -not (Test-Path $AgtPython)) {
    $cand = 'D:\install\python\anaconda\envs\python311\python.exe'
    $AgtPython = if (Test-Path $cand) { $cand } else { (Get-Command python -ErrorAction SilentlyContinue).Source }
}

$AgtJava = if ($env:JAVA_HOME -and (Test-Path "$env:JAVA_HOME\bin\java.exe")) { "$env:JAVA_HOME\bin\java.exe" }
           elseif (Test-Path 'D:\install\java\jdk\bin\java.exe') { 'D:\install\java\jdk\bin\java.exe' }
           else { (Get-Command java -ErrorAction SilentlyContinue).Source }

$AgtNpm = if (Test-Path 'C:\Program Files\nodejs\npm.cmd') { 'C:\Program Files\nodejs\npm.cmd' }
          else { (Get-Command npm -ErrorAction SilentlyContinue).Source }

$AgtLogDir = Join-Path $AgtRoot 'deploy\logs'

# --- 服务清单 ---
# Optional = $true 表示"未启动不阻塞其它服务"（外部系统，或页面可后起）
$AgtServices = @(
    [pscustomobject]@{
        Port          = 8060
        Name          = 'jsdstu-3'
        Role          = '无人机主控（外部系统）'
        TaskName      = 'drone-agent-8060-drone'
        Exe           = $AgtJava
        Args          = '-jar drone-0.0.1-SNAPSHOT.jar'
        WorkingDir    = Join-Path $AgtJsDstu3 'drone\target'
        Log           = Join-Path $AgtLogDir '8060-drone.log'
        ProbeUrl      = 'http://127.0.0.1:8060/v1/dashboard/summary'
        ProbeNeedle   = '"code"'
        ReadyTimeout  = 90
        Optional      = $true
        FixHint       = '它是外部系统：可在 IDEA 里起 DroneApplication，或先在该项目执行 mvn package'
    }
    [pscustomobject]@{
        Port          = 8000
        Name          = 'agt_python'
        Role          = '智能体服务（FastAPI + RAG）'
        TaskName      = 'drone-agent-8000-py'
        Exe           = $AgtPython
        Args          = '-m uvicorn app.main:app --host 127.0.0.1 --port 8000'
        WorkingDir    = Join-Path $AgtRoot 'agt_python'
        Log           = Join-Path $AgtLogDir '8000-agt_python.log'
        ProbeUrl      = 'http://127.0.0.1:8000/v1/health'
        ProbeNeedle   = '"code":2000'
        ReadyTimeout  = 60
        Optional      = $false
        FixHint       = '看日志尾部：密钥没配（AGENT_SHARED_KEY 为空会拒绝启动）、或依赖没装'
    }
    [pscustomobject]@{
        Port          = 8080
        Name          = 'agt_backend'
        Role          = '业务后端（Spring Boot，Vue 的唯一入口）'
        TaskName      = 'drone-agent-8080-backend'
        Exe           = Join-Path $AgtRoot 'deploy\start-backend.cmd'
        Args          = ''
        WorkingDir    = Join-Path $AgtRoot 'deploy'
        Log           = Join-Path $AgtLogDir '8080-agt_backend.log'
        ProbeUrl      = 'http://127.0.0.1:8080/v1/version'
        ProbeNeedle   = '"version"'
        ReadyTimeout  = 90
        Optional      = $false
        FixHint       = '常见：jar 未构建 / agent.key 缺失或不一致 / 8000 未就绪'
    }
    [pscustomobject]@{
        Port          = 9090
        Name          = 'agt_web'
        Role          = '前端页面（vue-cli dev server）'
        TaskName      = 'drone-agent-9090-web'
        Exe           = $AgtNpm
        Args          = 'run serve'
        WorkingDir    = Join-Path $AgtRoot 'agt_web'
        Log           = Join-Path $AgtLogDir '9090-agt_web.log'
        ProbeUrl      = 'http://127.0.0.1:9090/version.json'
        ProbeNeedle   = '"version"'
        ReadyTimeout  = 180
        Optional      = $false
        FixHint       = '首次编译较慢；若报错看 node_modules 是否装好'
    }
)

# --- 公共小工具 ---

# 计划任务要跑的命令行：cmd /c ""<exe>" <args> > "<log>" 2>&1"
# 注意最外层那对引号：cmd /c 后面整串是一个参数，内层 exe 再各自带引号。
function Get-AgtTaskArguments {
    param([Parameter(Mandatory)][object]$Service)
    $inner = if ($Service.Args) { '"{0}" {1}' -f $Service.Exe, $Service.Args } else { '"{0}"' -f $Service.Exe }
    return '/c "{0} > "{1}" 2>&1"' -f $inner, $Service.Log
}

# HTTP 探针：**必须显式绕开系统代理**（本机代理会让本机请求 502 / 空响应）
function Invoke-AgtProbe {
    param(
        [Parameter(Mandatory)][string]$Url,
        [string]$Needle = '',
        [int]$TimeoutSec = 6
    )
    try {
        $req = [System.Net.HttpWebRequest]::Create($Url)
        $req.Proxy = $null
        $req.Timeout = $TimeoutSec * 1000
        $req.UserAgent = 'agt-status'
        $resp = $req.GetResponse()
        $reader = New-Object System.IO.StreamReader($resp.GetResponseStream())
        $body = $reader.ReadToEnd()
        $code = [int]$resp.StatusCode
        $reader.Close(); $resp.Close()
        if ($Needle -and ($body -notlike "*$Needle*")) {
            return [pscustomobject]@{ Ok = $false; Info = "HTTP $code 但响应不含 $Needle" }
        }
        return [pscustomobject]@{ Ok = $true; Info = "HTTP $code" }
    } catch [System.Net.WebException] {
        $r = $_.Exception.Response
        if ($r) { return [pscustomobject]@{ Ok = $false; Info = "HTTP $([int]$r.StatusCode)" } }
        return [pscustomobject]@{ Ok = $false; Info = $_.Exception.Message }
    } catch {
        return [pscustomobject]@{ Ok = $false; Info = $_.Exception.Message }
    }
}

# 端口是否有人监听，返回监听进程 pid（无则 0）
function Get-AgtListenerPid {
    param([Parameter(Mandatory)][int]$Port)
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return 0
}

# 进程所在会话：0 = 非交互（S4U / 服务），1 = 当前交互桌面会话
function Get-AgtSessionId {
    param([Parameter(Mandatory)][int]$ProcessId)
    try { return (Get-Process -Id $ProcessId -ErrorAction Stop).SessionId } catch { return -1 }
}
