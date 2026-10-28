import math
from pathlib import Path
from typing import List, Tuple, Union

import numpy as np
import torch
import torchaudio


def get_1d_sincos_pos_embed_from_grid(embed_dim, pos):
    """NumPy 1D sin-cos positional embedding."""
    assert embed_dim % 2 == 0
    omega = np.arange(embed_dim // 2, dtype=float)
    omega /= embed_dim / 2.0
    omega = 1.0 / 10000**omega

    pos = pos.reshape(-1)
    out = np.einsum("m,d->md", pos, omega)
    emb_sin = np.sin(out)
    emb_cos = np.cos(out)
    return np.concatenate([emb_sin, emb_cos], axis=1)


def get_sincos_encoding_1d(pos, dim, freq_scale=25):
    """Torch 1D sin-cos encoding for band frequency positions."""
    device = pos.device
    n = pos.shape[0]
    pe = torch.zeros(n, dim, device=device)
    div_term = torch.exp(torch.arange(0, dim, 2, device=device).float() * (-math.log(10000.0) / dim))
    pe[:, 0::2] = torch.sin(pos * freq_scale * div_term)
    pe[:, 1::2] = torch.cos(pos * freq_scale * div_term)
    return pe


def sanitize_state_dict(state_dict: dict) -> dict:
    if not isinstance(state_dict, dict) or not state_dict:
        return state_dict

    keys = list(state_dict.keys())
    if all(k.startswith("module.") for k in keys):
        return {k[len("module."):]: v for k, v in state_dict.items()}
    if all(k.startswith("student.") for k in keys):
        return {k[len("student."):]: v for k, v in state_dict.items()}
    if all(k.startswith("encoder.") for k in keys):
        return {k[len("encoder."):]: v for k, v in state_dict.items()}
    return state_dict


def load_checkpoint(path: Union[str, Path]) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Checkpoint not found: {path}")

    if path.suffix == ".safetensors":
        from safetensors.torch import load_file

        return load_file(str(path))

    payload = torch.load(str(path), map_location="cpu")
    if isinstance(payload, dict) and "encoder" in payload:
        return payload["encoder"]
    return payload


def resolve_checkpoint(
    source: Union[str, Path],
    filename: str = "model.safetensors",
) -> Path:
    """Resolve checkpoint path from local file/dir or Hugging Face repo id."""
    source_path = Path(source)

    if source_path.is_file():
        return source_path

    local_candidate = source_path / filename
    if local_candidate.exists():
        return local_candidate

    try:
        from huggingface_hub import hf_hub_download
    except Exception as e:
        raise ImportError(
            "huggingface_hub is required when source is a remote repo id. "
            "Install with `pip install huggingface_hub`."
        ) from e

    ckpt_path = hf_hub_download(
        repo_id=str(source),
        filename=filename,
    )
    return Path(ckpt_path)


def audio_to_spectrogram(
    audio_signal: torch.Tensor,
    sample_rate: int,
    norm_mean: float,
    norm_std: float,
) -> torch.Tensor:
    waveform = audio_signal
    if waveform.dim() == 1:
        waveform = waveform.unsqueeze(0)
    waveform = waveform.float() - waveform.float().mean()

    window_size = int(0.025 * sample_rate)
    hop_size = int(0.01 * sample_rate)

    stft = torchaudio.transforms.Spectrogram(
        n_fft=window_size,
        hop_length=hop_size,
        power=1,
        center=False,
    )

    spec = stft(waveform.squeeze(0))
    spec = torch.log(spec + 1e-9)
    spec = (spec - norm_mean) / (norm_std * 2)
    return spec


def split_segments(spec: torch.Tensor, max_length: int) -> List[torch.Tensor]:
    segments = []
    num_segments = spec.shape[-1] // max_length

    for i in range(num_segments):
        segments.append(spec[..., i * max_length : (i + 1) * max_length])

    if num_segments * max_length < spec.shape[-1] or not segments:
        tail = min(spec.shape[-1], max_length)
        segments.append(spec[..., -tail:])

    return segments


def aggregate_segment_features(
    utt_features: List[torch.Tensor],
    frame_features: List[torch.Tensor],
) -> Tuple[torch.Tensor, torch.Tensor]:
    utt_feature = torch.stack(utt_features, dim=0).mean(dim=0)
    frame_feature = torch.vstack(frame_features)
    return utt_feature, frame_feature
