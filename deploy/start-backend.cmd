@echo off
rem =====================================================================
rem  Start agt_backend (port 8080)   [drone-agent001 / deploy]
rem
rem  Why this wrapper exists (S-1; see docs/specs/ , the spec titled in
rem  Chinese "Sheng Chan Ji Gai Jin Gui Yue"):
rem    The Python agent service (8000) now requires the header
rem    X-Agent-Key on every request, so the shared key has to be
rem    injected into this process as the environment variable
rem    AGENT_SHARED_KEY. Writing "set ... && java ..." directly into a
rem    scheduled task breaks cmd's quote-stripping rules, and it would
rem    also leak the key into the task arguments shown in Task Scheduler.
rem
rem  The key itself is NOT in the repository:
rem    deploy\secrets\agent.key   (gitignored, single line)
rem    value must equal agt_python\.env  AGENT_SHARED_KEY
rem
rem  Artifact name is fixed (pom <finalName>agt_backend</finalName>,
rem  spec E-2). The real version is NOT in the file name; check it at
rem  runtime:  GET http://127.0.0.1:8080/v1/version
rem
rem  HOUSE RULES for this file:
rem    * keep it ASCII-only and CRLF-terminated. cmd.exe mis-parses
rem      LF-only line endings, and non-ASCII bytes are decoded with the
rem      OEM codepage.
rem    * do NOT use "if (...) else (...)" blocks here: an unescaped ")"
rem      inside an echo line closes the block early. Use goto labels.
rem
rem  Usage:  deploy\start-backend.cmd
rem          scheduled task "drone-agent-8080-backend" runs this file.
rem          prefer:  deploy\scripts\start-all.ps1   (runs preflight first)
rem =====================================================================
setlocal

set "HERE=%~dp0"
set "KEY_FILE=%HERE%secrets\agent.key"

if not exist "%KEY_FILE%" goto :no_key

set "AGENT_SHARED_KEY="
set /p AGENT_SHARED_KEY=<"%KEY_FILE%"
if "%AGENT_SHARED_KEY%"=="" goto :empty_key
rem --- E-6 log dir: logback must not rely on the cwd (this script cd's into
rem     agt_backend/target), so hand it an absolute path via AGT_LOG_DIR.
rem     House rules for this file also apply to these lines: ASCII + CRLF.
for %%I in ("%HERE%..") do set "AGT_ROOT=%%~fI"
set "AGT_LOG_DIR=%AGT_ROOT%\deploy\logs"


rem JAVA_HOME wins; otherwise fall back to this machine's JDK path
set "JAVA_EXE=%JAVA_HOME%\bin\java.exe"
if not exist "%JAVA_EXE%" set "JAVA_EXE=D:\install\java\jdk\bin\java.exe"
if not exist "%JAVA_EXE%" goto :no_java

cd /d "%HERE%..\agt_backend\target"
if errorlevel 1 goto :no_target
if not exist "agt_backend.jar" goto :no_jar

"%JAVA_EXE%" -jar agt_backend.jar
goto :eof

:no_key
echo [FATAL] missing key file: %KEY_FILE%
echo [FATAL] generate one:  python -c "import secrets; print(secrets.token_urlsafe(32))"
echo [FATAL] then write it to that file AND to agt_python\.env , same value
echo [FATAL] refusing to start: fail-closed per spec S-1
exit /b 2

:empty_key
echo [FATAL] key file is empty: %KEY_FILE%
echo [FATAL] refusing to start: fail-closed per spec S-1
exit /b 2

:no_java
echo [FATAL] java.exe not found. Set JAVA_HOME or edit JAVA_EXE in this script.
exit /b 2

:no_target
echo [FATAL] cannot cd to agt_backend\target
exit /b 2

:no_jar
echo [FATAL] missing jar: agt_backend\target\agt_backend.jar
echo [FATAL] build it first:  cd agt_backend ^&^& mvn package -DskipTests
echo [FATAL] or run the deployment manual in docs\ (section: build)
exit /b 2
