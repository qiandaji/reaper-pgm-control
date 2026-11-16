#!/bin/bash
# PGM Reaper 控制端 — macOS 启动脚本
set -e
cd "$(dirname "$0")"

PORT=61370
OSC_PORT=9101

# 已有实例在跑：直接打开页面，不重复启动（避免 OSC 端口冲突）
if lsof -nP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1; then
  echo "[pgm] 控制端已在运行：http://127.0.0.1:$PORT"
  open "http://127.0.0.1:$PORT" 2>/dev/null || true
  exit 0
fi

# OSC 监听端口被占：给出排查指引，不带着 traceback 崩溃
if lsof -nP -iUDP:$OSC_PORT >/dev/null 2>&1; then
  echo "[pgm] 错误：OSC 监听端口 $OSC_PORT 已被占用（可能是另一个控制端实例或其它程序）。"
  echo "[pgm] 释放端口：执行 lsof -nP -iUDP:$OSC_PORT 找到 PID，然后 kill <PID>，再重新运行本脚本。"
  exit 1
fi

if [ ! -d .venv ]; then
  echo "[pgm] 创建虚拟环境 .venv ..."
  python3 -m venv .venv
fi

echo "[pgm] 安装/更新依赖 ..."
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt

echo "[pgm] 启动服务：http://127.0.0.1:$PORT （Ctrl+C 停止）"
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT
