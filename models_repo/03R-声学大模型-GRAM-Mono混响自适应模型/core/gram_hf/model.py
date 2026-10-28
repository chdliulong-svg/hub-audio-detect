import torch
from torch import nn


from .Patcher import PatchStrategy
from .mwmae import MWMHABlock
from .pos_embed import get_2d_sincos_pos_embed
from .utils import PatchEmbed, create_pretrained_model, repeat_token

from einops import rearrange

from typing import List 

def conv3x3(in_channels, out_channels, stride=1):
    "3x3 convolution with padding"
    return nn.Conv2d(
        in_channels, out_channels, kernel_size=3, stride=stride, padding=1, bias=False
    )


class GRAMT(nn.Module):
    def __init__(
        self,
        model_size="base",
        in_channels = 2,
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
        patch_size = (16,8),
        frequency_stride = 16,
        time_stride = 8,
        input_length = 200,
        num_mel_bins = 128,
        **kwargs,
    ):
        super().__init__()
        self.in_channels = in_channels
        self.input_length = input_length
        # Calculate intermediate shape after masking
        self.patch_strategy = PatchStrategy(tstride = time_stride, 
                                            tshape = patch_size[1], 
                                            fstride = frequency_stride, 
                                            fshape = patch_size[0], 
                                            input_fdim = num_mel_bins, 
                                            input_tdim = self.input_length)
        self.p_f_dim, self.p_t_dim = self.patch_strategy.get_patch_size()
        self.num_patches = self.p_f_dim * self.p_t_dim
        self.grid_size = (self.p_f_dim, self.p_t_dim)

        # This is our encoder.
        # --------------------------------------------------------------------------

        # Transformer
        (
            self.encoder,
            self.encoder_embedding_dim,
        ) = create_pretrained_model(model_size,
                                    encoder_num_layers = encoder_num_layers,
                                    encoder_num_heads = encoder_num_heads,
                                    encoder_hidden_dim = encoder_hidden_dim,
                                    encoder_mlp_dim = int(encoder_hidden_dim * encoder_mlp_ratio),
                                    encoder_dropout = encoder_dropout,
                                    encoder_attention_dropout = encoder_attention_dropout,
                                    encoder_norm_layer_eps = encoder_norm_layer_eps)
        self.encoder_cls_token_num = 1

        # Patch Embedder
        self.patch_embed = PatchEmbed()
        self._update_patch_embed_layers(self.patch_embed)
        
        # Norm/Pos
        self.register_buffer("cls_token",nn.Parameter(torch.zeros([1, 1, self.encoder_embedding_dim]), requires_grad = True))
        torch.nn.init.normal_(self.cls_token, std=0.02)

        # This is our decoder.
        # --------------------------------------------------------------------------
        # MAE decoder specifics
        self.decoder_depth = decoder_depth
        self.decoder_num_heads = decoder_num_heads
        self.decoder_embedding_dim = decoder_embedding_dim
        self.decoder_window_sizes = decoder_window_sizes
        self.decoder_embed = nn.Linear(
            self.encoder_embedding_dim, self.decoder_embedding_dim, bias=True
        )
        
        self.register_buffer("mask_token", nn.Parameter(torch.zeros(1, 1, self.decoder_embedding_dim, requires_grad = True)))
        torch.nn.init.normal_(self.mask_token, std=0.02)
        self.decoder_blocks = nn.ModuleList(
            [
                MWMHABlock(
                    dim=decoder_embedding_dim,
                    num_heads=decoder_num_heads,
                    window_sizes=decoder_window_sizes,
                    shift_windows=False,
                    mlp_ratio=decoder_mlp_ratio,
                    qkv_bias=True,
                    norm_layer=nn.LayerNorm,
                )
                for i in range(self.decoder_depth)
            ]
        )
        cls_token_num = 0
        self.encoder.pos_embedding = self._get_pos_embed_params()
        # Pos Embed init w/o the cls token num
        self.register_buffer("decoder_pos_embed", nn.Parameter(
            torch.zeros(1, self.num_patches, decoder_embedding_dim),
            requires_grad=False,
        ))
        pos_embed = get_2d_sincos_pos_embed(
            decoder_embedding_dim, self.grid_size, cls_token_num=cls_token_num
        )
        self.decoder_pos_embed.data.copy_(
            torch.from_numpy(pos_embed).float().unsqueeze(0)
        )
        # Define prediction layers for Masked Auto Encoder pretraining
        self.spec_pred = nn.Sequential(
            nn.Linear(
                decoder_embedding_dim,
                self.patch_strategy.fshape
                * self.patch_strategy.tshape
                * self.in_channels,
                bias=True,
            ),
        )
        self.decoder_norm = nn.LayerNorm(decoder_embedding_dim)
        # Normalize binaural/ambisonic spectrograms with Layer norm later.
        self.spectrogram_normalize = nn.LayerNorm(
                    [self.in_channels, num_mel_bins, self.input_length],
                    elementwise_affine=False
                )
        self.input_shape = [num_mel_bins, self.input_length]
        compile_modules = kwargs.get("compile_modules", None)
        if (compile_modules is not None) and (compile_modules):
            self._compile_operations()


    def _compile_operations(self):
        """
        Use torch.compile on the extractor, encoder and decoder blocks for faster forward
        """
        try:
            self.forward = torch.compile(self.get_audio_representation, mode = "reduce-overhead")
        except Exception as e:
            print(f"Warning: Could not compile operations: {e}")
            self.use_compiled_forward = False



    def _get_pos_embed_params(self):
        """Calculates the pos embedding embedding parameters and returns them."""
        # Update positional embedding
        pos_embed = nn.Parameter(
            torch.zeros(
                1,
                self.num_patches + self.encoder_cls_token_num,
                self.encoder_embedding_dim,
            ),
            requires_grad=False,
        )
        pos_embed_data = get_2d_sincos_pos_embed(
            self.encoder_embedding_dim,
            self.grid_size,
            cls_token_num=self.encoder_cls_token_num,
        )
        pos_embed.data.copy_(torch.from_numpy(pos_embed_data).float().unsqueeze(0))
        return pos_embed

    def _update_patch_embed_layers(self, patch_embed):
        """Updates the patch embedding embedding layers."""
        # Update patch projection layer
        # Use 2, as the spectrogram has 2 channels
        patch_embed.proj = torch.nn.Conv2d(
            self.in_channels,
            self.encoder_embedding_dim,
            kernel_size=(self.patch_strategy.fshape, self.patch_strategy.tshape),
            stride=(self.patch_strategy.fstride, self.patch_strategy.tstride),
        )
        patch_embed.num_patch = self.num_patches

    def pass_through_encoder(self, x, non_mask_index, B):
        """Passes the input through the Encoder Transformer network."""
        # Add positional embeddings to the x.
        x = x + self.encoder.pos_embedding[:, self.encoder_cls_token_num :, :]
        x = x[non_mask_index, :].reshape((B, -1, x.shape[-1]))
        cls_token = (
            self.cls_token.expand(B, -1, -1)
            + self.encoder.pos_embedding[:, :1, :]
        )
        
        try:
            dist_token = (
                self.encoder.dist_token.expand(B, -1, -1)
                + self.encoder.pos_embedding[:, 1:2, :]
            )
            x = torch.cat((cls_token, dist_token, x), dim=1)
        
        except Exception as e:
            x = torch.cat((cls_token, x), dim=1)


        x = self.encoder.dropout(x)
        for block in self.encoder.layers:
            x = block(x)
        return self.encoder.ln(x)


    def pass_through_decoder(self, encoder_output, non_mask_index, B):
        encoder_output = self.decoder_embed(encoder_output)
        x_ = repeat_token(
            self.mask_token, (B, self.num_patches)
        ).type_as(encoder_output)
        x_[non_mask_index, :] = encoder_output[
            :, self.encoder_cls_token_num :, :
        ].reshape((-1, encoder_output.shape[-1]))
        x_ = x_.reshape((B, -1, encoder_output.shape[-1]))

        # Concatenate the CLS and Possibly Distill tokens from the encoder
        # We can not do it with multi windowed attention though!
        # So remove the CLS token from the decoder!
        if self.use_mwmae_decoder:
            x = x_
            return_cut = 0
        else:
            x = torch.cat(
                [encoder_output[:, : self.encoder_cls_token_num, :], x_], dim=1
            )
            return_cut = self.encoder_cls_token_num
        x = x + self.decoder_pos_embed  # add the pos embeds
        # Pass through transformer blocks
        for blk in self.decoder_blocks:
            x = blk(x)
        x = self.decoder_norm(x)
        pred = self.spec_pred(x)
        pred = pred[:, return_cut:, :]
        return pred



    def _get_segment_representation(self, x, strategy="mean"):
        """Extract audio representation using different strategies."""
        # Put the model in eval mode when getting representations.
        assert x.shape[1] == self.in_channels, f"The GRAM has in channels {self.in_channels}, but the feature has shape {x.shape} which the channels are incompatible"
        B = x.shape[0]
        x = x.transpose(2, 3)
        x = self.spectrogram_normalize(x)
        patches = self.patch_strategy.patch(x)
        patches = patches.flatten(2)
        encoded_patches = self.patch_strategy.embed(x, self.patch_embed)
        mask = torch.zeros((B, self.num_patches), dtype=torch.bool, device=x.device)
        x = self.pass_through_encoder(encoded_patches, ~mask, B)
        if strategy == "mean":
            return x[:, self.encoder_cls_token_num :, :].mean(axis=1)
        elif strategy == "sum":
            return x[:, self.encoder_cls_token_num :, :].sum(axis=1)
        elif strategy == "cls":
            return x[:, 0, :]
        elif strategy == "raw":
            x = x[:, self.encoder_cls_token_num :, :]
            grid_size = self.grid_size
            f, t = grid_size
            # We have 25 time patches in 2 second audio. We need to have 20 for STARSS22.
            outcome = rearrange(
                x, "b (f t) d -> b t (f d)", f=f, d=self.encoder_embedding_dim
            )
            return outcome
        else:
            raise ValueError(f"Strategy '{strategy}' is unrecognized.")

    def get_audio_representation(self, x, strategy = "mean"):
        unit_frames = self.input_length
        cur_frames = x.shape[2]
        pad_frames = unit_frames - (cur_frames % unit_frames)
        if pad_frames > 0:
            # Padding with constant 0s
            pad_arg = (
                0,
                0,
                0,
                pad_frames,
            )  # (channel, channel, height, height, width, width)
            x = torch.nn.functional.pad(x, pad_arg, mode="constant")

        embeddings = []
        # Now get the embeddings of the model.
        for i in range(x.shape[2] // unit_frames):
            x_inp = x[:, :, i * unit_frames : (i + 1) * unit_frames, :]
            with torch.no_grad():
                embedding = self._get_segment_representation(
                        x_inp, strategy=strategy
                    )
            embeddings.append(embedding)
        # Stack the embeddings here if it is raw
        if strategy == "raw":
            x = torch.hstack(embeddings)
            pad_emb_frames = int(embeddings[0].shape[1] * pad_frames / unit_frames)
            if pad_emb_frames > 0:
                x = x[:, :-pad_emb_frames]  # remove padded tail
            return x
        else:
            x = torch.stack(embeddings, dim=1)
            return x
