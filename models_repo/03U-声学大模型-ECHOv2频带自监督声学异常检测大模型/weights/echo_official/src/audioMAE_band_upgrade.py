import numpy as np
import torch
import torch.nn as nn

from .modules import PatchEmbed
from .utils import (
    aggregate_segment_features,
    audio_to_spectrogram,
    get_1d_sincos_pos_embed_from_grid,
    get_sincos_encoding_1d,
    split_segments,
)


class AudioMAEWithBand(nn.Module):
    def __init__(
        self,
        spec_len=2000,
        band_width=32,
        shift_size=16,
        in_chans=1,
        embed_dim=384,
        encoder_depth=12,
        num_heads=6,
        mlp_ratio=4.0,
        norm_layer=nn.LayerNorm,
        fix_pos_emb=True,
        mask_ratio=0.75,
        freq_pos_emb_dim=384,
    ):
        super().__init__()
        self.band_width = band_width
        self.shift_size = shift_size
        self.embed_dim = embed_dim
        self.fix_pos_emb = fix_pos_emb
        self.in_chans = in_chans
        self.encoder_depth = encoder_depth
        self.num_heads = num_heads
        self.mlp_ratio = mlp_ratio
        self.spec_len = spec_len
        self.mask_ratio = mask_ratio
        self.freq_pos_emb_dim = freq_pos_emb_dim

        self.patch_embed = PatchEmbed(
            band_width=band_width,
            shift_size=shift_size,
            in_chans=in_chans,
            embed_dim=embed_dim,
        )

        # Number of time patches per band.
        if spec_len % shift_size != 0:
            self.num_patches_per_band = (spec_len // shift_size) + 1
        else:
            self.num_patches_per_band = spec_len // shift_size

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_embed = nn.Parameter(
            torch.zeros(1, self.num_patches_per_band + 1, embed_dim),
            requires_grad=(not self.fix_pos_emb),
        )

        self.encoder = nn.TransformerEncoder(
            nn.TransformerEncoderLayer(
                d_model=embed_dim,
                nhead=num_heads,
                dim_feedforward=int(embed_dim * mlp_ratio),
                activation="gelu",
                batch_first=True,
                layer_norm_eps=1e-6,
                dropout=0.1,
            ),
            num_layers=encoder_depth,
        )
        self.norm = norm_layer(embed_dim)
        self._init_weights()

    def _init_weights(self):
        pos_indices = np.arange(self.spec_len // self.shift_size, dtype=np.float32)
        pos_embed = get_1d_sincos_pos_embed_from_grid(self.pos_embed.shape[-1], pos_indices)
        cls_pos_embed = np.zeros([1, self.pos_embed.shape[-1]])
        pos_embed = np.concatenate([cls_pos_embed, pos_embed], axis=0)
        self.pos_embed.data.copy_(torch.from_numpy(pos_embed).float().unsqueeze(0))

        torch.nn.init.normal_(self.cls_token, std=0.02)
        self.apply(self._init_module_weights)

    @staticmethod
    def _init_module_weights(m):
        if isinstance(m, nn.Linear):
            torch.nn.init.xavier_uniform_(m.weight)
            if m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def patchify(self, bands):
        # bands: (num_bands, 1, band_width, time)
        return self.patch_embed(bands)

    @staticmethod
    def random_masking(patches, mask_ratio):
        """Apply random masking to patch tokens."""
        n, l, d = patches.shape
        len_keep = int(l * (1 - mask_ratio))

        if mask_ratio == 0:
            mask = torch.zeros([n, l], device=patches.device)
            ids_restore = torch.arange(l, device=patches.device).unsqueeze(0).expand(n, -1)
            ids_keep = torch.arange(l, device=patches.device).unsqueeze(0).expand(n, -1)
            return patches, mask, ids_restore, ids_keep

        noise = torch.rand(n, l, device=patches.device)
        ids_shuffle = torch.argsort(noise, dim=1)
        ids_restore = torch.argsort(ids_shuffle, dim=1)

        ids_keep = ids_shuffle[:, :len_keep]
        patches_unmasked = torch.gather(patches, dim=1, index=ids_keep.unsqueeze(-1).repeat(1, 1, d))

        mask = torch.ones([n, l], device=patches.device)
        mask[:, :len_keep] = 0
        mask = torch.gather(mask, dim=1, index=ids_restore)
        return patches_unmasked, mask, ids_restore, ids_keep

    def _compute_frequency_position_encoding(self, spectrogram, sample_rate):
        """Split spectrogram into bands and compute frequency positional encoding."""
        num_freq_bins = spectrogram.shape[0]
        num_bands = max(1, (num_freq_bins + self.band_width - 1) // self.band_width)

        band_spectrograms = []
        band_patch_indices = []
        all_freq_pos_norm = []

        for band_idx in range(num_bands):
            start_freq = band_idx * self.band_width
            end_freq = min((band_idx + 1) * self.band_width, num_freq_bins)
            if end_freq <= start_freq:
                continue

            band_spec = spectrogram[start_freq:end_freq, :]
            if band_spec.shape[0] < self.band_width:
                padding_height = self.band_width - band_spec.shape[0]
                band_spec = torch.nn.functional.pad(
                    band_spec, (0, 0, padding_height, 0), mode="constant", value=0
                )

            band_spectrograms.append(band_spec)
            band_patch_indices.append(torch.tensor([start_freq, end_freq], dtype=torch.long, device=spectrogram.device))

            center_bin = (start_freq + end_freq - 1) / 2.0
            n_fft = int(0.025 * sample_rate)
            center_freq = center_bin * sample_rate / n_fft
            nyquist = sample_rate / 2.0
            freq_pos_norm = center_freq / nyquist if nyquist > 0 else 0.0
            all_freq_pos_norm.append(freq_pos_norm)

        if not band_spectrograms:
            raise ValueError("No bands found in spectrogram")

        band_spectrograms = torch.stack(band_spectrograms, dim=0)
        band_patch_indices = torch.stack(band_patch_indices, dim=0)
        freq_pos_norm = torch.tensor(all_freq_pos_norm, dtype=torch.float32, device=spectrogram.device).unsqueeze(1)
        freq_pos_emb = get_sincos_encoding_1d(freq_pos_norm, self.freq_pos_emb_dim)
        return freq_pos_emb, band_spectrograms, band_patch_indices

    def extract_features(self, spectrogram, sample_rate):
        """Return utterance-level and frame-level features."""
        freq_pos_emb, band_spectrograms, band_patch_indices = self._compute_frequency_position_encoding(
            spectrogram, sample_rate
        )

        tokens, _, _, _, _, _, _, _ = self.forward(
            sample_rate=sample_rate,
            freq_pos_emb=freq_pos_emb,
            band_spectrograms=band_spectrograms.unsqueeze(1),
            band_patch_indices=band_patch_indices,
            mask_ratio=0.0,
        )

        cls_tokens = tokens[:, 0, :]
        utt_feature = cls_tokens.flatten()

        frame_feature = tokens[:, 1:, :]
        b_sz, t_sz, d_sz = frame_feature.shape
        frame_feature = frame_feature.permute(1, 0, 2).contiguous().view(t_sz, b_sz * d_sz)
        return utt_feature, frame_feature

    def forward(
        self,
        sample_rate,
        mask_ratio=None,
        freq_pos_emb=None,
        band_spectrograms=None,
        band_patch_indices=None,
    ):
        if mask_ratio is None:
            mask_ratio = self.mask_ratio

        if freq_pos_emb is None or band_spectrograms is None or band_patch_indices is None:
            raise ValueError("forward requires freq_pos_emb, band_spectrograms, and band_patch_indices")

        band_spectrograms = band_spectrograms.to(self.pos_embed.device)
        patches = self.patchify(band_spectrograms)

        patches = patches + self.pos_embed[:, 1 : patches.shape[1] + 1, :]

        freq_pos_emb = freq_pos_emb.to(self.pos_embed.device)
        freq_pos_emb_expanded = freq_pos_emb.unsqueeze(1).expand(-1, patches.shape[1], -1)
        patches = patches + freq_pos_emb_expanded

        patches_unmasked, mask, ids_restore, ids_keep = self.random_masking(patches, mask_ratio)

        n = patches_unmasked.shape[0]
        cls_token = self.cls_token.expand(n, -1, -1)
        cls_token = cls_token + freq_pos_emb.unsqueeze(1)
        tokens = torch.cat((cls_token, patches_unmasked), dim=1)

        tokens = self.encoder(tokens)
        tokens = self.norm(tokens)

        return tokens, mask, ids_restore, band_patch_indices, patches, ids_keep, freq_pos_emb, band_spectrograms

    def extract_features_from_audio(self, audio_signal: torch.Tensor, sample_rate: int):
        """Extract utterance-level and frame-level features from audio waveform."""
        spec = audio_to_spectrogram(
            audio_signal=audio_signal,
            sample_rate=sample_rate,
            norm_mean=-5.874158,
            norm_std=5.223174,
        )
        segments = split_segments(spec=spec, max_length=self.spec_len)
        device = next(self.parameters()).device

        utt_features = []
        frame_features = []
        with torch.inference_mode():
            for seg in segments:
                utt_feat, frame_feat = self.extract_features(seg.to(device), sample_rate)
                utt_features.append(utt_feat.cpu())
                frame_features.append(frame_feat.cpu())

        return aggregate_segment_features(utt_features, frame_features)
