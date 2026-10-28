@echo off
chcp 65001 >nul
title [1/4] 安装部署 Python 环境依赖
echo ======================================================================
echo 🔧 正在配置【风电机组碰撞声学智能诊断系统】运行环境...
echo ======================================================================
echo.

python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3.9 ~ 3.12 并添加至系统 PATH！
    pause
    exit /b 1
)

echo [✓] 正在检查并升级 pip 工具...
python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple

echo.
echo [✓] 正在安装 requirements.txt 生产运行依赖库 (使用国内清华高速源)...
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

echo.
echo ======================================================================
echo 🧪 正在验证核心组件就绪状态...
echo ======================================================================
python verify_deployment.py

pause
