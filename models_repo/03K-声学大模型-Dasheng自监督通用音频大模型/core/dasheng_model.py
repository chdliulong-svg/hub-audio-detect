# -*- coding: utf-8 -*-
"""
方案 03K：Dasheng-Base 自监督通用音频大模型封装与二分类迁移推理引擎
【27.2万小时海量预训练 + 时频 Patch 化 (64, 4) + 时空双路池化头 (1536-D) + 85/15 软聚合】
"""

import os
import sys
import json
import time
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchaudio
from safetensors.torch import load_file

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(ROOT_DIR)
WEIGHTS_DIR = os.path.join(ROOT_DIR, "weights")
SAFETENSORS_PATH = os.path.join(WEIGHTS_DIR, "model.safetensors")
TRANSFER_HEAD_PATH = os.path.join(WEIGHTS_DIR, "dasheng_official_transfer.pt")
NATIVE_FINETUNED_PATH = os.path.join(WEIGHTS_DIR, "03k_native_finetuned.pt")

if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.dasheng.configuration_dasheng import DashengConfig
from core.dasheng.modeling_dasheng import DashengModel
from common.audio_preprocessor import (
    preprocess_audio_for_model,
    aggregate_slice_predictions,
    load_audio_file
)


class DashengCollisionHead(nn.Module):
    """
    针对风电金属撞击与结构噪声的 Dasheng 专用双路时空波脊池化分类头
    骨干输出: [Batch, Time_tokens, 768] (每个 Token 代表 40ms)
    双路池化: Mean Pooling (捕捉稳态工况底噪) + Max Pooling (捕捉瞬态高能撞击尖峰)
    融合特征: [Batch, 1536]
    """
    def __init__(self, in_dim=768, num_classes=2, dropout=0.2):
        super().__init__()
        self.fc1 = nn.Linear(in_dim * 2, 256)
        self.act = nn.GELU()
        self.dropout = nn.Dropout(dropout)
        self.fc2 = nn.Linear(256, num_classes)

    def forward(self, x):
        # x can be tokens [B, T, 768] or pre-pooled features [B, 1536]
        if x.dim() == 3:
            mean_pool = x.mean(dim=1)
            max_pool, _ = x.max(dim=1)
            feat = torch.cat([mean_pool, max_pool], dim=-1)
        else:
            feat = x
        h = self.fc1(feat)
        h = self.act(h)
        h = self.dropout(h)
        logits = self.fc2(h)
        return logits


