@echo off
cd /d "%~dp0"
chcp 65001 >nul
title [1/4] 安装部署 Python 环境依赖与大模型权重
echo ======================================================================
echo 🔧 正在配置【风电机组碰撞声学智能诊断系统】运行环境与模型权重...
echo ======================================================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] 未检测到 Python，请先安装 Python 3.9 ~ 3.12 并添加至系统 PATH！
    pause
    exit /b 1
)

echo [1/3] 正在检查并升级 pip 工具...
python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple

echo.
echo [2/3] 正在安装 requirements.txt 生产依赖库 (使用国内清华高速镜像源)...
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] 依赖安装失败，请检查网络连接后重试。
    pause
    exit /b %errorlevel%
)

echo.
echo [3/3] 正在自动检测并拉取 3 核心声学大模型预训练权重 (03K + 03U + 03R)...
python download_weights.py
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] 模型权重拉取未完全就绪，请检查后重试。
    pause
    exit /b %errorlevel%
)

echo.
echo ======================================================================
echo 🧪 正在执行生产部署完整性自检...
echo ======================================================================
python verify_deployment.py

pause
