@echo off
cd /d "%~dp0"
chcp 65001 >nul
title [0/4] 自动检测并下载声学大模型基座权重
echo ======================================================================
echo 🚀 正在检测并拉取 3 核心声学大模型基座权重 (03K + 03U + 03R)...
echo ======================================================================
echo.

python download_weights.py
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] 权重拉取遇到错误，退出码: %errorlevel%
    pause
    exit /b %errorlevel%
)

echo.
pause
