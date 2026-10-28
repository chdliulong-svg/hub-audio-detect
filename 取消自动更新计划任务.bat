@echo off
cd /d "%~dp0"
chcp 65001 >nul
title 取消自动更新计划任务

echo ======================================================================
echo 正在移除【WindTurbineAudio_AutoUpdate】计划任务...
echo ======================================================================
echo.

schtasks /delete /tn "WindTurbineAudio_AutoUpdate" /f
if %errorlevel% equ 0 (
    echo.
    echo [OK] 计划任务已成功删除，系统将停止定时自动更新。
) else (
    echo.
    echo [INFO] 未找到该计划任务或已被移除。
)

echo.
pause
