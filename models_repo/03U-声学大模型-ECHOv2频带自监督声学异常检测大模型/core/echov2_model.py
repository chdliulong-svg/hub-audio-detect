# -*- coding: utf-8 -*-
"""
方案 03U：ECHOv2 (频带自监督声学异常检测大模型，Two-Level Band-Splitting 架构)
针对工业故障异常声音检测 (ASD) 的分频带建模与风电机组轮毂碰撞智能诊断核心模块
(ECHOv2 Band-Splitting Acoustic Foundation Model & Collision Transfer Pipeline)
"""

import os
import sys
import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torchaudio

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(ROOT_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

WEIGHTS_DIR = os.path.join(ROOT_DIR, "weights")
TRANSFER_HEAD_PATH = os.path.join(WEIGHTS_DIR, "echov2_transfer.pt")
ECHO_OFFICIAL_DIR = os.path.join(WEIGHTS_DIR, "echo_official")
if ECHO_OFFICIAL_DIR not in sys.path:
    sys.path.insert(0, ECHO_OFFICIAL_DIR)

from ECHOv2 import from_pretrained
os.makedirs(WEIGHTS_DIR, exist_ok=True)


class BandSplittingModule(nn.Module):
    """
    频带细分模块 (Band-Splitting Module):
    将 128 维对数梅尔谱划分为 8 个独立的物理频带 (每子带 16 维梅尔滤波器):
    子带 0: 0~350Hz (极低频机电运转波)
    子带 1: 350~750Hz (低频齿轮/轴承基频)
    子带 2: 750~1250Hz (结构振动带)
    子带 3: 1250~1800Hz (过渡伴生带)
    子带 4~6: 1800~4200Hz (金属剧烈碰撞高频固有谐振带)
    子带 7: 4200~5000Hz (超高频宽带摩擦衰减带)
    """
    def __init__(self, num_subbands=8, subband_bins=16, subband_dim=96):
        super().__init__()
        self.num_subbands = num_subbands
        self.subband_bins = subband_bins
        self.subband_dim = subband_dim

        # 各子带独立线性投射
        self.projs = nn.ModuleList([
            nn.Sequential(
                nn.Linear(subband_bins, subband_dim),
                nn.LayerNorm(subband_dim),
                nn.GELU()
            ) for _ in range(num_subbands)
        ])

        # 带内时域注意力编码层 (Intra-Band Temporal Attention)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=subband_dim,
            nhead=4,
            dim_feedforward=subband_dim * 4,
            dropout=0.1,
            activation="gelu",
            batch_first=True,
            norm_first=True
        )
        self.intra_band_encoder = nn.TransformerEncoder(encoder_layer, num_layers=2, enable_nested_tensor=False)

    def forward(self, mel_spec):
        # mel_spec: [B, T_frames, 128]
        B, T, F_dim = mel_spec.shape
        subband_feats = []

        for i in range(self.num_subbands):
            sub_x = mel_spec[:, :, i * self.subband_bins : (i + 1) * self.subband_bins]  # [B, T, 16]
            proj_x = self.projs[i](sub_x)  # [B, T, 96]
            intra_x = self.intra_band_encoder(proj_x)  # [B, T, 96]
            subband_feats.append(intra_x)

        # 8 个频段拼接为 [B, T, 8 * 96 = 768]
        cross_band_input = torch.cat(subband_feats, dim=-1)  # [B, T, 768]
        return cross_band_input


class ECHOv2Backbone(nn.Module):
    """
    ECHOv2 双层频带解耦骨干网络:
    Band-Splitting 频带投射 + 12 层跨频带时空交互 Transformer (768 维, 12 头)
    """
    def __init__(self, embed_dim=768, depth=12, num_heads=12, max_len=256):
        super().__init__()
        self.band_splitter = BandSplittingModule(num_subbands=8, subband_bins=16, subband_dim=96)
        self.pos_embed = nn.Parameter(torch.zeros(1, max_len, embed_dim))

        encoder_layer = nn.TransformerEncoderLayer(
            d_model=embed_dim,
            nhead=num_heads,
            dim_feedforward=int(embed_dim * 4),
            dropout=0.1,
            activation="gelu",
            batch_first=True,
            norm_first=True
        )
        self.cross_band_transformer = nn.TransformerEncoder(encoder_layer, num_layers=depth, enable_nested_tensor=False)
        self.norm = nn.LayerNorm(embed_dim)

        self._init_weights()

    def _init_weights(self):
        nn.init.trunc_normal_(self.pos_embed, std=0.02)
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.trunc_normal_(m.weight, std=0.02)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)
            elif isinstance(m, nn.LayerNorm):
                nn.init.constant_(m.bias, 0)
                nn.init.constant_(m.weight, 1.0)

    def forward(self, mel_spec):
        # mel_spec: [B, T_frames, 128]
        feat = self.band_splitter(mel_spec)  # [B, T, 768]
        B, T, D = feat.shape
        pos = self.pos_embed[:, :T, :]
        feat = feat + pos
        out = self.cross_band_transformer(feat)  # [B, T, 768]
        out = self.norm(out)
        return out


class ECHOv2CollisionHead(nn.Module):
    """
    1536-D 跨频带波脊特征工业分类头:
    融合 ECHOv2 全局声学表征 (768-D, Utterance Mean) + 跨带峰值表征 (768-D, 捕捉金属高能共振尖峰)
    """
    def __init__(self, in_features=1536, hidden_dim=384, num_classes=2, dropout=0.2):
        super().__init__()
        self.classifier = nn.Sequential(
            nn.Linear(in_features, hidden_dim),
            nn.LayerNorm(hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, 96),
            nn.LayerNorm(96),
            nn.GELU(),
            nn.Dropout(dropout * 0.5),
            nn.Linear(96, num_classes)
        )

    def forward(self, x):
        return self.classifier(x)


