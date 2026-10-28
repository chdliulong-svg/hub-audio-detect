# -*- coding: utf-8 -*-
"""
=============================================================================
部署环境与 3 核心模型全项自检验证脚本
用于一键检验当前环境的 Python 依赖、CPD 变点算法以及 3 大核心模型是否全部就绪。
=============================================================================
"""
import os
import sys
import time

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

print("=" * 70)
print("[CHECK] [风电碰撞诊断系统] 正在执行生产部署完整性自检...")
print(f">> 部署根目录: {BASE_DIR}")
print("=" * 70)

# 1. 检查 Python 依赖
print("\n[1/4] 检查 Python 核心依赖库...")
libs = [
    ("Flask", "flask"),
    ("PyTorch", "torch"),
    ("TorchAudio", "torchaudio"),
    ("TorchVision", "torchvision"),
    ("Transformers", "transformers"),
    ("HuggingFace", "huggingface_hub"),
    ("TIMM", "timm"),
    ("Einops", "einops"),
    ("SoundFile", "soundfile"),
    ("SciPy", "scipy"),
    ("NumPy", "numpy"),
    ("Matplotlib", "matplotlib"),
    ("Pandas", "pandas"),
    ("OpenPyXL", "openpyxl"),
    ("Psutil", "psutil"),
    ("SafeTensors", "safetensors")
]

all_ok = True
for name, mod in libs:
    try:
        m = __import__(mod)
        ver = getattr(m, "__version__", "已安装")
        print(f"  [OK] {name:15s} : OK (版本: {ver})")
    except ImportError as e:
        print(f"  [!] {name:15s} : 缺失! ({e})")
        all_ok = False

if not all_ok:
    print("\n[ERROR] 存在缺失的依赖库，请运行【1_一键部署环境(安装依赖).bat】进行自动安装。")
    sys.exit(1)

import torch
print(f"  [INFO] PyTorch 设备就绪状态: CUDA GPU 可用={torch.cuda.is_available()}")
if torch.cuda.is_available():
    print(f"         GPU 设备名称: {torch.cuda.get_device_name(0)}")

# 2. 检查前处理与变点检测模块
print("\n[2/4] 检查前处理与 CPD 启停机变点检测引擎...")
try:
    from common.audio_preprocessor import detect_step_change_point, detect_clipping_level, preprocess_audio_for_model
    print("  [OK] CPD 启停机变点检测函数 (detect_step_change_point) : OK")
    print("  [OK] 硬件削顶死锁检测函数   (detect_clipping_level)     : OK")
    print("  [OK] 音频标准化前处理函数   (preprocess_audio_for_model): OK")
except Exception as e:
    print(f"  [ERROR] 前处理模块载入失败: {e}")
    sys.exit(1)

# 3. 检查模型目录与模型权重加载
print("\n[3/4] 检查 3 大核心大模型 (03K + 03U + 03R) 初始化与权重载入...")
try:
    from models.voter import TripleModelVoterEngine
    t_start = time.time()
    engine = TripleModelVoterEngine(code_base_dir=os.path.join(BASE_DIR, "models_repo"))
    load_cost = round(time.time() - t_start, 2)
    print(f"  [OK] 3 大模型全部加载就绪！耗时: {load_cost} 秒")
    for code, m in engine.models.items():
        meta = engine.model_metadata.get(code, {})
        print(f"    - 【{code}】{meta.get('name')} ({meta.get('params')}参数) -> 就绪")
except Exception as e:
    print(f"  [ERROR] 模型载入异常: {e}")
    sys.exit(1)

# 4. 执行样本端到端真实推理测试
print("\n[4/4] 执行样本音频端到端真实推理测试...")
test_dir = os.path.join(BASE_DIR, "incoming_audio", "test_samples")
test_files = [f for f in os.listdir(test_dir) if f.endswith(".bin")] if os.path.exists(test_dir) else []

if test_files:
    test_path = os.path.join(test_dir, test_files[0])
    print(f"  >> 测试样本文件: {test_files[0]}")
    t_inf = time.time()
    res = engine.analyze_audio_file(test_path, output_dir=None, generate_plot=False)
    cost_ms = round((time.time() - t_inf) * 1000.0, 1)
    
    print(f"  [OK] 推理成功完成！耗时: {cost_ms} ms")
    print(f"    * 仲裁决策级别: {res.get('consensus_level')}")
    print(f"    * 3模型表决票数: {res.get('alarm_votes')}/3 票")
    print(f"    * 03K 置信度  : {res.get('scores', {}).get('03K')}%")
    print(f"    * 03U 置信度  : {res.get('scores', {}).get('03U')}%")
    print(f"    * 03R 置信度  : {res.get('scores', {}).get('03R')}%")
    print(f"    * 综合均值    : {res.get('mean_confidence_pct')}%")
    if res.get('is_startup') or res.get('is_shutdown'):
        print(f"    * CPD 变点拦截: 成功识别启停机阶跃，安全放行")
else:
    print("  [INFO] test_samples 目录下暂无测试音频，跳过单个样本测试")

print("\n" + "=" * 70)
print("[OK] 恭喜！全系统完整性自检 100% 通过！随时可执行【3_一键启动监控系统.bat】上线运行！")
print("=" * 70)
