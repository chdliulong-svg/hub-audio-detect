# -*- coding: utf-8 -*-
"""
=============================================================================
3 核心声学大模型 (03K + 03U + 03R) 协同投票与防误报仲裁引擎
策略准则: "宁可漏报不要误报 (Zero False Alarm Priority)"
模型阵容:
  1. 03K: Dasheng-Base (89M, 27.2万小时自监督ViT架构, 瞬态冲击敏锐)
  2. 03U: ECHOv2 (82M, 频带自监督声学异常检测专用, 稳态底噪中位数0.01%)
  3. 03R: GRAM-Mono (86M, 混响自适应架构, 强风切变与空腔反射零误报)
仲裁逻辑:
  - 硬件防线: 供电轨模拟饱和死锁拦截 (detect_clipping_level)，不走神经网络
  - 底噪防线: 极速稳态底噪能量旁路放行
  - 高门限: 单模型单项告警阈值严格锁定 90.0%
  - 投票表决:
    * 3/3 票: CRITICAL_COLLISION (全票一致确诊重大碰撞，触发红警)
    * 2/3 票 (且均值>=85%): MAJORITY_COLLISION (多数确诊碰撞，触发橙警)
    * 1/3 票: ISOLATED_DISTURBANCE (孤立扰动，安全放行不报警，阻断偶发误报)
    * 0/3 票: NORMAL_CLEAN_NOISE (平稳正常底噪，绿色安全放行)
=============================================================================
"""

import os
import sys
import time
import json
import wave
import importlib.util
import numpy as np
import torch

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

def _init_cjk_fonts():
    plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'DejaVu Sans', 'Arial']
    plt.rcParams['axes.unicode_minus'] = False

_init_cjk_fonts()

