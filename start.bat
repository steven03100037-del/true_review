@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 真评 - 本地服务

rem ===== [1/3] 首次运行：创建虚拟环境并安装依赖 =====
if not exist ".venv\Scripts\python.exe" (
  echo [1/3] 首次运行：创建虚拟环境并安装依赖，需要几分钟，请耐心等待……
  python -m venv .venv
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo 依赖安装失败，请检查网络后重试。
    pause
    exit /b 1
  )
)

rem ===== [2/3] 检查浏览器内核（已装则秒过） =====
echo [2/3] 检查浏览器内核……
".venv\Scripts\python.exe" -m playwright install chromium

rem ===== [3/3] 启动服务 =====
echo [3/3] 启动服务……
echo   本机访问：  http://127.0.0.1:8000
echo   手机访问：  同一 WiFi 下，Safari 打开 http://电脑IP:8000 （查 IP 方法见 README）
".venv\Scripts\python.exe" -m uvicorn backend.api:app --host 0.0.0.0 --port 8000
pause
