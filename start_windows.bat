@echo off
REM PGM Reaper 控制端 - Windows 启动脚本
setlocal
cd /d "%~dp0"

set PORT=61370
set OSC_PORT=9101

REM 已有实例在跑：直接打开页面，不重复启动（避免 OSC 端口冲突）
netstat -ano | findstr /C:":%PORT%" | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 (
  echo [pgm] 控制端已在运行：http://127.0.0.1:%PORT%
  start "" "http://127.0.0.1:%PORT%"
  exit /b 0
)

REM OSC 监听端口被占：给出排查指引，不带着 traceback 崩溃
netstat -ano | findstr /C:":%OSC_PORT%" >nul 2>&1
if not errorlevel 1 (
  echo [pgm] 错误：OSC 监听端口 %OSC_PORT% 已被占用（可能是另一个控制端实例或其它程序）。
  echo [pgm] 释放端口：执行 netstat -ano ^| findstr :%OSC_PORT% 找到 PID，然后 taskkill /PID ^<pid^> /F，再重新运行本脚本。
  exit /b 1
)

if not exist .venv (
  echo [pgm] 创建虚拟环境 .venv ...
  py -3 -m venv .venv 2>nul || python -m venv .venv
)

echo [pgm] 安装/更新依赖 ...
.venvScriptspython -m pip install -q --disable-pip-version-check -r requirements.txt

echo [pgm] 启动服务：http://127.0.0.1:%PORT% （Ctrl+C 停止）
.venvScriptspython -m uvicorn app.main:app --host 0.0.0.0 --port %PORT%
