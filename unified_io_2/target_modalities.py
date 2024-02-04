from dataclasses import dataclass
from typing import Dict, Optional

import torch
import torch.nn as nn
import numpy as np


from unified_io_2.data_utils import trim_or_pad_tf, make_autoregressive_inputs
from unified_io_2.input_modalities import ModalityEncoder
from unified_io_2.seq_features import TargetSequence
# from unified_io_2.dvitvqgan import ViTVQGAN, MlpBlock
from unified_io_2.config import *
from unified_io_2 import layers, config
import tensorflow as tf


TEXT_MODALITY_INDEX = 0
IMAGE_MODALITY_INDEX = 1
AUDIO_MODALITY_INDEX = 2


class TextEmbedder(nn.Module):
  def __init__(self, config):
    super().__init__()
    self.config = config

    cfg = self.config
    self.register_buffer("pos_emb_cache", layers.get_1d_position_embedding(
      cfg.text_pos_emb, cfg.decoder_max_text_length, cfg.emb_dim, cfg.head_dim, True, 1), persistent=False)
    if "llama_rope" in cfg.text_pos_emb:
      self.modality_embedding = nn.Parameter(torch.empty(cfg.emb_dim).normal_(std=0.02))
    
  def forward(self, inputs, shared_embed, mask=None, pos_ids=None, segment_ids=None,
              targets=None, cur_index=None):
    cfg = self.config
    bs = inputs.shape[0]

    if pos_ids is None:
      if cur_index is not None:
        pos_ids = torch.full_like(inputs, cur_index)
      else:
        pos_ids = torch.arange(inputs.shape[1], dtype=torch.int32, device=inputs.device)[None, ...]

    x = shared_embed(inputs)

    pos_emb = self.pos_emb_cache[None, :, :][torch.arange(bs, dtype=pos_ids.dtype, device=pos_ids.device)[:, None], pos_ids]

    if "llama_rope" in cfg.text_pos_emb:
      x += self.modality_embedding[None, None, :].to(x.dtype)

    attn_pattern_mask = torch.ones(
      (bs, 4, x.shape[1], x.shape[1]), dtype=x.dtype, device=x.device)
    modality_id = torch.full((), TEXT_MODALITY_INDEX, device=x.device, dtype=torch.int32)
    return TargetSequence(
      x, pos_emb, modality_id, mask, attn_pattern_mask=attn_pattern_mask,
      subsegments=segment_ids, target_tokens=targets, loss_mask=mask
    )


class TargetTextEncoder(ModalityEncoder):
  """Tokenize and embed input text, handles multiple target texts"""
  def __init__(self):
    super().__init__()

  def preprocess_inputs(self, features, sequence_length) -> Dict:
    text_targets = features.get(f"text_targets")
    if text_targets is None:
      # TODO maybe this should completely empty?
      text_targets = tf.constant([config.EOS_ID], tf.int32)

    max_len = sequence_length[f"text_targets"]
    if text_targets.dtype == tf.dtypes.string:
      raise NotImplementedError("Text must be pre-teoknized")

    text_targets = text_targets[..., :max_len-1]

    if isinstance(text_targets, tf.RaggedTensor):
      raise NotImplementedError()
    else:
      if tf.shape(text_targets)[0] == 0 or text_targets[-1] != config.EOS_ID:
        text_targets = tf.pad(text_targets, paddings=[[0, 1]], constant_values=config.EOS_ID)

    return dict(
      tokens=text_targets,
    )

  def convert_inputs(self, features, sequence_length) -> Dict:
    # Support old style and new style sequence_lengths
    text_len = tf.shape(features["tokens"])[0]
    if text_len is None:
      text_len = sequence_length["targets/text/tokens"]
    # vocab = get_default_vocabulary()
    for k, v in features.items():
      # TODO trimming here is a bit questionable since it might trim EOS, trimming
      # should really happen between tokenization and appending EOS, but keep for now
      # since older versions did this too
      features[k] = trim_or_pad_tf(v, text_len, pad_constant=config.PAD_ID)
    tokens = features.pop("tokens")

    features["targets"] = tokens
    features["inputs"] = make_autoregressive_inputs(
      tokens, sequence_id=features.get("segment_ids"), bos_id=config.BOS_ID)
    features["position_ids"] = tf.range(text_len, dtype=tf.int32)
    features["segment_ids"] = tf.ones((text_len,), dtype=tf.int32)
    features["mask"] = tf.cast(tokens > config.PAD_ID, tf.int32)
    return features

  def get_encoder(self, config: T5Config) -> nn.Module:
    return TextEmbedder(config)


