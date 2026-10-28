# -*- coding: utf-8 -*-
"""
方案 03R：GRAM-Mono 混响自适应音频大模型全流程分析引擎与诊断绘图
"""

import os
import sys
import time
import numpy as np
import scipy.signal as signal
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

plt.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei', 'SimSun', 'DejaVu Sans', 'Arial']
plt.rcParams['axes.unicode_minus'] = False
plt.rcParams['legend.labelcolor'] = '#ffffff'

from .gram_model import OfficialGRAMMonoAudioModel
from common.audio_preprocessor import load_audio_file, preprocess_audio_for_model


class GRAMMonoAudioEngine:
    def __init__(self, weights_path=None, transfer_head_path=None, device=None):
        self.model = OfficialGRAMMonoAudioModel(
            weights_path=weights_path,
            transfer_head_path=transfer_head_path,
            device=device
        )

    def analyze_audio_file(self, file_path, output_dir=None, generate_plot=True, alarm_threshold_pct=90.0):
        t0 = time.perf_counter()
        fn = os.path.basename(file_path)
        res = self.model.predict_audio_file(file_path, alarm_threshold_pct=alarm_threshold_pct)

        if generate_plot and output_dir:
            os.makedirs(output_dir, exist_ok=True)
            plot_path = os.path.join(output_dir, f"{fn}_03r_diagnosis.png")
            x_np, sr = load_audio_file(file_path, target_sr=10000)
            self.generate_diagnostic_plot(
                x_np, sr, res.get("slice_probs", []),
                fn, res["confidence_pct"], res["top1_pct"], res["top2_pct"],
                res["status"], plot_path
            )
            res["plot_path"] = plot_path

        return res

    def generate_diagnostic_plot(self, x, sr, slice_probs, fn, p_agg, top1, top2, state_str, save_path):
        fig, axes = plt.subplots(3, 1, figsize=(14, 8.5), facecolor='#0d1117', gridspec_kw={'height_ratios': [1.1, 1.2, 1.1]})
        fig.suptitle(f"03R GRAM-Mono (arXiv 2025-2026 混响自适应音频大模型) 现场诊断 - {fn}\n综合置信度: {p_agg:.1f}% [Top1: {top1:.1f}%, Top2: {top2:.1f}%] - {state_str}",
                     fontsize=12, fontweight='bold', color='#ffffff', y=0.98)

        # 1. 时域波形
        ax1 = axes[0]
        ax1.set_facecolor('#141923')
        t = np.linspace(0, len(x) / sr, len(x))
        norm_w = x / (np.max(np.abs(x)) + 1e-8) * 100.0
        ax1.plot(t, norm_w, color='#00f0ff', lw=0.6, alpha=0.9)
        ax1.set_xlim(0, len(x) / sr)
        ax1.set_ylim(-105, 105)
        ax1.set_ylabel('振幅 (%)', color='#ffffff', fontsize=9.5)
        ax1.tick_params(colors='#ffffff', labelsize=8.5)
        ax1.grid(True, color='#252d3d', ls=':', alpha=0.7)
        ax1.set_title('【时域波形】', fontsize=10.5, color='#ffffff', pad=5)

        # 2. 时频谱图
        ax2 = axes[1]
        ax2.set_facecolor('#141923')
        f, t_s, Sxx = signal.spectrogram(x, fs=sr, nperseg=int(0.04*sr), noverlap=int(0.02*sr))
        Sxx_dB = 10 * np.log10(Sxx + 1e-9)
        im = ax2.pcolormesh(t_s, f, Sxx_dB, shading='gouraud', cmap='magma', vmin=-70, vmax=0)
        ax2.set_xlim(0, len(x) / sr)
        ax2.set_ylim(0, 5000)
        ax2.set_ylabel('频率 (Hz)', color='#ffffff', fontsize=9.5)
        ax2.tick_params(colors='#ffffff', labelsize=8.5)
        ax2.grid(True, color='#252d3d', ls=':', alpha=0.4)
        ax2.set_title('【时频对数功率谱 (0 ~ 5000 Hz)】', fontsize=10.5, color='#ffffff', pad=5)
        cbar = plt.colorbar(im, ax=ax2, pad=0.015, fraction=0.025)
        cbar.ax.tick_params(colors='#ffffff', labelsize=8)

        # 3. 切片演化曲线
        ax3 = axes[2]
        ax3.set_facecolor('#141923')
        if slice_probs:
            sx = np.arange(len(slice_probs)) * 1.0 + 1.0
            ax3.plot(sx, slice_probs, color='#3b82f6', marker='o', ms=4, lw=1.5, label='切片置信度序列')
            ax3.axhline(90.0, color='#ff3366', ls='--', lw=1.2, label='90.0% 紧急报警线')
            ax3.axhline(50.0, color='#ffaa00', ls=':', lw=1.2, label='50.0% 关注预警线')
            ax3.set_xlim(0, len(x) / sr)
            ax3.set_ylim(0, 105)
            ax3.set_ylabel('置信度 (%)', color='#ffffff', fontsize=9.5)
            ax3.tick_params(colors='#ffffff', labelsize=8.5)
            ax3.grid(True, color='#252d3d', ls=':', alpha=0.7)
            ax3.legend(loc='upper right', facecolor='#0f131a', edgecolor='#252d3d', fontsize=8.5, labelcolor='#ffffff')
            ax3.set_title(f'【切片时序概率演化 (软 Top-2: {p_agg:.1f}%)】', fontsize=10.5, color='#ffffff', pad=5)

        plt.tight_layout()
        plt.savefig(save_path, dpi=120, facecolor=fig.get_facecolor(), edgecolor='none')
        plt.close()
