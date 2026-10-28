# -*- coding: utf-8 -*-
"""
通用音频前处理与持续削顶信号拦截器 (Common Audio Preprocessor & Persistent Clipping Detector)
存放路径: common/audio_preprocessor.py

核心功能：
  1. 统一标准工业音频加载器 (支持大端 10K >i2 .bin 与标准 .wav 格式)
  2. 独立持续削顶检测与舍弃机制 (Persistent Clipping Detector):
     - 针对现场传感器故障、线路满量程死锁引发的严重持续性方波削顶 (如 SY20260818_093525.bin)；
     - 在进入任何神经网络前自动完成前处理检验，若确认持续削顶则直接先予舍弃，不送入模型推断。
"""

import os
import wave
import numpy as np
from scipy import signal, stats


def load_audio_file(file_path: str, target_sr: int = 10000):
    """
    全格式工业级音频加载器：
    - .wav: 支持所有位深 (16-bit PCM, 24-bit PCM, 32-bit Float/PCM, 64-bit Float, 8-bit PCM, A-law, μ-law, Extensible)，
            支持单声道、立体声及多声道自动无损混缩为单声道；
            支持任意输入采样率 (8kHz, 16kHz, 22.05kHz, 44.1kHz, 48kHz, 96kHz 等)，使用高质量抗混叠重采样至 target_sr；
    - .bin: 现场工业大端格式 (>i2, 16-bit Signed Integer), 默认 10,000 Hz, 支持端序智能自适应容错；
    :param file_path: 音频文件路径
    :param target_sr: 目标采样率 (默认 10000 Hz)
    :return: (audio_np, target_sr) 单声道 float32 数组 [-1.0, 1.0] 与统一采样率
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"音频文件不存在: {file_path}")

    ext = os.path.splitext(file_path)[1].lower()
    audio = None
    orig_sr = target_sr

    if ext == ".wav":
        # 优先使用工业级 soundfile 加载 (支持所有标准与扩展 WAV 格式，包括 32-bit float、Extensible、24-bit 等)
        try:
            import soundfile as sf
            audio_data, sr_read = sf.read(file_path, dtype="float32", always_2d=False)
            orig_sr = int(sr_read)
            if audio_data.ndim > 1:
                # 多声道自动混缩为单声道均值
                audio = audio_data.mean(axis=1).astype(np.float32)
            else:
                audio = audio_data.astype(np.float32)
        except Exception:
            pass

        # 第二道防线：scipy.io.wavfile 回退
        if audio is None:
            try:
                import scipy.io.wavfile as sc_wav
                sr_read, raw_data = sc_wav.read(file_path)
                orig_sr = int(sr_read)
                if raw_data.ndim > 1:
                    raw_data = raw_data.mean(axis=1)
                if raw_data.dtype == np.int16:
                    audio = (raw_data / 32768.0).astype(np.float32)
                elif raw_data.dtype == np.int32:
                    audio = (raw_data / 2147483648.0).astype(np.float32)
                elif raw_data.dtype == np.uint8:
                    audio = ((raw_data.astype(np.float32) - 128.0) / 128.0).astype(np.float32)
                elif np.issubdtype(raw_data.dtype, np.floating):
                    audio = raw_data.astype(np.float32)
                else:
                    audio = (raw_data / (float(np.iinfo(raw_data.dtype).max) + 1.0)).astype(np.float32)
            except Exception:
                pass

        # 第三道防线：torchaudio 回退
        if audio is None:
            try:
                import torchaudio
                sig, sr_read = torchaudio.load(file_path)
                orig_sr = int(sr_read)
                if sig.shape[0] > 1:
                    sig = sig.mean(dim=0, keepdim=False)
                else:
                    sig = sig.squeeze(0)
                audio = sig.numpy().astype(np.float32)
            except Exception:
                pass

        # 第四道保底：Python 标准 wave 模块
        if audio is None:
            with wave.open(file_path, "rb") as wf:
                orig_sr = wf.getframerate()
                n_ch = wf.getnchannels()
                sampwidth = wf.getsampwidth()
                n_frames = wf.getnframes()
                raw_bytes = wf.readframes(n_frames)

                if sampwidth == 2:
                    audio = np.frombuffer(raw_bytes, dtype="<i2").astype(np.float32) / 32768.0
                elif sampwidth == 3:
                    rb = np.frombuffer(raw_bytes, dtype=np.uint8)
                    n_samples = len(rb) // 3
                    rb = rb[:n_samples * 3].reshape(-1, 3)
                    b0 = rb[:, 0].astype(np.int32)
                    b1 = rb[:, 1].astype(np.int32) << 8
                    b2 = rb[:, 2].astype(np.int32)
                    b2 = np.where(b2 >= 128, b2 - 256, b2) << 16
                    audio = ((b0 | b1 | b2) / 8388608.0).astype(np.float32)
                elif sampwidth == 4:
                    audio = np.frombuffer(raw_bytes, dtype=np.float32)
                elif sampwidth == 1:
                    audio = (np.frombuffer(raw_bytes, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
                else:
                    audio = np.frombuffer(raw_bytes, dtype="<i2").astype(np.float32) / 32768.0

                if n_ch > 1 and len(audio) >= n_ch:
                    audio = audio[:len(audio) - (len(audio) % n_ch)]
                    audio = audio.reshape(-1, n_ch).mean(axis=1).astype(np.float32)

    else:
        # .bin 纯二进制采样点 (现场默认 10k 大端)
        orig_sr = target_sr
        with open(file_path, "rb") as f:
            raw_data = f.read()
        valid_len = len(raw_data) - (len(raw_data) % 2)
        if valid_len == 0:
            audio = np.zeros(0, dtype=np.float32)
        else:
            sig_be = np.frombuffer(raw_data[:valid_len], dtype=">i2")
            if len(sig_be) > 100 and np.std(sig_be) < 1.0:
                sig_le = np.frombuffer(raw_data[:valid_len], dtype="<i2")
                if np.std(sig_le) >= 1.0:
                    audio = sig_le.astype(np.float32) / 32768.0
                else:
                    audio = sig_be.astype(np.float32) / 32768.0
            else:
                audio = sig_be.astype(np.float32) / 32768.0

    # 规范化幅值范围 (防极端 NaN/Inf 或溢出)
    if audio is None or len(audio) == 0:
        return np.zeros(0, dtype=np.float32), target_sr
    audio = np.nan_to_num(audio, nan=0.0, posinf=1.0, neginf=-1.0).astype(np.float32)

    # 工业自适应重采样准则：只降不升 (Downsample Only, No Upsampling)
    # 1. 凡是 <= target_sr (如 8000Hz, 8192Hz, 10000Hz) 的工业音频，坚决不执行插值升采样拉伸！
    #    彻底规避 Sinc/多相重采样引发的高频共振峰频移失真，100% 保留原生物理冲击波形。
    # 2. 仅当输入采样率明显高于 target_sr (如 16kHz, 22.05kHz, 44.1kHz, 48kHz, 96kHz) 时，执行高质量抗混叠降采样。
    if target_sr is not None and orig_sr is not None and orig_sr > target_sr and len(audio) > 0:
        resampled = False
        try:
            import torchaudio
            import torch
            resampler = torchaudio.transforms.Resample(orig_freq=orig_sr, new_freq=target_sr)
            audio = resampler(torch.from_numpy(audio)).numpy().astype(np.float32)
            resampled = True
        except Exception:
            pass

        if not resampled:
            gcd = np.gcd(int(orig_sr), int(target_sr))
            up = int(target_sr // gcd)
            down = int(orig_sr // gcd)
            if up * down <= 50000000:
                audio = signal.resample_poly(audio, up, down).astype(np.float32)
            else:
                target_len = int(len(audio) * target_sr / orig_sr)
                audio = signal.resample(audio, target_len).astype(np.float32)

    return audio, target_sr


def detect_clipping_level(
    audio: np.ndarray,
    sr: int = 10000,
    flat_thresh: float = 0.95,
    flat_diff_tol: float = 1e-4,
    full_scale: float = 0.98,
    clip_flat_ratio: float = 0.10,
    min_consec_flat_ms: float = 35.0,
) -> dict:
    """
    统一硬件饱和与削顶评级引擎 (Unified Saturation & Clipping Level Evaluator):
    全系统削顶与硬件饱和检测的唯一底层核心实现，融合：
      1. 自适应模拟供电轨与硬件嵌位检测 (内部集成 _detect_analog_rail_clipping)
      2. 持续性方波硬件死锁与长时间平顶拦截 (避免传感器/线路故障引发高频谐波虚警)
      3. 瞬态强撞击幅值饱和保全机制 (严格区分「持续死锁」与「真实大撞击触顶」，杜绝误杀重大事故)
      4. 统计偏度 (Skewness) 与多级软饱和健康度评级 (NONE / MILD / SOFT / SEVERE)

    分级判定准则：
      - clip (持续死锁故障):
        饱和点占比 >= clip_flat_ratio 或 连续死锁平顶时长 >= min_consec_flat_ms 或 模拟轨嵌位超标
        → should_discard = True, decision = "DISCARD"，在进入深度网络前直接阻断舍弃。
      - transient_clip (瞬态碰撞饱和):
        峰值达到满量程 (>= full_scale)，但未形成持续死锁，属于强冲击激振导致的短促瞬态触顶
        → should_discard = False, decision = "ACCEPT"，准予送入模型保全重大撞机告警能力。
      - no_clip (正常动态范围):
        信号动态范围健康，线性响应良好
        → should_discard = False, decision = "ACCEPT"，正常送检。

    :param audio: 单声道浮点音频数组 [-1.0, 1.0]
    :param sr: 采样率 (默认 10000 Hz)
    :param flat_thresh: 判定饱和区间的幅值下限 (默认 0.95)
    :param flat_diff_tol: 相邻采样点平顶波动容差 (默认 1e-4)
    :param full_scale: 满量程判定阈值 (默认 0.98)
    :param clip_flat_ratio: 削顶平顶占比阈值 (默认 0.10 即 10.0%)
    :param min_consec_flat_ms: 连续平顶判定阈值 (默认 35.0ms)
    :return: 包含完整质量诊断字段的字典 report
    """
    total_len = len(audio)
    if total_len == 0:
        return {
            "level": "clip",
            "multilevel_grade": "SEVERE",
            "decision": "DISCARD",
            "peak": 0.0,
            "flat_ratio": 0.0,
            "saturation_ratio": 0.0,
            "saturation_percent": 0.0,
            "max_consec_flat_ms": 0.0,
            "max_consecutive_flat_ms": 0.0,
            "flat_saturated_ratio": 0.0,
            "total_flat_plateaus": 0,
            "full_scale_ratio": 0.0,
            "skewness": 0.0,
            "is_clipping": True,
            "should_discard": True,
            "is_persistent_clipping": True,
            "is_analog_rail_clip": False,
            "analog_clip_info": {"reason": "EMPTY", "is_clipped": False},
            "message": "音频长度为 0，直接舍弃",
        }

    abs_a = np.abs(audio)
    peak = float(np.max(abs_a))

    # 1. 饱和电平样点统计 (|x| >= flat_thresh)
    sat_mask = abs_a >= flat_thresh
    sat_count = int(np.sum(sat_mask))
    sat_ratio = float(sat_count) / float(total_len)

    # 2. 动态适应采样率微调差分门限并统计平顶比
    adj_diff_tol = flat_diff_tol * (10000.0 / max(1000.0, float(sr)))
    diff = np.abs(np.diff(audio))

    hi_mask = abs_a[1:] >= flat_thresh
    hi_count = int(np.sum(hi_mask))
    flat_mask = hi_mask & (diff < adj_diff_tol)
    flat_count = int(np.sum(flat_mask))
    flat_ratio = (float(flat_count) / float(hi_count)) if hi_count > 0 else 0.0
    flat_sat_ratio = float(flat_count) / float(total_len)

    # 3. 连续饱和平顶时长与台阶数统计
    max_consec_samples = 0
    curr_run = 0
    plateau_count = 0
    for v in flat_mask:
        if v:
            curr_run += 1
        else:
            if curr_run > 0:
                plateau_count += 1
                if curr_run > max_consec_samples:
                    max_consec_samples = curr_run
                curr_run = 0
    if curr_run > 0:
        plateau_count += 1
        if curr_run > max_consec_samples:
            max_consec_samples = curr_run

    max_consec_ms = float(max_consec_samples) / (float(sr) / 1000.0)
    full_scale_ratio = float(np.sum(abs_a >= full_scale)) / float(total_len)

    # 4. 自适应模拟供电轨与硬件嵌位检测 (支持非 1.0 的任意硬件饱和电平)
    x_min, x_max = float(np.min(audio)), float(np.max(audio))
    span = float(x_max - x_min)

    if span < 0.06:
        is_analog_clip = False
        analog_direction = "NONE"
        analog_rail_v = 0.0
        analog_info = {
            "is_clipped": False,
            "clip_direction": "NONE",
            "bottom_pileup_pct": 0.0,
            "top_pileup_pct": 0.0,
            "valley_pin_pct": 0.0,
            "peak_pin_pct": 0.0,
            "max_bottom_dwell_ms": 0.0,
            "max_top_dwell_ms": 0.0,
            "rail_voltage_normalized": 0.0,
            "min_val": round(x_min, 4),
            "max_val": round(x_max, 4),
            "span": round(span, 4),
        }
    else:
        band_tol = span * 0.015
        bottom_band = x_min + band_tol
        top_band = x_max - band_tol

        bottom_ratio = float(np.sum(audio <= bottom_band)) / float(total_len)
        top_ratio = float(np.sum(audio >= top_band)) / float(total_len)

        min_dist = max(1, int(sr * 0.003))
        valleys_idx, _ = signal.find_peaks(-audio, distance=min_dist)
        peaks_idx, _ = signal.find_peaks(audio, distance=min_dist)

        v_pin_ratio = (float(np.sum(audio[valleys_idx] <= bottom_band)) / len(valleys_idx)) if len(valleys_idx) > 10 else 0.0
        p_pin_ratio = (float(np.sum(audio[peaks_idx] >= top_band)) / len(peaks_idx)) if len(peaks_idx) > 10 else 0.0

        def _calc_max_dwell_ms(mask):
            runs = []
            cur = 0
            for m in mask:
                if m:
                    cur += 1
                else:
                    if cur > 0:
                        runs.append(cur)
                        cur = 0
            if cur > 0:
                runs.append(cur)
            max_pts = max(runs) if runs else 0
            return float(max_pts) / (sr / 1000.0)

        max_bottom_dwell_ms = _calc_max_dwell_ms(audio <= bottom_band)
        max_top_dwell_ms = _calc_max_dwell_ms(audio >= top_band)

        is_bottom_clipped = (bottom_ratio >= 0.04 and v_pin_ratio >= 0.15) or (max_bottom_dwell_ms >= 5.0 and bottom_ratio >= 0.02)
        is_top_clipped = (top_ratio >= 0.04 and p_pin_ratio >= 0.15) or (max_top_dwell_ms >= 5.0 and top_ratio >= 0.02)

        if is_bottom_clipped and is_top_clipped:
            analog_direction = "BOTH"
        elif is_bottom_clipped:
            analog_direction = "BOTTOM"
        elif is_top_clipped:
            analog_direction = "TOP"
        else:
            analog_direction = "NONE"

        is_analog_clip = is_bottom_clipped or is_top_clipped
        analog_rail_v = x_min if is_bottom_clipped else (x_max if is_top_clipped else 0.0)

        analog_info = {
            "is_clipped": is_analog_clip,
            "clip_direction": analog_direction,
            "bottom_pileup_pct": round(bottom_ratio * 100, 2),
            "top_pileup_pct": round(top_ratio * 100, 2),
            "valley_pin_pct": round(v_pin_ratio * 100, 2),
            "peak_pin_pct": round(p_pin_ratio * 100, 2),
            "max_bottom_dwell_ms": round(max_bottom_dwell_ms, 2),
            "max_top_dwell_ms": round(max_top_dwell_ms, 2),
            "rail_voltage_normalized": round(analog_rail_v, 4),
            "min_val": round(x_min, 4),
            "max_val": round(x_max, 4),
            "span": round(span, 4),
        }

    # 5. 持续硬件死锁判定
    is_persistent = bool((sat_ratio >= clip_flat_ratio) or (max_consec_ms >= min_consec_flat_ms) or is_analog_clip)

    # 6. 计算偏度 (Skewness) 与多级健康度等级
    std_val = float(np.std(audio))
    skew = float(stats.skew(audio)) if (total_len > 10 and std_val > 1e-6) else 0.0

    if is_persistent:
        level = "clip"
        multilevel_grade = "SEVERE"
        should_discard = True
        is_clipping = True
        if is_analog_clip:
            rail_v = analog_info.get("rail_voltage_normalized", 0.0)
            msg = (f"持续性模拟供电轨硬件削顶 (饱和轨约 {rail_v:+.4f}, "
                   f"钉扎率 {analog_info.get('valley_pin_pct', 0):.1f}%, "
                   f"平底驻留 {analog_info.get('max_bottom_dwell_ms', 0):.1f}ms)，按规范舍弃不送入模型")
        else:
            msg = (f"持续性硬件削顶方波故障 (峰值{peak*100:.1f}%, 饱和占比{sat_ratio*100:.2f}%, "
                   f"最长连续死锁{max_consec_ms:.1f}ms, 削顶台阶数{plateau_count})，按规范舍弃不送入模型")
    elif peak >= full_scale:
        level = "transient_clip"
        multilevel_grade = "MILD"
        should_discard = False
        is_clipping = True
        msg = (f"瞬态强撞击幅值饱和 (峰值{peak*100:.1f}%, 饱和占比{sat_ratio*100:.2f}%, "
               f"最长连续{max_consec_ms:.1f}ms)，准予送入模型推断")
    elif abs(skew) > 1.2 and span > 0.3:
        level = "no_clip"
        multilevel_grade = "SOFT"
        should_discard = False
        is_clipping = False
        msg = f"检测到轻度非线性偏斜软饱和 (偏度{skew:.2f})，正常送入模型推断"
    else:
        level = "no_clip"
        multilevel_grade = "NONE"
        should_discard = False
        is_clipping = False
        msg = f"正常动态范围无削顶 (峰值{peak*100:.1f}%)，正常送入模型推断"

    decision = "DISCARD" if should_discard else "ACCEPT"

    return {
        "level": level,
        "multilevel_grade": multilevel_grade,
        "decision": decision,
        "peak": round(peak, 6),
        "flat_ratio": round(flat_ratio, 6),
        "saturation_ratio": round(sat_ratio, 6),
        "saturation_percent": round(sat_ratio * 100.0, 2),
        "max_consec_flat_ms": round(max_consec_ms, 2),
        "max_consecutive_flat_ms": round(max_consec_ms, 2),  # 向后兼容字段
        "flat_saturated_ratio": round(flat_sat_ratio, 6),
        "total_flat_plateaus": plateau_count,
        "full_scale_ratio": round(full_scale_ratio, 6),
        "skewness": round(skew, 4),
        "is_clipping": is_clipping,
        "should_discard": should_discard,
        "is_persistent_clipping": is_persistent,
        "is_analog_rail_clip": is_analog_clip,
        "rail_voltage": round(analog_info.get("rail_voltage_normalized", 0.0), 4),
        "clip_direction": analog_info.get("clip_direction", "NONE"),
        "analog_clip_info": analog_info,
        "message": msg,
    }



def detect_quiet_or_steady_noise(
    audio: np.ndarray,
    sr: int = 10000,
    min_rms_thresh: float = 0.02,         # 极低 RMS 能量绝对底噪门限 (默认 0.02)
    peak_amp_thresh: float = 0.05,        # 极低能量绝对底噪门限 5.0% (实测基准最小撞击为 6.63%)
    peak_steady_thresh: float = 0.10,     # 稳态运转杂音峰值上限 10.0%
    kurtosis_steady_thresh: float = 3.8,  # 稳态高斯振动最大分段峭度门限 (高斯分布理论值为 3.0)
    hf_band_min: float = 1500.0,          # 金属碰撞高频共振带下限
    hf_band_max: float = 4800.0           # 金属碰撞高频共振带上限
):
    """
    前置稳态底噪与微弱信号快速旁路检测器 (Quiet & Steady Noise Fast Bypass):
    结合工业状态监测标准 (ISO 20816/13373)，利用具有时域尺度不变性的四阶中心矩峭度 (Kurtosis)
    与物理峰值幅值，快速旁路无物理碰撞破坏性的稳态背景运转底噪与微弱单频机电杂音。

    判定准则：
      0. 极低能量均方根底噪：RMS < min_rms_thresh (默认 0.02)，微弱动能完全不足以构成机械损伤，直接快速放行；
      1. 极低能量微弱底噪：整段音频最大绝对峰值 < peak_amp_thresh (5.0%)，物理动能不足以构成机械损伤，直接安全旁路；
      2. 稳态高斯连续运转杂音：整段音频峰值 < peak_steady_thresh (10.0%) 且最大分段峭度 < kurtosis_steady_thresh (3.8)，
         波形呈现标准高斯分布特征，无厚尾瞬态脉冲；
      3. 高频共振保护校验：若高频带 (1.5k~4.8kHz) 滤波信号检测到强冲击共振脉冲 (HF 峭度 >= 5.0)，即使低频平稳也禁止旁路。

    :param audio: 浮点音频数组 [-1.0, 1.0]
    :param sr: 采样率 (默认 10000 Hz)
    :param min_rms_thresh: 极低 RMS 能量底噪门限 (默认 0.02)
    :param peak_amp_thresh: 极低能量绝对底噪门限 (默认 0.05 即 5.0%)
    :param peak_steady_thresh: 稳态杂音峰值门限 (默认 0.10 即 10.0%)
    :param kurtosis_steady_thresh: 稳态高斯峭度门限 (默认 3.8)
    :param hf_band_min: 金属高频带下限 (默认 1500 Hz)
    :param hf_band_max: 金属高频带上限 (默认 4800 Hz)
    :return: (is_quiet_normal: bool, message: str)
    """
    if len(audio) == 0:
        return False, "音频为空"

    abs_a = np.abs(audio)
    peak_val = float(np.max(abs_a))
    peak_pct = peak_val * 100.0
    rms_val = float(np.sqrt(np.mean(audio ** 2)))

    # 准则 0: 极低能量均方根底噪 (RMS < min_rms_thresh，直接快速放行)
    if min_rms_thresh > 0 and rms_val < min_rms_thresh:
        return True, f"前置快速旁路: 极低静默底噪 (RMS {rms_val:.5f} < {min_rms_thresh:.4f})"

    # 准则 1: 极低能量微弱底噪 (物理动能极小，100% 安全旁路)
    if peak_val < peak_amp_thresh:
        return True, f"前置快速旁路: 极低能量微弱底噪 (峰值 {peak_pct:.2f}% < {peak_amp_thresh*100:.1f}%)"

    # 计算分段最大峭度 (采用 2.0s 滑动窗口，50% 重叠，严格捕捉任意局域突发脉冲)
    chunk_len = min(len(audio), int(2.0 * sr))
    hop_len = max(1, chunk_len // 2)
    if len(audio) <= chunk_len:
        mean_val = np.mean(audio)
        var_val = np.mean((audio - mean_val) ** 2)
        max_chunk_kurt = float(np.mean((audio - mean_val) ** 4) / (var_val ** 2 + 1e-12))
    else:
        chunks = np.lib.stride_tricks.sliding_window_view(audio, chunk_len)[::hop_len]
        means = np.mean(chunks, axis=1, keepdims=True)
        vars_ = np.mean((chunks - means) ** 2, axis=1)
        m4 = np.mean((chunks - means) ** 4, axis=1)
        kurts = m4 / (vars_ ** 2 + 1e-12)
        max_chunk_kurt = float(np.max(kurts))

    # 检查高频共振带脉冲特征 (防止低频强风噪掩盖高频金属敲击)
    hf_kurt = 0.0
    nyq = sr * 0.5
    if nyq > hf_band_min and len(audio) >= 256:
        try:
            high_cutoff = min(hf_band_max, nyq - 50.0)
            b, a = signal.butter(4, [hf_band_min / nyq, high_cutoff / nyq], btype='band')
            hf_audio = signal.filtfilt(b, a, audio)
            hf_mean = np.mean(hf_audio)
            hf_var = np.mean((hf_audio - hf_mean) ** 2)
            hf_kurt = float(np.mean((hf_audio - hf_mean) ** 4) / (hf_var ** 2 + 1e-12))
        except Exception:
            hf_kurt = 0.0

    # 若高频共振带呈现极端厚尾冲击脉冲 (HF 峭度 >= 5.0)，属于潜在金属微弱敲击，严禁旁路
    if hf_kurt >= 5.0:
        return False, f"高频金属共振带检测到突发冲击 (HF峭度 {hf_kurt:.2f} >= 5.0)"

    # 准则 2: 稳态低能量运转杂音 (峰值 < 10.0% 且分段峭度 < 3.8，典型高斯稳态无冲击)
    if peak_val < peak_steady_thresh and max_chunk_kurt < kurtosis_steady_thresh:
        return True, f"前置快速旁路: 稳态低能量运转底噪 (峰值 {peak_pct:.2f}%, 分段峭度 {max_chunk_kurt:.2f} < {kurtosis_steady_thresh:.1f})"

    # 准则 3: 稳态高能量连续运转轰鸣 (峰值 < 35.0% 但分段峭度严格 < 3.3，纯电机/风机稳态运转)
    if peak_val < 0.35 and max_chunk_kurt < 3.3 and hf_kurt < 3.5:
        return True, f"前置快速旁路: 稳态机电连续运转轰鸣 (峰值 {peak_pct:.2f}%, 分段峭度 {max_chunk_kurt:.2f} < 3.3)"

    return False, f"可疑突发冲击信号 (峰值 {peak_pct:.2f}%, 分段峭度 {max_chunk_kurt:.2f})"


def detect_step_change_point(
    audio: np.ndarray,
    sr: int = 10000,
    min_margin_sec: float = 0.3,
    jump_ratio_thresh: float = 2.0,
    max_steady_kurt: float = 3.8,
    max_steady_crest: float = 4.0,
    min_high_energy_rms: float = 0.05
) -> dict:
    """
    工业级全局最优变点检测器 (Global Optimal Two-Segment Change-Point Detector):
    针对现场机组启机 (Step-up) 与停机 (Step-down) 过程设计，支持跃迁时刻在时域两端极大偏移 (0.3s ~ 19.7s)。
    
    算法机理：
      1. 提取短时 RMS 能量包络，以动态最优两段划分扫描所有候选切分点 k*；
      2. 采用统计方差加权两样本离散度目标函数 S(k) 搜索全局最大跃迁点；
      3. 对高能段进行机械旋转力学质检 (低峭度 + 低波峰比)，彻底封死将真实大碰撞误判为阶跃的漏洞。

    :return: 包含检测结论、工况类型 (STARTUP_STEP_UP / SHUTDOWN_STEP_DOWN / NONE)、跃迁时刻、跳变倍数等
    """
    dur = len(audio) / sr if sr > 0 else 0.0
    if dur < 1.5 or len(audio) == 0:
        return {
            "detected": False,
            "type": "NONE",
            "time_sec": 0.0,
            "jump_ratio": 1.0,
            "message": "音频时长过短 (<1.5s)"
        }

    # 1. 提取短时 RMS 能量包络 (100ms 窗, 50ms 步长)
    hop = max(1, int(0.05 * sr))
    win = max(1, int(0.10 * sr))
    rms_seq = []
    times = []
    for st in range(0, len(audio) - win + 1, hop):
        w = audio[st : st + win]
        rms_seq.append(float(np.sqrt(np.mean(w**2))))
        times.append(st / sr)

    rms_seq = np.array(rms_seq, dtype=np.float32)
    times = np.array(times, dtype=np.float32)
    N = len(rms_seq)

    # 2. 全局动态扫描最佳变点 k*
    min_margin = max(3, int(min_margin_sec / 0.05))
    if N <= 2 * min_margin:
        return {
            "detected": False,
            "type": "NONE",
            "time_sec": 0.0,
            "jump_ratio": 1.0,
            "message": "有效采样帧不足"
        }

    best_stat = 0.0
    best_k = 0
    best_direction = "NONE"

    for k in range(min_margin, N - min_margin):
        m_left = float(np.mean(rms_seq[:k]))
        m_right = float(np.mean(rms_seq[k:]))
        diff = m_right - m_left
        weight = np.sqrt((k * (N - k)) / float(N))
        stat = abs(diff) * weight

        if stat > best_stat:
            best_stat = stat
            best_k = k
            best_direction = "STEP_UP" if diff > 0 else "STEP_DOWN"

    t_split = float(times[best_k])
    left_mean = float(np.mean(rms_seq[:best_k])) + 1e-6
    right_mean = float(np.mean(rms_seq[best_k:])) + 1e-6
    ratio = (right_mean / left_mean) if best_direction == "STEP_UP" else (left_mean / right_mean)

    # 3. 提取高能运转段并进行物理力学校验
    if best_direction == "STEP_UP":
        high_seg = audio[int(t_split * sr) :]
    else:
        high_seg = audio[: int(t_split * sr)]

    if len(high_seg) < 64:
        return {"detected": False, "type": "NONE", "time_sec": 0.0, "jump_ratio": 1.0, "message": "高能段样点过少"}

    rms_high = float(np.sqrt(np.mean(high_seg**2)))
    peak_high = float(np.max(np.abs(high_seg)))
    crest_high = peak_high / (rms_high + 1e-6)
    var_high = float(np.var(high_seg))
    kurt_high = float(np.mean((high_seg - np.mean(high_seg))**4) / (var_high**2 + 1e-12)) if var_high > 1e-8 else 1.0

    # 物理合法性判定 (确保高能段是机械运转而非冲击脉冲群，且能量跨越显著)
    is_valid = (
        (ratio >= jump_ratio_thresh) and
        (rms_high >= min_high_energy_rms) and
        (kurt_high <= max_steady_kurt) and
        (crest_high <= max_steady_crest)
    )

    result_type = "STARTUP_STEP_UP" if (is_valid and best_direction == "STEP_UP") else (
        "SHUTDOWN_STEP_DOWN" if (is_valid and best_direction == "STEP_DOWN") else "NONE"
    )

    msg = ""
    if result_type == "STARTUP_STEP_UP":
        msg = f"检测到机组启动工况 (跃迁点约 {t_split:.2f}s, 能量跳升 {ratio:.1f}倍, 稳态峭度 {kurt_high:.2f})"
    elif result_type == "SHUTDOWN_STEP_DOWN":
        msg = f"检测到机组停机工况 (跃迁点约 {t_split:.2f}s, 能量跌落 {ratio:.1f}倍, 稳态峭度 {kurt_high:.2f})"
    else:
        msg = "未检出单向启停机阶跃"

    return {
        "detected": bool(is_valid),
        "type": result_type,
        "direction": best_direction,
        "time_sec": round(t_split, 2),
        "jump_ratio": round(float(ratio), 2),
        "high_rms": round(float(rms_high), 4),
        "high_kurtosis": round(float(kurt_high), 2),
        "high_crest": round(float(crest_high), 2),
        "high_duration_sec": round(len(high_seg) / sr, 2),
        "message": msg
    }


def preprocess_audio_for_model(file_path_or_audio, target_sr: int = 10000, min_rms_threshold: float = 0.02):
    """
    供全系统所有模型统一调用的标准前处理入口：
    1. 统一加载并规范化音频为 10kHz 单声道浮点数组 (自动多声道合并与高质量重采样)；
    2. 执行持续削顶与硬件故障方波拦截 (保留瞬态碰撞饱和，仅拦截死锁方波)；
    3. 执行工业级双向阶跃变点检测 (现场启动 / 现场停机 专属分类与拦截)；
    4. 执行微弱底噪 (RMS < min_rms_threshold) 与稳态连续杂音前置快速旁路检测；
    5. 综合输出 should_discard、is_quiet_normal、is_startup、is_shutdown、决策状态及清洗后音频。

    :return: (should_discard: bool, report: dict, audio: np.ndarray)
    """
    if isinstance(file_path_or_audio, str):
        audio, orig_sr = load_audio_file(file_path_or_audio, target_sr=target_sr)
    else:
        audio = np.array(file_path_or_audio, dtype=np.float32)
        orig_sr = target_sr

    # 1. 削顶与饱和状态判别：严格区分持续性硬件故障方波与瞬态碰撞饱和
    clip_report = detect_clipping_level(audio, sr=orig_sr)
    should_discard = bool(clip_report["should_discard"])

    # 2. 工业双向阶跃变点检测 (识别现场启动与现场停机)
    is_startup = False
    is_shutdown = False
    step_report = {"detected": False, "type": "NONE"}
    if not should_discard:
        step_report = detect_step_change_point(audio, sr=orig_sr)
        if step_report.get("detected", False):
            if step_report.get("type") == "STARTUP_STEP_UP":
                is_startup = True
            elif step_report.get("type") == "SHUTDOWN_STEP_DOWN":
                is_shutdown = True

    # 3. 极低能量底噪与稳态杂音前置快速旁路检测 (含 RMS < min_rms_threshold)
    if not should_discard and not is_startup and not is_shutdown:
        is_quiet_normal, quiet_msg = detect_quiet_or_steady_noise(
            audio, sr=orig_sr, min_rms_thresh=min_rms_threshold
        )
    else:
        is_quiet_normal, quiet_msg = False, "已检出硬件削顶或启停机工况"

    # 记录判别信息与向下兼容字段
    clip_report["is_persistent_clipping"] = should_discard
    clip_report["is_quiet_normal"] = is_quiet_normal
    clip_report["quiet_message"] = quiet_msg
    clip_report["is_startup"] = is_startup
    clip_report["is_shutdown"] = is_shutdown
    clip_report["step_report"] = step_report

    if should_discard:
        clip_report["decision"] = "DISCARD"
    elif is_startup:
        clip_report["decision"] = "MACHINE_STARTUP"
    elif is_shutdown:
        clip_report["decision"] = "MACHINE_SHUTDOWN"
    elif is_quiet_normal:
        clip_report["decision"] = "QUIET_NORMAL"
    else:
        clip_report["decision"] = "ACCEPT"

    return should_discard, clip_report, audio


def aggregate_slice_predictions(
    slice_probs,
    top1_weight: float = 0.85,
    top2_weight: float = 0.15
):
    """
    多切片软加权聚合标准算法 (Soft Top-2 Aggregation):
    解决两极分化与单一切片瞬态绑架全局问题。
    - 针对连续严重撞机: Top1 与 Top2 均高，综合置信度坚挺在 95%~99%
    - 针对孤立短促单脉冲: Top1 高但 Top2 低，综合置信度平滑处于 85%~90%
    - 针对偶发假脉冲干扰: Top2 极低，综合置信度被有效压低在告警线以下

    :param slice_probs: 各切片的碰撞置信度 (一维数组/列表, 百分比 0.0~100.0)
    :param top1_weight: 最高置信度切片权重 (默认 0.85)
    :param top2_weight: 次高置信度切片权重 (默认 0.15)
    :return: (agg_prob, top1_prob, top2_prob)
    """
    if slice_probs is None or len(slice_probs) == 0:
        return 0.0, 0.0, 0.0
    arr = np.asarray(slice_probs, dtype=np.float32)
    if arr.ndim > 1:
        arr = arr.squeeze()
    if arr.ndim == 0:
        val = float(arr)
        return val, val, val
    sorted_p = np.sort(arr)[::-1]
    top1 = float(sorted_p[0])
    top2 = float(sorted_p[1]) if len(sorted_p) > 1 else top1
    agg = float(top1_weight * top1 + top2_weight * top2)
    return round(agg, 2), round(top1, 2), round(top2, 2)