class OfficialECHOv2AudioModel(nn.Module):
    """
    03U 方案：ECHOv2 频带自监督声学异常检测大模型骨干网络 + 碰撞检测分类头集成模型
    """
    def __init__(self, weights_path=TRANSFER_HEAD_PATH, device=None):
        super().__init__()
        if device is None:
            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.device = device
        self.norm_mean = -4.268
        self.norm_std = 4.569
        self.target_length = 192  # 192 frames @ 10ms frame shift
        self.temperature = 1.0
        self.sample_rate = 16000

        # 加载官方自监督预训练 ECHOv2 骨干网络 (82MB 预训练 safetensors)
        ckpt_path = os.path.join(WEIGHTS_DIR, "model.safetensors")
        if os.path.exists(ckpt_path):
            self.backbone = from_pretrained(ckpt_path).to(self.device)
            print(f"[03U ECHOv2] 成功加载官方自监督预训练骨干网络: {ckpt_path}")
        else:
            self.backbone = from_pretrained().to(self.device)
        self.backbone.eval()
        for p in self.backbone.parameters():
            p.requires_grad = False

        # 分类 Head (1536 -> 2)
        self.head = ECHOv2CollisionHead(in_features=1536, num_classes=2).to(self.device)
        target_weights = weights_path if weights_path else TRANSFER_HEAD_PATH
        if target_weights and os.path.exists(target_weights):
            ckpt = torch.load(target_weights, map_location=self.device, weights_only=True)
            if "head_state" in ckpt:
                self.head.load_state_dict(ckpt["head_state"])
            elif "head" in ckpt:
                self.head.load_state_dict(ckpt["head"])
            else:
                self.head.load_state_dict(ckpt)
            print(f"[03U ECHOv2] 成功加载分类头权重: {target_weights}")
        else:
            print(f"[!] 提示: 分类头权重未找到: {target_weights} (使用初始化权重)")

        self.head.eval()

    def extract_features(self, waveform, orig_sr=10000):
        """
        利用官方 ECHOv2 频带自监督大模型提取 1536-D 声学表征向量
        """
        if isinstance(waveform, np.ndarray):
            waveform = torch.from_numpy(waveform).float()
        if waveform.dim() == 1:
            waveform = waveform.unsqueeze(0)

        feats = []
        with torch.no_grad():
            for i in range(waveform.shape[0]):
                wav_1d = waveform[i].cpu()
                utt_feat, _ = self.backbone.extract_features_from_audio(wav_1d, sample_rate=orig_sr)
                feats.append(utt_feat.to(self.device))
        return torch.stack(feats, dim=0)  # [B, 1536]

    def forward(self, waveform, orig_sr=10000):
        features = self.extract_features(waveform, orig_sr=orig_sr)
        logits = self.head(features)
        scaled_logits = logits / max(self.temperature, 1e-4)
        probs = F.softmax(scaled_logits, dim=-1)
        return {
            "logits": logits,
            "probabilities": probs,
            "features": features
        }

    def predict(self, audio, orig_sr=10000, threshold_pct=None):
        self.eval()
        with torch.no_grad():
            res = self.forward(audio, orig_sr=orig_sr)
            probs = res["probabilities"][0].cpu().numpy()
            p_normal = float(probs[0] * 100.0)
            p_collision = float(probs[1] * 100.0)

        thresh = threshold_pct if threshold_pct is not None else 90.0
        suspect_thresh = 50.0

        if p_collision >= thresh:
            state = "ALARM"
        elif p_collision >= suspect_thresh:
            state = "SUSPECT"
        else:
            state = "NORMAL"

        return {
            "decision_state": state,
            "collision_confidence_pct": round(p_collision, 2),
            "probabilities": {
                "background_noise_pct": round(p_normal, 2),
                "collision_probability_pct": round(p_collision, 2)
            },
            "features": res["features"][0].cpu().numpy(),
            "is_suspect": state == "SUSPECT",
            "is_alarm": state == "ALARM"
        }

    def predict_slices(self, slices_10k):
        """
        对一组 10kHz 2.0s 切片进行批处理推理
        slices_10k: list of np.ndarray 或 torch.Tensor (长度 20000)
        返回: np.ndarray [B, 2] (类别 0: 噪声, 类别 1: 撞击)
        """
        if len(slices_10k) == 0:
            return np.zeros((0, 2), dtype=np.float32)

        feats = []
        with torch.no_grad():
            for sl in slices_10k:
                if isinstance(sl, np.ndarray):
                    wav_1d = torch.from_numpy(sl).float()
                else:
                    wav_1d = sl.cpu().float()
                utt_feat, _ = self.backbone.extract_features_from_audio(wav_1d, sample_rate=10000)
                feats.append(utt_feat.to(self.device))
            batch_feats = torch.stack(feats, dim=0)  # [B, 1536]
            logits = self.head(batch_feats) / max(self.temperature, 1e-4)
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
        collision_probs = probs[:, 1]
        from common.audio_preprocessor import aggregate_slice_predictions
        p_agg, top1, top2 = aggregate_slice_predictions(collision_probs * 100.0, top1_weight=0.85, top2_weight=0.15)

        return {
            "confidence_pct": p_agg,
            "top1_pct": top1,
            "top2_pct": top2,
            "num_slices": len(slices),
            "slice_probs": [round(float(p * 100.0), 2) for p in collision_probs]
        }
