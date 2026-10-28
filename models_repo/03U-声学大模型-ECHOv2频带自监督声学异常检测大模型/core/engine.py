# -*- coding: utf-8 -*-
"""
方案 03U：ECHOv2 (频带自监督声学异常检测大模型，Two-Level Band-Splitting 架构) 诊断与 4 面板诊断图谱渲染引擎
(ECHOv2 Audio Diagnostic Engine & 4-Panel Visualization Pipeline)
2.0s 窗长 + 1.0s 步长 + 物理自适应电压轨检测 + 削顶方波舍弃 + 极速稳态能量旁路 + 85/15 软加权聚合
"""

import os
import sys
import time
import json
import numpy as np
from scipy import signal

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm

def _init_cjk_fonts():
    cur_dir = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(os.path.dirname(cur_dir), "assets", "fonts", "simsun.ttc"),
        os.path.join(cur_dir, "assets", "fonts", "simsun.ttc"),
        os.path.join(os.path.dirname(os.path.dirname(cur_dir)), "assets", "fonts", "simsun.ttc"),
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/msyh.ttc",
        "/usr/share/fonts/truetype/simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc"
    ]
    for fp in candidates:
        if os.path.exists(fp):
            try:
                fm.fontManager.addfont(fp)
                break
            except Exception:
                pass
    plt.rcParams['font.sans-serif'] = ['SimSun', 'Microsoft YaHei', 'SimHei', 'DejaVu Sans']
    plt.rcParams['axes.unicode_minus'] = False

_init_cjk_fonts()

