# -*- coding: utf-8 -*-
"""
方案 03R：GRAM-Mono (Radboud Univ / arXiv 2025-2026) 混响自适应通用音频大模型
【12层 ViT-Base + MW-MAE 多尺度局部/全局时空窗口掩码 + RIR 混响自适应 + 1536-D 双时空池化头 + 85/15 软Top2聚合】
"""

import os
import sys
import time
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchaudio
import safetensors.torch

CORE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(CORE_DIR)
PROJECT_ROOT = os.path.dirname(ROOT_DIR)
WEIGHTS_DIR = os.path.join(ROOT_DIR, "weights")
GRAM_HF_DIR = os.path.join(CORE_DIR, "gram_hf")

BACKBONE_WEIGHTS_PATH = os.path.join(WEIGHTS_DIR, "model.safetensors")
TRANSFER_HEAD_PATH = os.path.join(WEIGHTS_DIR, "gram_mono_transfer.pt")

if CORE_DIR not in sys.path:
    sys.path.insert(0, CORE_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from gram_hf.configuration_gramt_mono import GRAMTMonoConfig
from gram_hf.modeling_gramt_mono import GRAMTMonoModel
from common.audio_preprocessor import (
    preprocess_audio_for_model,
    aggregate_slice_predictions,
    load_audio_file
)


class GRAMMonoCollisionHead(nn.Module):
    """
    针对风机金属空腔长混响与瞬态撞击的 GRAM-Mono 专用双路时空池化头
    输入: 1536-D (768-D Mean Pooling 稳态混响能量基底 + 768-D Max Pooling 瞬态撞击波脊)
    """
    def __init__(self, in_dim=1536, hidden_dim=256, num_classes=2, dropout=0.25):
        super().__init__()
        self.norm = nn.LayerNorm(in_dim)
        self.fc1 = nn.Linear(in_dim, hidden_dim)
        self.act = nn.GELU()
        self.drop = nn.Dropout(dropout)
        self.fc2 = nn.Linear(hidden_dim, num_classes)

    def forward(self, x):
        x = self.norm(x)
        x = self.fc1(x)
        x = self.act(x)
        x = self.drop(x)
        return self.fc2(x)


class OfficialGRAMMonoAudioModel(nn.Module):
    """
    GRAM-Mono 官方自监督骨干网络封装与风电轮毂碰撞诊断引擎
    """
    def __init__(self, weights_path=None, transfer_head_path=None, device=None):
        super().__init__()
        self.device = device or torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.weights_path = weights_path or BACKBONE_WEIGHTS_PATH
        self.transfer_head_path = transfer_head_path or TRANSFER_HEAD_PATH

        # 1. 加载配置与骨干网络
        config_path = os.path.join(GRAM_HF_DIR, "config.json")
        self.config = GRAMTMonoConfig.from_json_file(config_path)
        self.backbone = GRAMTMonoModel(self.config)

        if os.path.exists(self.weights_path):
            state_dict = safetensors.torch.load_file(self.weights_path)
            self.backbone.load_state_dict(state_dict, strict=False)
            print(f"[03R GRAM-Mono] 成功加载官方自监督骨干权重: {self.weights_path}")
        else:
            print(f"[!] 警告: 未找到官方骨干权重: {self.weights_path}")

        self.backbone.to(self.device)
        self.backbone.eval()

        # 2. 前处理算子: 10kHz -> 32kHz Sinc重采样 + 128通道 Mel 谱图提取
        self.resampler_10k_to_32k = torchaudio.transforms.Resample(10000, 32000).to(self.device)
        self.mel_trans = torchaudio.transforms.MelSpectrogram(
            sample_rate=32000,
            n_fft=1024,
            win_length=1024,
            hop_length=320,
            f_min=50,
            f_max=16000,
            n_mels=128,
            power=2.0
        ).to(self.device)

        # 3. 碰撞二分类 Transfer Head (1536 -> 256 -> 2)
        self.head = GRAMMonoCollisionHead(in_dim=1536, hidden_dim=256, num_classes=2, dropout=0.25).to(self.device)
        if os.path.exists(self.transfer_head_path):
            ckpt = torch.load(self.transfer_head_path, map_location=self.device)
            self.head.load_state_dict(ckpt["head_state_dict"] if "head_state_dict" in ckpt else ckpt)
            print(f"[03R GRAM-Mono] 成功加载微调分类头权重: {self.transfer_head_path}")
        else:
            print(f"[!] 提示: 尚未微调分类头 (权重路径: {self.transfer_head_path})")

        self.temperature = 1.0

    def forward_features(self, batch_10k):
        """
        batch_10k: [B, 20000] (2.0s 切片 @ 10kHz)
        返回: [B, 1536]
        """
        B = batch_10k.shape[0]
        # 上采样至 32kHz (64000点)
        batch_32k = self.resampler_10k_to_32k(batch_10k)
        # 提取 Log-Mel
        mel = self.mel_trans(batch_32k) # [B, 128, 201]
        log_mel = torch.log(mel + 1e-6)
        # 对齐至 200 帧并排列为 [B, 1, 200, 128]
        feat_in = log_mel.transpose(1, 2)[:, :200, :].unsqueeze(1)

        # 经过 GRAM 骨干网络
        x = feat_in.transpose(2, 3) # [B, 1, 128, 200]
        x = self.backbone.model.spectrogram_normalize(x)
        encoded_patches = self.backbone.model.patch_strategy.embed(x, self.backbone.model.patch_embed)
        mask = torch.zeros((B, self.backbone.model.num_patches), dtype=torch.bool, device=self.device)
        tokens = self.backbone.model.pass_through_encoder(encoded_patches, ~mask, B)

        # 双路时空波脊池化
        patch_tokens = tokens[:, 1:, :] # [B, 200, 768]
        mean_p = patch_tokens.mean(dim=1)
        max_p = patch_tokens.max(dim=1)[0]
        comb_feat = torch.cat([mean_p, max_p], dim=-1) # [B, 1536]
        return comb_feat

    def predict_slices(self, slices_list, batch_size=16):
        """
        批量切片推理
        """
        self.head.eval()
        all_probs = []

        for st in range(0, len(slices_list), batch_size):
            batch_np = slices_list[st:st + batch_size]
            batch_tensor = torch.tensor(np.array(batch_np), dtype=torch.float32, device=self.device)
            with torch.no_grad():
                feats = self.forward_features(batch_tensor)
                logits = self.head(feats) / self.temperature
                probs = F.softmax(logits, dim=-1).cpu().numpy()
            all_probs.append(probs)

        return np.concatenate(all_probs, axis=0) if all_probs else np.zeros((0, 2))

    def predict_preprocessed_waveform(self, x_np, sr=10000, window_sec=2.0, hop_sec=1.0):
        """
        标准 2.0s 窗长 + 1.0s 步长 (50% 重叠) + 85/15 软 Top-2 聚合
        """
        window_len = int(window_sec * sr)
        hop_len = int(hop_sec * sr)

        if len(x_np) <= window_len:
            pad_len = window_len - len(x_np)
            sl = np.pad(x_np, (0, pad_len), mode="constant")
            slices = [sl]
        else:
            slices = []
            for st in range(0, len(x_np) - window_len + 1, hop_len):
                slices.append(x_np[st:st + window_len])
            if (len(x_np) - window_len) % hop_len != 0:
                slices.append(x_np[-window_len:])

        probs = self.predict_slices(slices)
        collision_probs = probs[:, 1]
        p_agg, top1, top2 = aggregate_slice_predictions(collision_probs, top1_weight=0.85, top2_weight=0.15)

        return {
            "confidence_pct": round(float(p_agg * 100.0), 2),
            "top1_pct": round(float(top1 * 100.0), 2),
            "top2_pct": round(float(top2 * 100.0), 2),
            "num_slices": len(slices),
            "slice_probs": [round(float(p * 100.0), 2) for p in collision_probs]
        }

    def predict_audio_file(self, audio_path, alarm_threshold_pct=90.0):
        t0 = time.perf_counter()
        filename = os.path.basename(audio_path)
        should_discard, clip_rep, x_np = preprocess_audio_for_model(audio_path, target_sr=10000)

        if should_discard:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            return {
                "device": "172.16.80.10", "filename": filename, "status": "DISCARD",
                "confidence_pct": 0.0, "top1_pct": 0.0, "top2_pct": 0.0,
                "is_alarm": 0, "is_clipped": 1, "clip_type": clip_rep.get("clip_type", "UNKNOWN"),
                "latency_ms": latency_ms, "file_path": audio_path
            }

        res = self.predict_preprocessed_waveform(x_np, sr=10000, window_sec=2.0, hop_sec=1.0)
        conf = res["confidence_pct"]
        is_alarm = 1 if conf >= alarm_threshold_pct else 0
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)

        return {
            "device": "172.16.80.10", "filename": filename,
            "status": "ALARM" if is_alarm else "NORMAL",
            "confidence_pct": conf, "top1_pct": res["top1_pct"], "top2_pct": res["top2_pct"],
            "is_alarm": is_alarm, "is_clipped": int(clip_rep.get("is_clipped", False)),
            "clip_type": clip_rep.get("clip_type", "CLEAN"),
            "peak_amp_pct": round(float(np.max(np.abs(x_np))) * 100.0, 2) if len(x_np) > 0 else 0.0,
            "duration_sec": round(len(x_np) / 10000.0, 2) if len(x_np) > 0 else 0.0,
            "latency_ms": latency_ms, "num_slices": res["num_slices"],
            "slice_probs": res["slice_probs"], "file_path": audio_path
        }
