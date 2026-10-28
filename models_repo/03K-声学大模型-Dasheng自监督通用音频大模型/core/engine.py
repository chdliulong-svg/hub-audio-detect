# -*- coding: utf-8 -*-
"""
方案 03K：Dasheng-Base 自监督通用音频大模型诊断与 4 面板诊断图谱渲染引擎
(Dasheng Audio Diagnostic Engine & 4-Panel Visualization Pipeline)
2.0s 窗长 + 1.0s 步长 + 物理自适应电压轨检测 + 削顶方波舍弃 + 极速稳态能量旁路
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
from .dasheng_model import OfficialDashengAudioModel, TRANSFER_HEAD_PATH

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(ROOT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from common.audio_preprocessor import preprocess_audio_for_model, aggregate_slice_predictions

RECORDS_DIR = os.path.join(ROOT_DIR, "records")
CONFIG_PATH = os.path.join(ROOT_DIR, "config.json")
WEIGHTS_PATH = TRANSFER_HEAD_PATH
os.makedirs(RECORDS_DIR, exist_ok=True)


class DashengAudioEngine:
    def __init__(self, config_path=CONFIG_PATH, weights_path=WEIGHTS_PATH, device=None):
        self.config_path = config_path
        self.weights_path = weights_path
        if device is None:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.device = device
        self.model = OfficialDashengAudioModel(weights_path=weights_path, device=device)
        self.sr = 10000
        self.alarm_thresh = 85.0
        self.suspect_threshold = 50.0
        self.reload_config()

    def reload_config(self):
        if os.path.exists(self.config_path):
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                self.alarm_thresh = float(cfg.get("alarm_threshold_percent", 85.0))
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
                "decision_state": "DISCARD (持续削顶方波舍弃)",
                "status": "NORMAL",
                "is_alarm": False,
                "is_suspect": False,
                "collision_confidence_percent": 0.0,
                "background_noise_percent": 100.0,
                "peak_amplitude_percent": round(peak_amp, 2),
                "clipping_rate_percent": round(clip_rep.get("saturation_ratio", 0.0) * 100.0, 2) if clip_rep else 0.0,
                "is_clipped": True,
                "clip_type": clip_rep.get("clip_type", "UNKNOWN") if clip_rep else "UNKNOWN",
                "rail_voltage": clip_rep.get("rail_voltage") if clip_rep else None,
                "inference_time_ms": round(infer_ms, 2),
                "time_sec": round(len(x) / float(sr), 2) if len(x) > 0 else 0.0,
                "plot_path": None,
                "message": clip_rep.get("message", "持续削顶异常丢弃") if clip_rep else "持续削顶异常丢弃"
            }

        if clip_rep and clip_rep.get("is_quiet_normal", False):
            infer_ms = (time.perf_counter() - t0) * 1000.0
            peak_amp = float(np.max(np.abs(x)) * 100.0) if len(x) > 0 else 0.0
            return {
                "name": fn,
                "file_name": fn,
                "decision_state": "NORMAL (稳态平稳底噪快速旁路)",
                "status": "NORMAL",
                "is_alarm": False,
                "is_suspect": False,
                "collision_confidence_percent": 0.0,
                "background_noise_percent": 100.0,
                "peak_amplitude_percent": round(peak_amp, 2),
                "clipping_rate_percent": round(clip_rep.get("saturation_ratio", 0.0) * 100.0, 2) if clip_rep else 0.0,
                "is_clipped": bool(clip_rep.get("is_clipped", False)) if clip_rep else False,
                "clip_type": clip_rep.get("clip_type", "CLEAN") if clip_rep else "CLEAN",
                "rail_voltage": clip_rep.get("rail_voltage") if clip_rep else None,
                "inference_time_ms": round(infer_ms, 2),
                "time_sec": round(len(x) / float(sr), 2) if len(x) > 0 else 0.0,
                "plot_path": None,
                "message": clip_rep.get("quiet_message", "稳态微弱底噪快速旁路")
            }

        # 切片滑动分析
        window_len = int(2.0 * sr)
        hop_len = int(1.0 * sr)
        if len(x) <= window_len:
            pad_len = window_len - len(x)
            slices = [np.pad(x, (0, pad_len), mode="constant")]
            slice_times = [0.0]
        else:
            slices = []
            slice_times = []
            for st in range(0, len(x) - window_len + 1, hop_len):
                slices.append(x[st:st + window_len])
                slice_times.append(st / float(sr))
            if (len(x) - window_len) % hop_len != 0:
                slices.append(x[-window_len:])
                slice_times.append((len(x) - window_len) / float(sr))

        probs = self.model.predict_slices(slices)
        collision_probs = probs[:, 1]
        p_agg, top1, top2 = aggregate_slice_predictions(collision_probs, top1_weight=0.85, top2_weight=0.15)
        conf_pct = float(p_agg * 100.0)

        infer_ms = (time.perf_counter() - t0) * 1000.0
        is_alarm = conf_pct >= self.alarm_thresh
        is_suspect = (conf_pct >= self.suspect_threshold) and not is_alarm
        status = "ALARM" if is_alarm else ("SUSPECT" if is_suspect else "NORMAL")
        decision_state = f"{status} (置信度: {conf_pct:.2f}%)"

        plot_path = None
        if generate_plot and (is_alarm or output_dir == RECORDS_DIR):
            plot_path = self.plot_diagnostic(
                fn=fn,
                x=x,
                sr=sr,
                slices=slices,
                slice_times=slice_times,
                collision_probs=collision_probs,
                conf_pct=conf_pct,
                status=status,
                clip_rep=clip_rep,
                output_dir=output_dir
            )

        peak_amp = float(np.max(np.abs(x)) * 100.0) if len(x) > 0 else 0.0

        return {
            "name": fn,
            "file_name": fn,
            "decision_state": decision_state,
            "status": status,
            "is_alarm": is_alarm,
            "is_suspect": is_suspect,
            "collision_confidence_percent": round(conf_pct, 2),
            "background_noise_percent": round(100.0 - conf_pct, 2),
            "top1_confidence_percent": round(float(top1 * 100.0), 2),
            "top2_confidence_percent": round(float(top2 * 100.0), 2),
            "peak_amplitude_percent": round(peak_amp, 2),
            "clipping_rate_percent": round(clip_rep.get("saturation_ratio", 0.0) * 100.0, 2) if clip_rep else 0.0,
            "is_clipped": bool(clip_rep.get("is_clipped", False)) if clip_rep else False,
            "clip_type": clip_rep.get("clip_type", "CLEAN") if clip_rep else "CLEAN",
            "rail_voltage": clip_rep.get("rail_voltage") if clip_rep else None,
            "inference_time_ms": round(infer_ms, 2),
            "time_sec": round(len(x) / float(sr), 2) if len(x) > 0 else 0.0,
            "plot_path": plot_path
        }

    def plot_diagnostic(self, fn, x, sr, slices, slice_times, collision_probs, conf_pct, status, clip_rep, output_dir):
        fig, axes = plt.subplots(4, 1, figsize=(13, 11), sharex=False)
        fig.suptitle(f"03K Dasheng-Base 自监督通用大模型诊断全景: {fn}\n判定: {status} | 置信度: {conf_pct:.2f}% (门限: {self.alarm_thresh:.1f}%)",
                     fontsize=13, fontweight='bold', color='crimson' if status == 'ALARM' else 'navy')

        # 1. 时域波形
        time_axis = np.linspace(0, len(x) / float(sr), len(x))
        axes[0].plot(time_axis, x, color='#1f77b4', lw=0.8, label="去均值归一化波形")
        if clip_rep and clip_rep.get("rail_voltage") is not None:
            axes[0].axhline(y=clip_rep.get("rail_voltage"), color='r', linestyle='--', alpha=0.6, label=f"正轨电压 ({clip_rep.get('rail_voltage'):.2f}V)")
            axes[0].axhline(y=-clip_rep.get("rail_voltage"), color='r', linestyle='--', alpha=0.6, label=f"负轨电压 (-{clip_rep.get('rail_voltage'):.2f}V)")
        axes[0].set_ylabel("幅值 (V)")
        axes[0].set_title("面板 1: 原始时域连续声学振动波形与动态轨电压监测")
        axes[0].grid(True, linestyle=":", alpha=0.6)
        axes[0].legend(loc="upper right", fontsize=8)

        # 2. 短时傅里叶时频谱
        f, t_spec, Sxx = signal.spectrogram(x, fs=sr, nperseg=512, noverlap=384)
        Sxx_db = 10 * np.log10(np.clip(Sxx, 1e-10, None))
        mesh = axes[1].pcolormesh(t_spec, f, Sxx_db, shading='gouraud', cmap='inferno')
        fig.colorbar(mesh, ax=axes[1], format="%+2.0f dB", pad=0.01)
        axes[1].set_ylabel("频率 (Hz)")
        axes[1].set_title("面板 2: 短时傅里叶宽频声学时频谱 (高频金属碰撞能量分布)")

        # 3. 滑动窗撞击概率轨迹
        axes[2].plot(slice_times, [p * 100.0 for p in collision_probs], marker='o', color='purple', lw=1.5, label="2.0s 滑动切片置信度")
        axes[2].axhline(y=self.alarm_thresh, color='red', linestyle='--', label=f"报警门限 ({self.alarm_thresh:.1f}%)")
        axes[2].axhline(y=self.suspect_threshold, color='orange', linestyle=':', label=f"疑似门限 ({self.suspect_threshold:.1f}%)")
        axes[2].set_ylabel("碰撞概率 (%)")
        axes[2].set_ylim([-5, 105])
        axes[2].set_title("面板 3: 03K Dasheng 模型时序注意力切片置信度演化轨迹")
        axes[2].grid(True, linestyle=":", alpha=0.6)
        axes[2].legend(loc="upper right", fontsize=8)

        # 4. 统计与削顶门禁诊断报告
        axes[3].axis('off')
        clip_desc = f"削顶状态: {'【已拦截削顶】' if clip_rep and clip_rep.get('is_clipped', False) else '正常未削顶'} ({clip_rep.get('clip_type', 'CLEAN') if clip_rep else 'CLEAN'})"
        pile_desc = f"堆积比例: 顶={clip_rep.get('top_pileup_pct', 0.0):.2f}%, 底={clip_rep.get('bottom_pileup_pct', 0.0):.2f}% | 驻留: {clip_rep.get('max_dwell_ms', 0.0):.1f}ms" if clip_rep else ""
        text_info = (
            f"【03K Dasheng 工业级诊断综合指标】\n"
            f"• 最终综合置信度: {conf_pct:.2f}% (85% Top1 + 15% Top2 软加权聚合)\n"
            f"• 切片极值置信度: Top1 = {np.max(collision_probs)*100.0:.2f}%, Top2 = {(np.partition(collision_probs, -2)[-2]*100.0) if len(collision_probs)>1 else 0.0:.2f}%\n"
            f"• {clip_desc} | {pile_desc}\n"
            f"• 物理判定依据: 27.2万小时通用自监督表征 + 时频 (64,4) Patch注意力 + Mean/Max 双通道波脊池化"
        )
        axes[3].text(0.05, 0.5, text_info, fontsize=10, linespacing=1.6,
                     bbox=dict(boxstyle="round,pad=0.6", facecolor='#f0f4f8' if status != 'ALARM' else '#ffebee', edgecolor='#b0bec5'))

        plt.tight_layout()
        out_name = f"plot_{os.path.splitext(fn)[0]}.png"
        out_fp = os.path.join(output_dir, out_name)
        plt.savefig(out_fp, dpi=120)
        plt.close(fig)
        return out_fp