class OfficialDashengAudioModel:
    """
    03K 方案：官方 Dasheng-Base 模型与碰撞分类迁移流水线
    """
    def __init__(self, weights_path=None, device=None):
        if device is None:
            self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        elif isinstance(device, str):
            self.device = torch.device(device)
        else:
            self.device = device

        self.temperature = 1.0  # 物理自然标定温度
        self.sample_rate = 16000

        # 1. 加载配置
        config_path = os.path.join(ROOT_DIR, "core", "dasheng", "config.json")
        with open(config_path, "r", encoding="utf-8") as f:
            cfg_dict = json.load(f)
        self.config = DashengConfig(**cfg_dict)

        # 2. 构建 Dasheng 骨干模型
        self.backbone = DashengModel(self.config).to(self.device)

        # 3. 加载预训练 safetensors 权重
        if os.path.exists(SAFETENSORS_PATH):
            state_dict = load_file(SAFETENSORS_PATH)
            # 过滤 head 权重，仅载入骨干
            encoder_dict = {k: v for k, v in state_dict.items() if not k.startswith("outputlayer")}
            missing, unexpected = self.backbone.load_state_dict(encoder_dict, strict=False)
            print(f"[03K Dasheng] 成功加载预训练骨干: {SAFETENSORS_PATH}")
        else:
            print(f"[!] 警告: 未找到预训练权重 {SAFETENSORS_PATH}，使用随机初始化")

        # 4. 构建分类 Head
        self.head = DashengCollisionHead(in_dim=768, num_classes=2).to(self.device)

        # 5. 加载微调权重（如果存在）
        target_weights = weights_path if weights_path else TRANSFER_HEAD_PATH
        if target_weights and os.path.exists(target_weights):
            head_state = torch.load(target_weights, map_location=self.device, weights_only=True)
            if "head" in head_state:
                self.head.load_state_dict(head_state["head"])
            else:
                self.head.load_state_dict(head_state)
            print(f"[03K Dasheng] 成功加载分类头权重: {target_weights}")

        self.backbone.eval()
        self.head.eval()

        # 6. GPU 特征提取流水线 (Kaldi/Torchaudio 规范 Mel + dB 转换)
        self.mel_extractor = torchaudio.transforms.MelSpectrogram(
            sample_rate=16000,
            n_fft=512,
            win_length=512,
            hop_length=160,
            f_min=0,
            f_max=8000,
            n_mels=64,
            center=True,
            power=2.0
        ).to(self.device)
        self.amplitude_to_db = torchaudio.transforms.AmplitudeToDB(top_db=120).to(self.device)

        # 10k -> 16k 重采样器
        self.resampler_10k_to_16k = torchaudio.transforms.Resample(10000, 16000).to(self.device)

    def eval(self):
        self.backbone.eval()
        self.head.eval()
        return self

    def train(self, mode=True):
        self.head.train(mode)
        return self

    def extract_mel_gpu(self, waveform_16k):
        """
        在 GPU 上将 16kHz 音频波形转换为 64-mel dB 谱图
        waveform_16k: [B, num_samples]
        返回: [B, 64, T_frames]
        """
        if waveform_16k.dim() == 1:
            waveform_16k = waveform_16k.unsqueeze(0)
        mel = self.mel_extractor(waveform_16k)
        mel_db = self.amplitude_to_db(mel)
        return mel_db

    def forward_features(self, waveform_16k):
        """
        从波形提取 Dasheng 骨干序列 Token
        waveform_16k: [B, num_samples]
        返回: [B, T_tokens, 768]
        """
        mel_db = self.extract_mel_gpu(waveform_16k)  # [B, 64, T]
        x = torch.unsqueeze(mel_db, 1)               # [B, 1, 64, T]
        hidden_states = self.backbone.encoder(x)     # [B, T_tokens, 768]
        return hidden_states

    def predict_slices(self, slices_10k):
        """
        对一组 10kHz 2.0s 切片进行批处理推理
        slices_10k: list of np.ndarray 或 torch.Tensor (长度 20000)
        返回: np.ndarray [B, 2] (类别 0: 噪声, 类别 1: 撞击)
        """
        if len(slices_10k) == 0:
            return np.zeros((0, 2), dtype=np.float32)

        if isinstance(slices_10k[0], np.ndarray):
            tensors_10k = [torch.from_numpy(s).float() for s in slices_10k]
        else:
            tensors_10k = slices_10k

        batch_10k = torch.stack(tensors_10k, dim=0).to(self.device)
        batch_16k = self.resampler_10k_to_16k(batch_10k)  # [B, 32000]

        with torch.no_grad():
            hidden = self.forward_features(batch_16k)
            logits = self.head(hidden) / self.temperature
            probs = F.softmax(logits, dim=-1).cpu().numpy()

        return probs

    def predict_preprocessed_waveform(self, x_np, sr=10000, window_sec=2.0, hop_sec=1.0):
        """
        对已完成前处理去均值与增益归一化的单通道波形进行多切片滑动推理与 85/15 软加权聚合
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
        collision_probs = probs[:, 1]  # 碰撞类别概率
        p_agg, top1, top2 = aggregate_slice_predictions(collision_probs, top1_weight=0.85, top2_weight=0.15)

        return {
            "confidence_pct": round(float(p_agg * 100.0), 2),
            "top1_pct": round(float(top1 * 100.0), 2),
            "top2_pct": round(float(top2 * 100.0), 2),
            "num_slices": len(slices),
            "slice_probs": [round(float(p * 100.0), 2) for p in collision_probs]
        }

    def predict_audio_file(self, audio_path, alarm_threshold_pct=85.0):
        """
        端到端全流程诊断接口（含工业前处理门禁）
        """
        t0 = time.perf_counter()
        filename = os.path.basename(audio_path)

        should_discard, clip_rep, x_np = preprocess_audio_for_model(audio_path, target_sr=10000)

        # 削顶方波拦截舍弃
        if should_discard:
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            return {
                "filename": filename,
                "status": "DISCARD",
                "is_alarm": 0,
                "confidence_pct": 0.0,
                "collision_confidence_pct": 0.0,
                "background_noise_pct": 100.0,
                "latency_ms": latency_ms,
                "is_clipped": 1,
                "clip_type": clip_rep.get("clip_type", "UNKNOWN") if clip_rep else "UNKNOWN",
                "message": clip_rep.get("message", "削顶异常舍弃") if clip_rep else "削顶异常舍弃"
            }

        # 稳态极低能量快速旁路
        if clip_rep and clip_rep.get("is_quiet_normal", False):
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)
            return {
                "filename": filename,
                "status": "NORMAL",
                "is_alarm": 0,
                "confidence_pct": 0.0,
                "collision_confidence_pct": 0.0,
                "background_noise_pct": 100.0,
                "latency_ms": latency_ms,
                "is_clipped": int(clip_rep.get("is_clipped", False)),
                "clip_type": clip_rep.get("clip_type", "CLEAN"),
                "message": clip_rep.get("quiet_message", "稳态微弱底噪快速旁路")
            }

        # 模型多切片推理
        res = self.predict_preprocessed_waveform(x_np, sr=10000)
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        conf_pct = res["confidence_pct"]
        is_alarm = int(conf_pct >= alarm_threshold_pct)
        status = "ALARM" if is_alarm else "NORMAL"

        return {
            "filename": filename,
            "status": status,
            "is_alarm": is_alarm,
            "confidence_pct": conf_pct,
            "collision_confidence_pct": conf_pct,
            "top1_confidence_pct": res["top1_pct"],
            "top2_confidence_pct": res["top2_pct"],
            "background_noise_pct": round(100.0 - conf_pct, 2),
            "latency_ms": latency_ms,
            "is_clipped": int(clip_rep.get("is_clipped", False)) if clip_rep else 0,
            "clip_type": clip_rep.get("clip_type", "CLEAN") if clip_rep else "CLEAN",
            "peak_amp_pct": round(float(np.max(np.abs(x_np))) * 100.0, 2) if len(x_np) > 0 else 0.0,
            "num_slices": res["num_slices"]
        }
