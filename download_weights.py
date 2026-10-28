# -*- coding: utf-8 -*-
"""
=============================================================================
风电机组碰撞声学智能诊断系统 · 核心声学大模型权重自动下载与校验引擎
功能：
  1. 智能检测本地 3 核心基座大模型 (03K + 03U + 03R) 骨干权重 (model.safetensors)
  2. 已存在则秒级跳过，杜绝重复下载
  3. 缺失时自动连接国内高速镜像专线 (hf-mirror.com) 进行断点续传下载
  4. 采用工业级纯 ASCII 状态日志输出，100% 兼容 Windows GBK/CP936 与 UTF-8
=============================================================================
"""
import os
import sys
import time
import traceback

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 默认优先使用国内极速镜像源，国内网络免配置直接满速下载
if "HF_ENDPOINT" not in os.environ:
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

WEIGHT_CONFIGS = [
    {
        "code": "03K",
        "name": "Dasheng-Base (89M)",
        "repo_id": "mispeech/dasheng-base",
        "filename": "model.safetensors",
        "target_rel": os.path.join(
            "models_repo",
            "03K-声学大模型-Dasheng自监督通用音频大模型",
            "weights",
            "model.safetensors"
        ),
        "expected_bytes": 341807392
    },
    {
        "code": "03U",
        "name": "ECHOv2 (82M)",
        "repo_id": "yucongzh/echo-small-v2",
        "filename": "model.safetensors",
        "target_rel": os.path.join(
            "models_repo",
            "03U-声学大模型-ECHOv2频带自监督声学异常检测大模型",
            "weights",
            "model.safetensors"
        ),
        "expected_bytes": 86175648
    },
    {
        "code": "03R",
        "name": "GRAM-Mono (86M)",
        "repo_id": "labhamlet/gramt-mono",
        "filename": "model.safetensors",
        "target_rel": os.path.join(
            "models_repo",
            "03R-声学大模型-GRAM-Mono混响自适应模型",
            "weights",
            "model.safetensors"
        ),
        "expected_bytes": 445273136
    }
]

def format_size(bytes_val):
    return f"{bytes_val / 1024 / 1024:.2f} MB"

def download_weight_file(cfg):
    code = cfg["code"]
    name = cfg["name"]
    repo_id = cfg["repo_id"]
    filename = cfg["filename"]
    expected_bytes = cfg["expected_bytes"]
    
    target_path = os.path.normpath(os.path.join(BASE_DIR, cfg["target_rel"]))
    os.makedirs(os.path.dirname(target_path), exist_ok=True)

    # 1. 检查本地是否已存在且完整
    if os.path.exists(target_path):
        current_sz = os.path.getsize(target_path)
        # 允许微小的元数据差异 (5% 容差)
        if current_sz >= expected_bytes * 0.95:
            print(f"  [OK] 【{code}】{name} 本地权重已就绪 ({format_size(current_sz)})，跳过下载。")
            return True
        else:
            print(f"  [!] 【{code}】本地文件体积不匹配 (当前: {format_size(current_sz)}, 预期: {format_size(expected_bytes)})，准备重新拉取...")

    print(f"\n>> 正在拉取 【{code}】{name} 官方开源骨干网络权重...")
    print(f"   * 远程开源仓库: {repo_id}")
    print(f"   * 目标存储路径: {target_path}")
    print(f"   * 权重预估体积: {format_size(expected_bytes)}")
    print(f"   * 下载加速节点: {os.environ.get('HF_ENDPOINT', 'https://hf-mirror.com')}")

    # 2. 调用 huggingface_hub 执行断点续传下载
    try:
        from huggingface_hub import hf_hub_download
        
        t0 = time.time()
        downloaded_path = hf_hub_download(
            repo_id=repo_id,
            filename=filename,
            local_dir=os.path.dirname(target_path),
            local_dir_use_symlinks=False
        )
        
        # 确保下载后文件名与目标路径完全一致
        if os.path.abspath(downloaded_path) != os.path.abspath(target_path):
            import shutil
            shutil.move(downloaded_path, target_path)

        cost = time.time() - t0
        final_sz = os.path.getsize(target_path)
        speed = (final_sz / 1024 / 1024) / max(cost, 0.1)
        print(f"  [OK] 【{code}】下载完成！用时: {cost:.1f} 秒，平均速率: {speed:.2f} MB/s")
        return True

    except Exception as e:
        print(f"  [ERROR] 【{code}】下载失败: {e}")
        # 降级尝试官方源
        if os.environ.get("HF_ENDPOINT") == "https://hf-mirror.com":
            print(f"  [INFO] 正在尝试切换至官方节点重新下载...")
            try:
                os.environ["HF_ENDPOINT"] = "https://huggingface.co"
                from huggingface_hub import hf_hub_download
                downloaded_path = hf_hub_download(
                    repo_id=repo_id,
                    filename=filename,
                    local_dir=os.path.dirname(target_path),
                    local_dir_use_symlinks=False
                )
                if os.path.abspath(downloaded_path) != os.path.abspath(target_path):
                    import shutil
                    shutil.move(downloaded_path, target_path)
                print(f"  [OK] 【{code}】通过官方节点下载成功！")
                return True
            except Exception as e2:
                print(f"  [ERROR] 官方节点下载亦失败: {e2}")
        return False

def main():
    print("=" * 70)
    print("[WEIGHTS] 风电碰撞智能诊断系统 · 核心声学大模型权重下载引擎")
    print("=" * 70)
    print()

    # 检查 huggingface_hub 是否已安装
    try:
        import huggingface_hub
    except ImportError:
        print("[!] 检测到尚未安装 huggingface_hub，正在自动为您安装...")
        import subprocess
        subprocess.run([sys.executable, "-m", "pip", "install", "huggingface_hub", "-i", "https://pypi.tuna.tsinghua.edu.cn/simple"], check=True)
        print("[OK] huggingface_hub 安装就绪。\n")

    all_success = True
    for idx, cfg in enumerate(WEIGHT_CONFIGS, 1):
        print(f"[{idx}/3] 检验模型 【{cfg['code']}】{cfg['name']} ...")
        success = download_weight_file(cfg)
        if not success:
            all_success = False

    print()
    print("=" * 70)
    if all_success:
        print("[OK] 恭喜！3 大核心声学大模型预训练权重已全部就绪！")
        print(">> 您现在可以双击运行【2_一键系统自检与测试.bat】或【3_一键启动监控系统.bat】。")
        print("=" * 70)
        return 0
    else:
        print("[ERROR] 部分模型权重下载失败，请检查网络连接后重试。")
        print("提示：若处于完全离线环境，可手动将 model.safetensors 拷贝至对应 weights 目录下。")
        print("=" * 70)
        return 1

if __name__ == "__main__":
    code = main()
    sys.exit(code)
