@echo off
chcp 65001 >nul
title Collision Audio Monitor Debug Console
echo ======================================================================
echo 🌟 正在启动 3 核心声学大模型实时监控系统 (前台调试模式，保留黑框窗口)...
echo 👉 监控大屏: http://127.0.0.1:8088
echo 👉 后台管理: http://127.0.0.1:8088/admin
echo ======================================================================
echo.

start "" http://127.0.0.1:8088
python app.py

pause
