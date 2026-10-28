"""ECHOv2-small loader."""

from __future__ import annotations

import os
from pathlib import Path
from os import PathLike
from typing import Optional, Union

from src.audioMAE_band_upgrade import AudioMAEWithBand
from src.utils import load_checkpoint, resolve_checkpoint, sanitize_state_dict


def from_pretrained(source: Optional[Union[str, PathLike]] = None) -> AudioMAEWithBand:
    """Load ECHOv2-small weights from source, local file, or Hugging Face."""
    model = AudioMAEWithBand()
    pkg_dir = Path(__file__).resolve().parent

    if source is not None:
        ckpt_path = resolve_checkpoint(source=source, filename="model.safetensors")
    else:
        local_ckpt = pkg_dir / "model.safetensors"
        if local_ckpt.exists():
            ckpt_path = local_ckpt
        else:
            default_repo_id = os.environ.get("ECHOv2_HF_REPO_ID", "yucongzh/echo-small-v2")
            ckpt_path = resolve_checkpoint(source=default_repo_id, filename="model.safetensors")

    state_dict = sanitize_state_dict(load_checkpoint(ckpt_path))
    msg = model.load_state_dict(state_dict, strict=False)

    print(f"ECHOv2 loaded from: {ckpt_path}")
    print(f"Missing keys: {len(msg.missing_keys)} | Unexpected keys: {len(msg.unexpected_keys)}")
    return model.eval()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=str, default=None, help="Local checkpoint path, directory, or HF repo id")
    args = parser.parse_args()

    _ = from_pretrained(args.source)
    print("ECHOv2 model initialized.")