class TargetImageDVAEEmbedder(ModalityEncoder):
  def __init__(self, config):
    super().__init__()
    self.config = config
  def preprocess_inputs(
      self, features: Dict, sequence_length) -> Optional[Dict[str, tf.Tensor]]:
    image_target_size = IMAGE_TARGET_SIZE
    image_target_d = IMAGE_TARGET_D
    target_padding_size = tf.constant(
      np.array(image_target_size) / image_target_d, tf.int32)

    image_targets = features.pop("image_targets", None)
    image_target_masks = features.pop("image_target_masks", None)
    image_target_task_masks = features.pop("image_target_task_masks", None)
    if image_targets is None:
      assert image_target_masks is None
      assert 'image_target_loss_masks' not in features
      assert image_target_task_masks is None
      image_targets = tf.zeros(image_target_size+[0], tf.float32)
      image_target_masks = tf.zeros([0], tf.int32)
      image_target_task_masks = tf.zeros([0], tf.int32)
    else:
      image_targets = image_targets * 2.0 - 1  # VAE pre-processing
      # In case the dimension were unknown
      image_targets = tf.ensure_shape(image_targets, image_target_size + [3])
      assert image_target_masks is not None
      if len(image_target_masks.shape) == 1:
        # Given mask is on the patches rather then pixels, used in depth_preprocessing
        image_target_masks = image_target_masks
      else:
        image_target_masks = tf.image.resize(
          tf.expand_dims(image_target_masks, -1),
          target_padding_size,
          method=tf.image.ResizeMethod.NEAREST_NEIGHBOR)
      image_target_masks = tf.cast(tf.reshape(image_target_masks, [-1]), tf.int32)
      if image_target_task_masks is None:
        image_target_task_masks = tf.zeros(image_target_masks.shape, tf.int32)
      else:
        if len(image_target_task_masks.shape) == 1:
          image_target_task_masks = image_target_task_masks
        else:
          image_target_task_masks = tf.image.resize(
            tf.expand_dims(image_target_task_masks, -1),
            target_padding_size,
            method=tf.image.ResizeMethod.NEAREST_NEIGHBOR)
        image_target_task_masks = tf.cast(tf.reshape(image_target_task_masks, [-1]), tf.int32)

    loss_mask = features.get('image_target_loss_masks', image_target_masks)

    return dict(
      image=image_targets,
      mask=image_target_masks,
      loss_mask=loss_mask,
      task_mask=image_target_task_masks,
    )

  def convert_inputs(self, features: Optional[Dict], sequence_length) -> Dict[str, tf.Tensor]:
    image_len = (IMAGE_TARGET_SIZE[0] // IMAGE_TARGET_D) \
                * (IMAGE_TARGET_SIZE[1] // IMAGE_TARGET_D)
    image_trm_len =  image_len
    image_shape = IMAGE_TARGET_SIZE + [3]
    if tf.shape(features["image"])[-1] == 0:
      features = {
        'image': tf.zeros(image_shape, tf.float32),
        'mask': tf.zeros((image_trm_len,), tf.int32),
        'loss_mask': tf.zeros((image_len,), tf.int32),
        'task_mask': tf.zeros((image_trm_len,), tf.int32),
      }
    features["loss_mask"] = tf.ensure_shape(features["loss_mask"], [image_len])
    features["mask"] = tf.ensure_shape(features["mask"], [image_len])
    features["task_mask"] = tf.ensure_shape(features["task_mask"], [image_len])

    features["image"] = tf.ensure_shape(features["image"], image_shape)
    return features

  def get_encoder(self, config: T5Config) -> nn.Module:
    return ImageViTVQGAN(config, self.config)


class ImageViTVQGAN(nn.Module):
  def __init__(self, config, vae_config, embedding_layer):
    super().__init__()
    self.config = config

  def target_image_to_seq(self, image, loss_mask=None, init=False,
                          task_mask=None):
    pass

  def get_target_sequence(self, input_tokens, mask, target_tokens=None, task_mask=None,
                          loss_mask=None, segment_ids=None, cur_index=None, pos_ids=None):
    pass

  def __call__(self, image, mask=None, loss_mask=None, task_mask=None, init=False, segment_ids=None,
              decode=False, decode_length=None, cur_index=None, pos_ids=None):
    
    cfg = self.config
    if decode:
      return self.get_target_sequence(image, mask, segment_ids, cur_index=cur_index)
    else:
      input_tokens, target_tokens, loss_mask = self.target_image_to_seq(
          image, loss_mask, init, task_mask)

      return self.get_target_sequence(input_tokens, mask, target_tokens, task_mask,
                                      loss_mask, segment_ids, pos_ids=pos_ids)


class TargetAudioDVAEEmbedder(ModalityEncoder):
  def __init__(self, config):
    super().__init__()    
    self.config = config
    
  def get_encoder(self, config: T5Config) -> nn.Module:
    return ImageViTVQGAN(config, self.config)

  def preprocess_inputs(
      self, features: Dict, sequence_length) -> Optional[Dict[str, tf.Tensor]]:

    target_size = AUDIO_TARGET_SIZE
    target_d = AUDIO_TARGET_D

    target_padding_size = tf.constant(
      np.array(target_size) / target_d, tf.int32)

    targets = features.pop("audio_targets", None)
    target_masks = features.pop("audio_target_masks", None)
    target_task_masks = features.pop("audio_target_task_masks", None)

    if targets is None:
      assert target_masks is None
      assert 'audio_target_loss_masks' not in features
      assert target_task_masks is None
      targets = tf.zeros(target_size+[0], tf.float32)
      target_masks = tf.zeros([0], tf.int32)
      target_task_masks = tf.zeros([0], tf.int32)
    else:
      targets = (targets - AUDIOSET_MEAN) / AUDIOSET_STD
      # In case the dimension were unknown
      targets = tf.ensure_shape(targets, target_size + [1])
      assert target_masks is not None
      if len(target_masks.shape) == 1:
        # Given mask is on the patches rather then pixels, used in depth_preprocessing
        target_masks = target_masks
      else:
        target_masks = tf.image.resize(
          tf.expand_dims(target_masks, -1),
          target_padding_size,
          method=tf.image.ResizeMethod.NEAREST_NEIGHBOR)
      target_masks = tf.cast(tf.reshape(target_masks, [-1]), tf.int32)
      if target_task_masks is None:
        target_task_masks = tf.zeros(target_masks.shape, tf.int32)
      else:
        if len(target_task_masks.shape) == 1:
          target_task_masks = target_task_masks
        else:
          target_task_masks = tf.image.resize(
            tf.expand_dims(target_task_masks, -1),
            target_padding_size,
            method=tf.image.ResizeMethod.NEAREST_NEIGHBOR)
          target_task_masks = tf.cast(tf.reshape(target_task_masks, [-1]), tf.int32)

    loss_mask = features.get('audio_target_loss_masks', target_masks)

    return dict(
      audio=targets,
      mask=target_masks,
      loss_mask=loss_mask,
      task_mask=target_task_masks,
    )

  def convert_inputs(self, features: Optional[Dict], sequence_length) -> Dict[str, tf.Tensor]:

    target_len = (AUDIO_TARGET_SIZE[0] // AUDIO_TARGET_D) * (AUDIO_TARGET_SIZE[1] // AUDIO_TARGET_D)
    target_shape = AUDIO_TARGET_SIZE + [1]
    target_trm_len =  target_len

    if features is None or tf.shape(features["audio"])[-1] == 0:
      # Replace dummy features with full-sized masked features to keep shape consistent
      target = tf.zeros(target_shape, tf.float32)
      features = {
        'audio': target,
        'mask': tf.zeros((target_trm_len,), tf.int32),
        'loss_mask': tf.zeros((target_len,), tf.int32),
        'task_mask': tf.zeros((target_trm_len,), tf.int32),
      }

    # If statement can screw up shape info, fix here:
    features["mask"] = tf.ensure_shape(features["mask"], [target_trm_len])
    features["loss_mask"] = tf.ensure_shape(features["loss_mask"], [target_len])
    features["task_mask"] = tf.ensure_shape(features["task_mask"], [target_trm_len])

    features["audio"] = tf.ensure_shape(features["audio"], target_shape)
    return features
