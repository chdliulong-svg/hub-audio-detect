# -*- coding: utf-8 -*-
"""
通用音频前处理与质量检验公共模块 (Common Audio Preprocessing & Quality Verification)
供全系统所有声学模型（01、02、03 全系列）统一调用
"""

from .audio_preprocessor import (
    load_audio_file,
    detect_clipping_level,
    detect_quiet_or_steady_noise,
    preprocess_audio_for_model,
    aggregate_slice_predictions
)

__all__ = [
    "load_audio_file",
    "detect_clipping_level",
    "detect_quiet_or_steady_noise",
    "preprocess_audio_for_model",
    "aggregate_slice_predictions"
]