import torch
from .echov2_model import OfficialECHOv2AudioModel, TRANSFER_HEAD_PATH

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(ROOT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common.audio_preprocessor import preprocess_audio_for_model, aggregate_slice_predictions

RECORDS_DIR = os.path.join(ROOT_DIR, "records")
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")
WEIGHTS_PATH = TRANSFER_HEAD_PATH
os.makedirs(RECORDS_DIR, exist_ok=True)


class ECHOv2AudioEngine:
    def __init__(self, config_path=CONFIG_PATH, weights_path=WEIGHTS_PATH, device=None, model=None):
        self.config_path = config_path
        self.weights_path = weights_path
        if device is None:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.device = device
        if model is not None:
            self.model = model
        else:
            self.model = OfficialECHOv2AudioModel(weights_path=weights_path, device=device)
        self.sr = 10000
        self.alarm_thresh = 90.0
        self.suspect_threshold = 50.0
        self.reload_config()

    def reload_config(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self.alarm_thresh = float(cfg.get("alarm_threshold_percent", 90.0))
                self.suspect_threshold = float(cfg.get("suspect_threshold_percent", 50.0))
            except Exception:
                pass

    def analyze_audio(self, file_path):
        return self.analyze_audio_file(file_path, output_dir=RECORDS_DIR, generate_plot=True)

    def analyze_audio_file(self, file_path, output_dir=RECORDS_DIR, generate_plot=False):
        t0 = time.perf_counter()
        fn = os.path.basename(file_path)

        should_discard, clip_rep, x = preprocess_audio_for_model(file_path, target_sr=self.sr)
        sr = self.sr

        if should_discard:
            infer_ms = (time.perf_counter() - t0) * 1000.0
            peak_amp = float(np.max(np.abs(x)) * 100.0) if len(x) > 0 else 0.0
            return {
                "name": fn,
                "file_name": fn,
                "decision_state": "DISCARD",
                "classification": "持续削顶故障舍弃",
                "metal_collision_probability": 0.0,
                "confidence_percent": 0.0,
                "top1_confidence": 0.0,
                "top2_confidence": 0.0,
                "is_alarm": False,
                "is_suspect": False,
                "is_discarded_by_preprocessor": True,
                "preprocessor_report": clip_rep,
                "inference_time_ms": round(infer_ms, 2),
                "peak_amplitude_pct": round(peak_amp, 2),
                "diagnostic_plot": None
            }

        # 方案 A: 极低能量底噪与稳态运转杂音前置快速旁路
        if clip_rep.get("is_quiet_normal", False):
            infer_ms = (time.perf_counter() - t0) * 1000.0
            peak_amp = float(np.max(np.abs(x)) * 100.0) if len(x) > 0 else 0.0
            return {
                "name": fn,
                "file_name": fn,
                "decision_state": "NORMAL",
                "classification": "正常运转背景 (旁路放行)",
                "metal_collision_probability": 1.0,
                "confidence_percent": 1.0,
                "top1_confidence": 1.0,
                "top2_confidence": 1.0,
                "is_alarm": False,
                "is_suspect": False,
                "is_discarded_by_preprocessor": False,
                "preprocessor_report": clip_rep,
                "inference_time_ms": round(infer_ms, 2),
                "peak_amplitude_pct": round(peak_amp, 2),
                "diagnostic_plot": None
            }

        win_len = int(2.0 * sr)  # 2.0s 窗长
        hop_len = int(1.0 * sr)  # 1.0s 步长
        n_samples = len(x)

        if n_samples < win_len:
            pad_w = win_len - n_samples
            slices = [np.pad(x, (0, pad_w), mode='constant')]
            slice_times = [0.0]
        else:
            slices = []
            slice_times = []
            for st in range(0, n_samples - win_len + 1, hop_len):
                slices.append(x[st:st + win_len])
                slice_times.append(st / sr)
            if (n_samples - win_len) % hop_len != 0:
                st = n_samples - win_len
                slices.append(x[st:st + win_len])
                slice_times.append(st / sr)

        slice_probs = []
        for sl in slices:
            pred = self.model.predict(sl, orig_sr=sr)
            p_coll = pred["collision_confidence_pct"]
            slice_probs.append(p_coll)

        agg_prob, top1_p, top2_p = aggregate_slice_predictions(slice_probs, top1_weight=0.85, top2_weight=0.15)

        if agg_prob >= self.alarm_thresh:
            decision_state = "ALARM"
            cls_cn = "金属撞击高危告警"
        elif agg_prob >= self.suspect_threshold:
            decision_state = "SUSPECT"
            cls_cn = "疑似冲击关注预警"
        else:
            decision_state = "NORMAL"
            cls_cn = "正常背景运转"

        infer_ms = (time.perf_counter() - t0) * 1000.0
        peak_amp = float(np.max(np.abs(x)) * 100.0) if len(x) > 0 else 0.0

        plot_path = None
        if generate_plot:
            plot_dir = os.path.join(output_dir, "diagnostic_plots")
            os.makedirs(plot_dir, exist_ok=True)
            plot_path = os.path.join(plot_dir, f"{os.path.splitext(fn)[0]}_03u_diag.png")
            self._render_diagnostic_plot(
                fn=fn,
                audio=x,
                sr=sr,
                slice_times=slice_times,
                slice_probs=slice_probs,
                agg_prob=agg_prob,
                decision_state=decision_state,
                save_path=plot_path
            )

        return {
            "name": fn,
            "file_name": fn,
            "decision_state": decision_state,
            "classification": cls_cn,
            "metal_collision_probability": round(agg_prob, 2),
            "confidence_percent": round(agg_prob, 2),
            "top1_confidence": round(top1_p, 2),
            "top2_confidence": round(top2_p, 2),
            "is_alarm": decision_state == "ALARM",
            "is_suspect": decision_state == "SUSPECT",
            "is_discarded_by_preprocessor": False,
            "preprocessor_report": clip_rep,
            "inference_time_ms": round(infer_ms, 2),
            "peak_amplitude_pct": round(peak_amp, 2),
            "diagnostic_plot": plot_path
        }

    def analyze_audio_array(self, audio_array, sr=10000):
        t0 = time.perf_counter()
        should_discard, clip_rep, x = preprocess_audio_for_model(audio_array, target_sr=self.sr)
        if should_discard:
            infer_ms = (time.perf_counter() - t0) * 1000.0
            return {
                "decision_state": "DISCARD",
                "confidence_percent": 0.0,
                "is_alarm": False,
                "is_suspect": False,
                "is_discarded_by_preprocessor": True,
                "inference_time_ms": round(infer_ms, 2)
            }
        pred = self.model.predict(x, orig_sr=self.sr)
        p_coll = pred["collision_confidence_pct"]
        if p_coll >= self.alarm_thresh:
            st = "ALARM"
        elif p_coll >= self.suspect_threshold:
            st = "SUSPECT"
        else:
            st = "NORMAL"
        infer_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "decision_state": st,
            "confidence_percent": p_coll,
            "is_alarm": st == "ALARM",
            "is_suspect": st == "SUSPECT",
            "is_discarded_by_preprocessor": False,
            "inference_time_ms": round(infer_ms, 2)
        }

    def _render_diagnostic_plot(self, fn, audio, sr, slice_times, slice_probs, agg_prob, decision_state, save_path):
        fig, axes = plt.subplots(4, 1, figsize=(12, 10), sharex=False, gridspec_kw={'height_ratios': [1.2, 1.5, 1.2, 0.8]})
        t = np.arange(len(audio)) / sr

        # 1. 时域波形
        ax0 = axes[0]
        ax0.plot(t, audio, color="#9467bd", lw=0.8, alpha=0.85)
        ax0.set_ylabel("归一化幅值", fontsize=10)
        ax0.set_title(f"[03U ECHOv2] 诊断图谱: {fn} | 状态: {decision_state} (置信度: {agg_prob:.1f}%)", fontsize=12, fontweight='bold')
        ax0.grid(True, alpha=0.3, linestyle="--")
        ax0.set_xlim(0, max(t[-1] if len(t) > 0 else 1.0, 1.0))

        # 2. 语谱图 (0 ~ 5000 Hz)
        ax1 = axes[1]
        n_fft = 512
        hop = 128
        f, t_spec, Sxx = signal.spectrogram(audio, fs=sr, nperseg=n_fft, noverlap=n_fft - hop)
        Sxx_db = 10 * np.log10(Sxx + 1e-9)
        im = ax1.pcolormesh(t_spec, f, Sxx_db, shading='gouraud', cmap='plasma')
        ax1.set_ylabel("频率 (Hz)", fontsize=10)
        ax1.set_ylim(0, 5000)
        ax1.axhspan(1800, 4800, color='cyan', alpha=0.15, label='金属撞击特征谐振带 (1.8k~4.8kHz)')
        ax1.legend(loc='upper right', fontsize=9)
        fig.colorbar(im, ax=ax1, format="%+2.0f dB", pad=0.01)

        # 3. 滑窗置信度曲线
        ax2 = axes[2]
        if len(slice_times) > 0:
            ax2.plot(slice_times, slice_probs, marker='^', color='#d62728', lw=1.8, label='切片碰撞置信度 (%)')
            ax2.axhline(self.alarm_thresh, color='red', linestyle='--', label=f'告警门限 ({self.alarm_thresh:.0f}%)')
            ax2.axhline(self.suspect_threshold, color='orange', linestyle=':', label=f'预警门限 ({self.suspect_threshold:.0f}%)')
            ax2.set_ylabel("置信度 (%)", fontsize=10)
            ax2.set_ylim(0, 105)
            ax2.grid(True, alpha=0.3, linestyle="--")
            ax2.legend(loc='upper right', fontsize=9)

        # 4. 汇总信息卡片
        ax3 = axes[3]
        ax3.axis('off')
        color_box = "#d4edda" if decision_state == "NORMAL" else ("#fff3cd" if decision_state == "SUSPECT" else "#f8d7da")
        color_text = "#155724" if decision_state == "NORMAL" else ("#856404" if decision_state == "SUSPECT" else "#721c24")
        summary_text = (
            f"● 模型架构: ECHOv2 双层分频带大模型 (8个子带时域编码 + 12层跨带交互Transformer, 92.8M)\n"
            f"● 综合评估置信度: {agg_prob:.2f}% (85% Top1 + 15% Top2 软加权聚合)\n"
            f"● 现场诊断结论: 【{decision_state}】 | 门限策略: 告警≥{self.alarm_thresh:.0f}%, 预警≥{self.suspect_threshold:.0f}%\n"
            f"● 物理声学判据: 细粒子分频建模 + 1.8k~4.8kHz 高频金属特征子带谐振注意力交互"
        )
        ax3.text(0.02, 0.5, summary_text, transform=ax3.transAxes, fontsize=10.5, verticalalignment='center',
                 bbox=dict(boxstyle="round,pad=0.6", facecolor=color_box, edgecolor=color_text, lw=1.5), color=color_text)

        plt.tight_layout()
        plt.savefig(save_path, dpi=120, bbox_inches='tight')
        plt.close(fig)
