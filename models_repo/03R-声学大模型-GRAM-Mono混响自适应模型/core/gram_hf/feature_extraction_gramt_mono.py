from typing import Optional, Union
import numpy as np

from transformers import SequenceFeatureExtractor
from transformers import BatchFeature
from transformers.utils import TensorType 

import torch 
import torchaudio

from typing import List 



class MonoFeatureExtractor(SequenceFeatureExtractor):
    r"""
    Constructs a Audio Spectrogram Transformer (AST) feature extractor.

    This feature extractor inherits from [`~feature_extraction_sequence_utils.SequenceFeatureExtractor`] which contains
    most of the main methods. Users should refer to this superclass for more information regarding those methods.

    This class extracts mel-filter bank features from raw speech using TorchAudio if installed or using numpy
    otherwise, pads/truncates them to a fixed length and normalizes them using a mean and standard deviation.

    Args:
        feature_size (`int`, *optional*, defaults to 1):
            The feature dimension of the extracted features.
        sampling_rate (`int`, *optional*, defaults to 16000):
            The sampling rate at which the audio files should be digitalized expressed in hertz (Hz).
        num_mel_bins (`int`, *optional*, defaults to 128):
            Number of Mel-frequency bins.
        max_length (`int`, *optional*, defaults to 1024):
            Maximum length to which to pad/truncate the extracted features

    """

    def __init__(
        self,
        feature_size=1,
        sampling_rate=32000,
        num_mel_bins=128,
        padding_value=0.0,
        **kwargs,
    ):
        super().__init__(feature_size=feature_size, sampling_rate=sampling_rate, padding_value=padding_value, **kwargs)
        self.num_mel_bins = num_mel_bins

    def _extract_fbank_features(
        self,
        waveform: np.ndarray,
        normalize: bool,
    ) -> np.ndarray:
        """
        Get mel-filter bank features using TorchAudio. Note that TorchAudio requires 16-bit signed integers as inputs
        and hence the waveform should not be normalized before feature extraction.
        """
        melspec = torchaudio.transforms.MelSpectrogram(
                sample_rate=self.sampling_rate,
                n_fft=1024,
                win_length=1024,
                hop_length=320,
                f_min=50,
                f_max=self.sampling_rate // 2,
                n_mels=self.num_mel_bins,
                power=2.0,
            )
        waveform = torch.tensor(waveform.clone().detach())
        melspec.to(waveform.device)
        if normalize:
            waveform = self._normalize_audio(waveform)
        # If waveform has two channels, but the channel information is not the first dimension, transpose.
        if (waveform.ndim == 2) and (waveform.shape[0] > 100):
            waveform = waveform.transpose(1, 0)
        if waveform.ndim == 1:
            waveform = waveform.unsqueeze(0)
        # Handle stereo/mono channels consistently
        if waveform.shape[0] == 1:
            mel = melspec(waveform.squeeze()).transpose(1, 0).unsqueeze(0)
            log_mel = (mel + torch.finfo().eps).log() 
            return log_mel
        elif waveform.shape[0] == 2:
            waveform = waveform.mean(axis = 0).unsqueeze(0)
            mel = melspec(waveform).transpose(2, 1)
            log_mel = (mel + torch.finfo().eps).log()       
            return log_mel
        elif waveform.shape[0] == 4:
            mel = melspec(waveform[[0]]).transpose(2, 1)
            log_mel = (mel + torch.finfo().eps).log()
            return log_mel
        else:
            raise Exception("Unknown channel count")

    def _normalize_audio(self, audio_data, target_dBFS=-14.0):
        rms = torch.sqrt(torch.mean(audio_data**2))  # Calculate the RMS of the audio
        if rms == 0:  # Avoid division by zero in case of a completely silent audio
            return audio_data
        current_dBFS = 20 * torch.log10(rms)  # Convert RMS to dBFS
        gain_dB = target_dBFS - current_dBFS  # Calculate the required gain in dB
        gain_linear = 10 ** (gain_dB / 20)  # Convert gain from dB to linear scale
        normalized_audio = audio_data * gain_linear  # Apply the gain to the audio data
        return normalized_audio

    def __call__(
        self,
        raw_speech: Union[np.ndarray, List[float], List[np.ndarray], List[List[float]]],
        sampling_rate: Optional[int] = None,
        return_tensors: Optional[Union[str, TensorType]] = None,
        normalize : bool = True,
        **kwargs,
    ) -> BatchFeature:
        """
        Main method to featurize and prepare for the model one or several sequence(s).

        Args:
            raw_speech (`np.ndarray`, `list[float]`, `list[np.ndarray]`, `list[list[float]]`):
                The sequence or batch of sequences to be padded. Each sequence can be a numpy array, a list of float
                values, a list of numpy arrays or a list of list of float values.

            sampling_rate (`int`, *optional*):
                The sampling rate at which the `raw_speech` input was sampled. It is strongly recommended to pass
                `sampling_rate` at the forward call to prevent silent errors.
            return_tensors (`str` or [`~utils.TensorType`], *optional*):
                If set, will return tensors instead of list of python integers. Acceptable values are:

                - `'tf'`: Return TensorFlow `tf.constant` objects.
                - `'pt'`: Return PyTorch `torch.Tensor` objects.
                - `'np'`: Return Numpy `np.ndarray` objects.
        """

        if sampling_rate is not None:
            if sampling_rate != self.sampling_rate:
                raise ValueError(
                    f"The model corresponding to this feature extractor: {self} was trained using a sampling rate of"
                    f" {self.sampling_rate}. Please make sure that the provided `raw_speech` input was sampled with"
                    f" {self.sampling_rate} and not {sampling_rate}."
                )

        # extract fbank features and pad/truncate to max_length
        features = [self._extract_fbank_features(waveform, normalize) for waveform in raw_speech]
        features = torch.nn.utils.rnn.pad_sequence(features, batch_first=True)
        inputs = BatchFeature({"input_values": features})
        return inputs


