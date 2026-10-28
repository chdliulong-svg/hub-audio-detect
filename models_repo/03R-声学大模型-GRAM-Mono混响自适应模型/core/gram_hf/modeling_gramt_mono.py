from transformers import PreTrainedModel
from .model import GRAMT
from .configuration_gramt_mono import GRAMTMonoConfig


class GRAMTMonoModel(PreTrainedModel):
    config_class = GRAMTMonoConfig

    def __init__(self, config):
        super().__init__(config)
        self.model = GRAMT(
                in_channels = config.in_channels,
                decoder_mlp_ratio = config.decoder_mlp_ratio,
                decoder_depth = config.decoder_depth,
                decoder_num_heads = config.decoder_num_heads,
                decoder_embedding_dim = config.decoder_embedding_dim,
                decoder_window_sizes = config.decoder_window_sizes,
                encoder_num_layers = config.encoder_num_layers,
                encoder_num_heads = config.encoder_num_heads,
                encoder_hidden_dim = config.encoder_hidden_dim,
                encoder_mlp_ratio = config.encoder_mlp_ratio,
                encoder_dropout = config.encoder_dropout,
                encoder_attention_dropout = config.encoder_attention_dropout,
                encoder_norm_layer_eps = config.encoder_norm_layer_eps,
                patch_size = config.patch_size,
                frequency_stride = config.frequency_stride,
                time_stride = config.time_stride,
                input_length = getattr(config, 'input_length', 200),
                num_mel_bins = config.num_mel_bins
        )

    def forward(self, tensor, strategy = "raw"):
        return self.model.get_audio_representation(tensor, strategy = strategy)

