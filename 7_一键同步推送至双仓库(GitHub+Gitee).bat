@echo off
cd /d "%~dp0"
chcp 65001 >nul
title 同步代码至双仓库 (GitHub + Gitee)
echo ======================================================================
echo 🚀 正在检查并同步最新代码至 GitHub 与 Gitee 双远端仓库...
echo 1. GitHub: https://github.com/chdliulong-svg/hub-audio-detect
echo 2. Gitee:  https://gitee.com/chdliulong/hub-audio-detect
echo ======================================================================
echo.

git status -s
echo.
echo 正在执行提交与双平台同时推送...
git add -A
git commit -m "chore(sync): automated sync to GitHub and Gitee" 2>nul
git push origin main

if %errorlevel% equ 0 (
    echo.
    echo ======================================================================
    echo [OK] 恭喜！GitHub 与 Gitee 双平台代码已 100% 同步推送成功！
    echo ======================================================================
) else (
    echo.
    echo [!] 推送遇到问题，请检查网络或 Gitee/GitHub 访问权限。
)

echo.
pause
