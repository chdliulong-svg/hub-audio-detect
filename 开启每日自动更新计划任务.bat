@echo off
cd /d "%~dp0"
chcp 65001 >nul
title 注册系统每日凌晨自动更新计划任务

echo ======================================================================
echo 🕒 正在配置【每日凌晨 02:00 自动检查并更新】Windows 计划任务...
echo ======================================================================
echo.

schtasks /create /tn "WindTurbineAudio_AutoUpdate" /tr "python \"%~dp0auto_update.py\" --silent" /sc daily /st 02:00 /f /rl highest
if %errorlevel% equ 0 (
    echo.
    echo ======================================================================
    echo [OK] 自动更新计划任务已成功建立！
    echo >> 触发周期: 每天凌晨 02:00
    echo >> 静默机制: 仅在检测到 GitHub 有新版本时才自动拉取并重启，无更新时静默退出
    echo ======================================================================
) else (
    echo.
    echo [ERROR] 注册计划任务失败，请右键选择【以管理员身份运行】后重试！
)

echo.
pause
