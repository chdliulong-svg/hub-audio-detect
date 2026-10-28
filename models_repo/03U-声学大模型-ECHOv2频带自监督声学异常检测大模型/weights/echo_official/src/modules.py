import torch
from torch import nn

class PatchEmbed(nn.Module):
    """Convert a band spectrogram to patch embeddings."""
    def __init__(self, band_width, shift_size, in_chans=1, embed_dim=768):
        super().__init__()
        
        self.band_width = band_width
        self.shift_size = shift_size
        self.in_chans = in_chans
        self.embed_dim = embed_dim
        
        # Convolution over full band height and time stride.
        self.proj = nn.Conv2d(in_chans, embed_dim, kernel_size=(band_width, shift_size), stride=(band_width, shift_size))
        
    def forward(self, x):
        """x: (B, band_width, T) or (B, 1, band_width, T)."""
        if x.dim() == 3:
            x = x.unsqueeze(1)

        assert x.dim() == 4, f"Input shape must be (B, band_width, T), but got {x.shape}"
        B, C, H, W = x.shape
        
        assert H == self.band_width, f"Input height ({H}) doesn't match band_width ({self.band_width})"

        num_patches = W // self.shift_size

        if W % self.shift_size != 0:
            padding_width = self.shift_size - (W % self.shift_size)
            x = torch.nn.functional.pad(x, (0, padding_width, 0, 0), mode='constant', value=0)
            num_patches += 1

        patches = self.proj(x)

        patches = patches.squeeze(2).transpose(1, 2)
        
        return patches
