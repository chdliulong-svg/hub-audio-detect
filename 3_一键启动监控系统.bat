@echo off
cd /d "%~dp0"
chcp 65001 >nul
python start_daemon.py
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] 启动过程遇到错误，退出码: %errorlevel%
    pause
)
