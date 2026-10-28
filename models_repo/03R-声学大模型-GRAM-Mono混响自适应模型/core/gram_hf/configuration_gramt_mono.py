from transformers import PretrainedConfig
from typing import List


class GRAMTMonoConfig(PretrainedConfig):
    model_type = "gramt-mono"
    model_size = "base"
    in_channels: int = 1
    patch_size = (16,8)
    frequency_stride = 16
    time_stride = 8

    def __init__(
        self,
        decoder_mlp_ratio: float = 4.0,
        decoder_depth: int = 8,
        decoder_num_heads: int = 8,
        decoder_embedding_dim: int = 512,
        decoder_window_sizes: List[int] = [2, 5, 10, 25, 50, 100, 0, 0],
        encoder_num_layers = 12,
        encoder_num_heads = 12,
        encoder_hidden_dim = 768,
        encoder_mlp_ratio = 4.0,
        encoder_dropout = 0.0,
        encoder_attention_dropout = 0.0,
        encoder_norm_layer_eps = 1e-6,
        input_length = 200,
        num_mel_bins = 128,
        **kwargs,
    ):

        self.decoder_mlp_ratio = decoder_mlp_ratio
        self.decoder_depth = decoder_depth
        self.decoder_num_heads = decoder_num_heads
        self.decoder_embedding_dim = decoder_embedding_dim
        self.decoder_window_sizes = decoder_window_sizes

        self.encoder_num_layers = encoder_num_layers
        self.encoder_num_heads = encoder_num_heads
        self.encoder_hidden_dim = encoder_hidden_dim
        self.encoder_mlp_ratio = encoder_mlp_ratio
        self.encoder_dropout = encoder_dropout
        self.encoder_attention_dropout = encoder_attention_dropout
        self.encoder_norm_layer_eps = encoder_norm_layer_eps


        self.input_length = input_length
        self.max_length = input_length
        self.num_mel_bins = num_mel_bins
        super().__init__(**kwargs)