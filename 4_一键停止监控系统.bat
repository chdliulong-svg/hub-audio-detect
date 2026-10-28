@echo off
cd /d "%~dp0"
chcp 65001 >nul
python stop_daemon.py
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] 停止服务遇到错误，退出码: %errorlevel%
    pause
)
