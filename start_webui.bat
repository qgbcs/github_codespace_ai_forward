@echo off
chcp 65001 >nul
title Codespace Copilot Web UI
cd /d C:\test\py

echo ============================================================
echo   Codespace Copilot Agent 网页端
echo   启动后浏览器自动打开 http://127.0.0.1:1189
echo   前提: codespace 在线 + 本机常驻 PTY (pty_client_mqtt.py -t co)
echo ============================================================
echo.

C:\QGB\miniforge3\python.exe webui.py
set RC=%ERRORLEVEL%

if not "%RC%"=="0" (
  echo.
  echo [!] 服务异常退出, 退出码 %RC%
  pause
)
