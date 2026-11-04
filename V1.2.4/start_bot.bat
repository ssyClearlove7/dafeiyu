@echo off
chcp 936 >nul
setlocal
rem ============================================================
rem  Start the wc-chatbot main loop.
rem  Usage: start_bot.bat [logfile]     default: run_bot.log
rem
rem  2026-10-29 改动：
rem    1) 启动前先查 wx_multi.py 是否已经在跑。在跑就拒绝启动——两个实例抢同一个
rem       微信 hook，表现出来就是「启动又不行了」（点了没反应 / 机器人不回话 / 重复发消息）。
rem    2) 每一步失败都停下来 pause 说明原因，不再一闪而过。
rem ============================================================
cd /d "%~dp0"
set "LOG=%~1"
if "%LOG%"=="" set "LOG=run_bot.log"
set "ERR=run_bot.err"
if not "%~1"=="" set "ERR=%~dpn1.err"
set "PY="
set "PYARG="

call :probe "%~dp0python\python.exe"
if not defined PY call :probe "%WX_PYTHON%"
if not defined PY call :probe python
if not defined PY call :probe python3
if not defined PY call :probe py -3

if not defined PY (
  echo.
  echo [start_bot] 找不到 python。修法三选一：
  echo   1^) 把 python.exe 放到 %~dp0python\
  echo   2^) 设环境变量 WX_PYTHON 指向 python.exe
  echo   3^) 装 Python 3.12 并加进 PATH
  echo.
  pause
  exit /b 2
)

rem ---- 单实例检查：已经有实例在跑就别再起一个 ----
set "RUNPIDS="
"%PY%" %PYARG% bot_running.py > "%TEMP%\dshbot_pids.txt" 2>nul
if errorlevel 2 goto :not_running
if not errorlevel 1 goto :not_running
set /p RUNPIDS=<"%TEMP%\dshbot_pids.txt"
echo.
echo [start_bot] 已经有一个实例在跑（pid %RUNPIDS%），这次不启动。
echo   要重启请先跑 stop_bot.bat，再跑本脚本。
echo   面板: http://127.0.0.1:8790/
echo.
timeout /t 10 >nul 2>nul
exit /b 4

:not_running
echo [start_bot] Output: %LOG%  Errors: %ERR%
echo [start_bot] Configure chat rooms at http://127.0.0.1:8790/
echo ==== start %date% %time% ====  1>>"%LOG%"
echo [start_bot] python = %PY%  1>>"%LOG%"
"%PY%" %PYARG% -u ensure_hook.py 1>>"%LOG%" 2>>"%ERR%"
if errorlevel 1 (
  echo.
  echo [start_bot] hook 注入失败，主循环没起来。
  echo   1^) 确认微信开着，且是固定版 4.1.10.27（%~dp0Weixin-4.1.10.27）
  echo   2^) 微信被自动更新或完整性重置过，就重跑一次 install-wechat-fixed.cmd
  echo   3^) 细节见 %LOG% 和 %ERR%
  echo.
  1>>"%LOG%" echo [start_bot] hook unavailable, abort
  pause
  exit /b 3
)
"%PY%" %PYARG% wx_multi.py --rooms rooms.json --rounds 999999 --gap 2  1>>"%LOG%" 2>>"%ERR%"
set "RC=%errorlevel%"
echo ==== exit %date% %time% code=%RC% ====  1>>"%LOG%"
if not "%RC%"=="0" (
  echo.
  echo [start_bot] 主循环退出了 ^(code=%RC%^)，最后几行日志：
  if exist "%LOG%" powershell -NoProfile -Command "Get-Content -Tail 6 -LiteralPath '%LOG%'"
  if exist "%ERR%" powershell -NoProfile -Command "Get-Content -Tail 6 -LiteralPath '%ERR%'"
  echo.
  echo 本窗口 20 秒后自动关闭。
  timeout /t 20 >nul 2>nul
)
exit /b %RC%

:probe
if "%~1"=="" exit /b 0
"%~1" %2 -V >nul 2>nul || exit /b 0
set "PY=%~1"
set "PYARG=%2"
exit /b 0