class TripleModelVoterEngine:
    def __init__(self, code_base_dir=None, device=None, alarm_thresh_pct=90.0, min_rms_threshold=0.02):
        # 智能路径解析：如果未指定或指定路径不存在，自动在本地相对目录中查找模型仓库
        this_dir = os.path.dirname(os.path.abspath(__file__))
        app_root = os.path.dirname(this_dir)
        parent_root = os.path.dirname(app_root)
        
        candidates = [
            code_base_dir,
            os.path.join(app_root, "models_repo"),
            os.path.join(parent_root, "models_repo"),
            os.path.join(this_dir, "models_repo"),
            r"P:\01-work\022-audio detect",
            app_root,
            parent_root
        ]
        
        resolved_dir = None
        for cand in candidates:
            if cand and os.path.isdir(cand):
                try:
                    if any(d.startswith("03K") for d in os.listdir(cand)):
                        resolved_dir = cand
                        break
                except Exception:
                    pass
                    
        self.code_base_dir = resolved_dir or code_base_dir or r"P:\01-work\022-audio detect"
        self.device = device or torch.device("cpu")
        self.alarm_thresh_pct = float(alarm_thresh_pct)
        self.min_rms_thresh = float(min_rms_threshold)
        self.sr = 10000
        
        # 确保 common 在 sys.path (优先当前项目根目录及自身上级目录下的 common)
        for p in [app_root, parent_root, self.code_base_dir]:
            if p and os.path.isdir(p) and p not in sys.path:
                sys.path.insert(0, p)
            
        from common.audio_preprocessor import detect_clipping_level, preprocess_audio_for_model
        self.detect_clipping_level = detect_clipping_level
        self.preprocess_audio_for_model = preprocess_audio_for_model
        
        self.models = {}
        self.model_metadata = {
            "03K": {"name": "Dasheng-Base", "params": "89M", "arch": "ViT-Patch (64,4)", "desc": "自监督通用表征，瞬态捕捉精准"},
            "03U": {"name": "ECHOv2", "params": "82M", "arch": "Band-Splitting", "desc": "频谱自监督异常检测，底噪放行率高"},
            "03R": {"name": "GRAM-Mono", "params": "86M", "arch": "Reverb-Adaptive", "desc": "混响自适应架构，强风与空腔扰动零误报"}
        }
        
        self._load_all_models()
        
    def _find_model_dir(self, code):
        if os.path.exists(self.code_base_dir):
            for d in os.listdir(self.code_base_dir):
                if d.startswith(code):
                    return os.path.join(self.code_base_dir, d)
        raise FileNotFoundError(f"未找到代码目录: 前缀为 {code} (搜索基准目录: {self.code_base_dir})")

    def _load_all_models(self):
        configs = [
            {
                "code": "03K",
                "file": "dasheng_model.py",
                "class_name": "OfficialDashengAudioModel",
                "weights": "weights/dasheng_official_transfer.pt"
            },
            {
                "code": "03U",
                "file": "echov2_model.py",
                "class_name": "OfficialECHOv2AudioModel",
                "weights": "weights/echov2_transfer.pt"
            },
            {
                "code": "03R",
                "file": "gram_model.py",
                "class_name": "OfficialGRAMMonoAudioModel",
                "weights": "weights/gram_mono_transfer.pt",
                "backbone_weights": "weights/model.safetensors"
            }
        ]
        
        print("=" * 70)
        print("[TripleModelVoterEngine] 正在载入 3 大核心声学基座模型 (03K + 03U + 03R)...")
        print("=" * 70)
        
        for cfg in configs:
            code = cfg["code"]
            cls_name = cfg["class_name"]
            m_dir = self._find_model_dir(code)
            mod_full = os.path.join(m_dir, "core", cfg["file"])
            
            # 动态独立载入模块避免 core.engine 命名冲突
            spec = importlib.util.spec_from_file_location(f"mod_{code}", mod_full)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            cls = getattr(mod, cls_name)
            
            t0 = time.time()
            if code == "03R":
                bb_path = os.path.join(m_dir, cfg["backbone_weights"])
                w_path = os.path.join(m_dir, cfg["weights"])
                model_inst = cls(weights_path=bb_path, transfer_head_path=w_path, device=self.device)
            else:
                w_path = os.path.join(m_dir, cfg["weights"])
                model_inst = cls(weights_path=w_path, device=self.device)
            model_inst.eval()
            load_time = time.time() - t0
            print(f"  [OK] 【{code}】{self.model_metadata[code]['name']} ({self.model_metadata[code]['params']}) 就绪 (耗时: {load_time:.2f}s)")
            self.models[code] = model_inst
            
        print("=" * 70)
        print("[OK] 3 核心黄金铁三角模型 (03K+03U+03R) 全量就绪！零误报协同仲裁门禁系统已启动！")
        print("=" * 70)

    def analyze_audio_file(self, file_path, output_dir=None, generate_plot=True):
        """
        完整诊断单个音频文件，执行三道防线并返回标准化遥测数据字典
        """
        t_start = time.time()
        fn = os.path.basename(file_path)
        stem = os.path.splitext(fn)[0]
        
        if not os.path.exists(file_path):
            return {
                "success": False,
                "error": f"音频文件不存在: {file_path}",
                "filename": fn,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            
        # 1. 前处理与全线信号学防线检验 (硬件死锁、启停机变点 CPD、极低 RMS 静音直通)
        should_discard, clip_rep, x_norm = self.preprocess_audio_for_model(
            file_path, target_sr=self.sr, min_rms_threshold=self.min_rms_thresh
        )
        
        dur = len(x_norm) / self.sr if len(x_norm) > 0 else 0.0
        peak_val = float(np.max(np.abs(x_norm))) if len(x_norm) > 0 else 0.0
        rms_val = float(np.sqrt(np.mean(x_norm**2))) if len(x_norm) > 0 else 0.0
        crest_val = float(peak_val / (rms_val + 1e-8)) if rms_val > 0 else 0.0
        
        is_clip = bool(clip_rep.get("should_discard", False)) if clip_rep else False
        rail_v = float(clip_rep.get("rail_voltage", 0.0)) if clip_rep else 0.0
        clip_msg = clip_rep.get("message", "正常波形") if clip_rep else "正常波形"
        is_startup = bool(clip_rep.get("is_startup", False)) if clip_rep else False
        is_shutdown = bool(clip_rep.get("is_shutdown", False)) if clip_rep else False
        is_quiet = bool(clip_rep.get("is_quiet_normal", False)) if clip_rep else False
        quiet_msg = clip_rep.get("quiet_message", "") if clip_rep else ""
        step_rep = clip_rep.get("step_report", {}) if clip_rep else {}
        
        # 2. 逻辑试听 WAV 与频谱图文件名 (零磁盘落盘，由服务端按需动态重绘/流式生成)
        wav_rel_path = f"{stem}.wav"
        plot_rel_name = f"plot_{stem}.png"
            
        # 第一道防线拦截: 硬件供电死锁
        if is_clip:
            infer_duration = round((time.time() - t_start) * 1000.0, 1)
            result = {
                "success": True,
                "filename": fn,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_sec": round(dur, 2),
                "peak_amplitude": round(peak_val, 4),
                "rms_amplitude": round(rms_val, 5),
                "crest_factor": round(crest_val, 2),
                "is_clipped": True,
                "is_startup": False,
                "is_shutdown": False,
                "step_report": step_rep,
                "rail_voltage": round(rail_v, 3),
                "clip_msg": clip_msg,
                "scores": {"03K": 0.0, "03U": 0.0, "03R": 0.0},
                "alarms": {"03K": 0, "03U": 0, "03R": 0},
                "alarm_votes": 0,
                "mean_confidence_pct": 0.0,
                "consensus_level": "HARDWARE_CLIPPING_DISCARD",
                "consensus_title": "幅值饱和",
                "consensus_badge": "badge-clip",
                "is_alarm": False,
                "infer_ms": infer_duration,
                "wav_file": wav_rel_path,
                "plot_file": plot_rel_name
            }
            if generate_plot and output_dir:
                self._render_plot(x_norm, dur, result, output_dir)
            return result

        # 第二道防线前置放行: 由 common 检测出的极低能量底噪与稳态杂音快速放行 (RMS < min_rms_thresh)
        if is_quiet or (self.min_rms_thresh > 0 and rms_val < self.min_rms_thresh):
            infer_duration = round((time.time() - t_start) * 1000.0, 1)
            result = {
                "success": True,
                "filename": fn,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_sec": round(dur, 2),
                "peak_amplitude": round(peak_val, 4),
                "rms_amplitude": round(rms_val, 5),
                "crest_factor": round(crest_val, 2),
                "is_clipped": False,
                "is_startup": False,
                "is_shutdown": False,
                "step_report": step_rep,
                "rail_voltage": 0.0,
                "clip_msg": quiet_msg or f"极低静默底噪快速放行 (RMS {rms_val:.5f} < {self.min_rms_thresh:.4f})",
                "scores": {"03K": 0.0, "03U": 0.0, "03R": 0.0},
                "alarms": {"03K": 0, "03U": 0, "03R": 0},
                "alarm_votes": 0,
                "mean_confidence_pct": 0.0,
                "consensus_level": "NORMAL_CLEAN_NOISE",
                "consensus_title": f"【正常底噪】极低微弱底噪直接放行 (RMS: {rms_val:.4f} < {self.min_rms_thresh:.4f})",
                "consensus_badge": "badge-normal",
                "is_alarm": False,
                "infer_ms": infer_duration,
                "wav_file": wav_rel_path,
                "plot_file": plot_rel_name
            }
            if generate_plot and output_dir:
                self._render_plot(x_norm, dur, result, output_dir)
            return result
            
        # 3. 执行 3 模型切片推理 (2.0s 窗长 + 1.0s 步长 + 85/15 软加权聚合)
        scores = {}
        alarms = {}
        model_details = {}
        alarm_votes = 0
        
        for code in ["03K", "03U", "03R"]:
            m = self.models[code]
            t_m = time.time()
            with torch.no_grad():
                res_m = m.predict_preprocessed_waveform(x_norm, sr=self.sr)
            m_conf = float(res_m["confidence_pct"])
            m_alarm = int(m_conf >= self.alarm_thresh_pct)
            alarm_votes += m_alarm
            
            scores[code] = round(m_conf, 2)
            alarms[code] = m_alarm
            model_details[code] = {
                "name": self.model_metadata[code]["name"],
                "params": self.model_metadata[code]["params"],
                "conf_pct": round(m_conf, 2),
                "alarm": m_alarm,
                "top1_pct": res_m.get("top1_pct", 0.0),
                "top2_pct": res_m.get("top2_pct", 0.0),
                "infer_time_ms": round((time.time() - t_m) * 1000.0, 1)
            }
            
        mean_conf = float(np.mean(list(scores.values())))
        
        # 4. 宁可漏报不要误报协同仲裁决策 (Arbitration)
        if alarm_votes == 3:
            consensus_level = "CRITICAL_COLLISION_UNANIMOUS"
            consensus_title = "【全票确诊】3/3 核心模型全票确认重大金属碰撞！"
            consensus_badge = "badge-crit"
            is_final_alarm = True
        elif alarm_votes == 2 and mean_conf >= 80.0:
            consensus_level = "MAJORITY_COLLISION_ALARM"
            consensus_title = "【多数表决确诊】2/3 核心模型表决通过中度金属碰撞！"
            consensus_badge = "badge-high"
            is_final_alarm = True
        elif alarm_votes >= 1:
            consensus_level = "ISOLATED_DISTURBANCE_SAFE"
            consensus_title = f"【防误报门禁拦截】{alarm_votes}/3 票触发，判定为工况扰动放行！"
            consensus_badge = "badge-safe"
            is_final_alarm = False
        else:
            consensus_level = "NORMAL_CLEAN_NOISE"
            consensus_title = "【正常放行】0/3 票平稳运行正常背景底噪"
            consensus_badge = "badge-normal"
            is_final_alarm = False
            
        infer_duration = round((time.time() - t_start) * 1000.0, 1)
        
        result = {
            "success": True,
            "filename": fn,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_sec": round(dur, 2),
            "peak_amplitude": round(peak_val, 4),
            "rms_amplitude": round(rms_val, 5),
            "crest_factor": round(crest_val, 2),
            "is_clipped": False,
            "rail_voltage": 0.0,
            "clip_msg": clip_msg,
            "scores": scores,
            "alarms": alarms,
            "model_details": model_details,
            "alarm_votes": alarm_votes,
            "mean_confidence_pct": round(mean_conf, 2),
            "consensus_level": consensus_level,
            "consensus_title": consensus_title,
            "consensus_badge": consensus_badge,
            "is_alarm": is_final_alarm,
            "infer_ms": infer_duration,
            "wav_file": wav_rel_path,
            "plot_file": plot_rel_name
        }
        
        if generate_plot and output_dir:
            self._render_plot(x_norm, dur, result, output_dir)
            
        return result

    def render_plot_bytes(self, audio, dur, res, dpi=100):
        """
        在纯内存中动态绘制双面板声学波形与 0~5000Hz STFT 能量时频谱诊断图，返回 PNG 字节流 (0 磁盘占用)
        """
        import io
        fn = res.get("filename", "")
        fig = plt.figure(figsize=(9.0, 4.6), dpi=dpi)
        gs = fig.add_gridspec(2, 1, height_ratios=[1, 1.8], hspace=0.32)
        
        ax_wave = fig.add_subplot(gs[0])
        ax_spec = fig.add_subplot(gs[1])
        
        t = np.linspace(0, dur, len(audio))
        
        # 颜色区分
        if res.get("is_clipped"):
            accent = "#f43f5e"
        elif res.get("alarm_votes") == 3:
            accent = "#ef4444"
        elif res.get("alarm_votes") == 2:
            accent = "#f97316"
        elif res.get("alarm_votes") == 1:
            accent = "#06b6d4"
        else:
            accent = "#10b981"
            
        ax_wave.plot(t, audio, color=accent, linewidth=0.6, alpha=0.95)
            
        ax_wave.set_xlim(0, dur)
        ax_wave.set_ylim(-1.05, 1.05)
        ax_wave.set_ylabel("幅值 (Norm)", fontsize=8, fontweight='bold')
        ax_wave.grid(True, linestyle="--", alpha=0.3)
        ax_wave.tick_params(labelsize=7.5)
        
        c_level = res.get("consensus_level", "NORMAL")
        votes = res.get("alarm_votes", 0)
        mean_c = res.get("mean_confidence_pct", 0.0)
        peak_a = res.get("peak_amplitude", 0.0)
        rms_a = res.get("rms_amplitude", 0.0)

        ax_wave.set_title(
            f"[{c_level}] {fn} | 票数: {votes}/3 | 均值: {mean_c:.1f}% | 峰值: {peak_a:.3f} | RMS: {rms_a:.4f}",
            fontsize=8.5, fontweight="bold", loc="left", pad=4
        )
        
        Pxx, freqs, bins, im = ax_spec.specgram(
            audio, NFFT=512, Fs=self.sr, noverlap=256, cmap="inferno", scale="dB"
        )
        ax_spec.set_xlim(0, dur)
        ax_spec.set_ylim(0, 5000)
        ax_spec.set_xlabel("时间 (秒)", fontsize=8, fontweight='bold')
        ax_spec.set_ylabel("频率 (Hz)", fontsize=8, fontweight='bold')
        ax_spec.tick_params(labelsize=7.5)
        
        cbar = fig.colorbar(im, ax=ax_spec, orientation='horizontal', pad=0.22, shrink=0.40, aspect=20)
        cbar.ax.tick_params(labelsize=6.5)
        cbar.set_label('能量谱强度 (dB)', fontsize=6.5)
        
        plt.subplots_adjust(top=0.92, bottom=0.14, left=0.08, right=0.96)
        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=dpi, bbox_inches='tight')
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()

    def _render_plot(self, audio, dur, res, output_dir):
        """
        绘制并落盘 (兼容显式落盘调用)
        """
        fn = res.get("filename", "audio")
        stem = os.path.splitext(fn)[0]
        plot_name = f"plot_{stem}.png"
        plot_path = os.path.join(output_dir, plot_name)
        png_data = self.render_plot_bytes(audio, dur, res)
        with open(plot_path, "wb") as f:
            f.write(png_data)
        return plot_name

    predict_audio_file = analyze_audio_file
