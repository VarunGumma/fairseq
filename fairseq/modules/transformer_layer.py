# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

from typing import Dict, List, Optional

import torch
import torch.nn as nn
from torch import Tensor

from fairseq import utils
from fairseq.models.transformer import TransformerConfig
from fairseq.modules.fairseq_dropout import FairseqDropout
from fairseq.modules.quant_noise import quant_noise
from fairseq.modules import (
    LayerNorm,
    RMSNorm,
    GLU,
    MultiheadAttention,
    FastMultiheadAttention,
    FastGroupedQueryAttention,
)

# BUG: This module does not support `scale_attn`, `scale_heads`, `scale_fc`, `scale_resids` anymore


class TransformerEncoderLayerBase(nn.Module):
    """Encoder layer block.

    In the original paper each operation (multi-head attention or FFN) is
    postprocessed with: `dropout -> add residual -> layernorm`. In the
    tensor2tensor code they suggest that learning is more robust when
    preprocessing each layer with layernorm and postprocessing with:
    `dropout -> add residual`. We default to the approach in the paper, but the
    tensor2tensor approach can be enabled by setting
    *cfg.encoder.normalize_before* to ``True``.

    Args:
        cfg (argparse.Namespace): parsed command-line arguments
    """

    def __init__(self, cfg, return_fc=False):
        super().__init__()
        self.cfg = cfg
        self.return_fc = return_fc
        self.embed_dim = cfg.encoder.embed_dim
        self.quant_noise = cfg.quant_noise.pq
        self.quant_noise_block_size = cfg.quant_noise.pq_block_size
        self.attn_implementation = getattr(cfg, "attn_implementation", None)
        self.self_attn = self.build_self_attention(self.embed_dim, cfg)
        self.self_attn_layer_norm = self.build_normalization(
            self.embed_dim, rms=cfg.encoder.use_rmsnorm
        )

        self.dropout_module = FairseqDropout(
            cfg.dropout, module_name=self.__class__.__name__
        )
        self.activation_dropout_module = FairseqDropout(
            float(cfg.activation_dropout), module_name=self.__class__.__name__
        )
        self.normalize_before = cfg.encoder.normalize_before
        self.use_glu = getattr(cfg.encoder, "use_glu", False)
        self.activation_fn = utils.get_activation_fn(activation=cfg.activation_fn)

        if self.use_glu:
            self.glu = self.build_glu(
                self.embed_dim, cfg.encoder.ffn_embed_dim, self.activation_fn
            )
        else:
            self.fc1 = self.build_fc1(
                self.embed_dim,
                cfg.encoder.ffn_embed_dim,
                self.quant_noise,
                self.quant_noise_block_size,
            )
            self.fc2 = self.build_fc2(
                cfg.encoder.ffn_embed_dim,
                self.embed_dim,
                self.quant_noise,
                self.quant_noise_block_size,
            )

        self.final_layer_norm = self.build_normalization(
            self.embed_dim, rms=cfg.encoder.use_rmsnorm
        )

    def build_fc1(self, input_dim, output_dim, q_noise, qn_block_size):
        return quant_noise(nn.Linear(input_dim, output_dim), q_noise, qn_block_size)

    def build_fc2(self, input_dim, output_dim, q_noise, qn_block_size):
        return quant_noise(nn.Linear(input_dim, output_dim), q_noise, qn_block_size)

    def build_glu(self, input_dim, intermediate_dim, activation_fn, bias=False):
        return GLU(input_dim, intermediate_dim, activation_fn=activation_fn, bias=bias)

    def _get_fc_rank(self, remove_num: int) -> List[int]:
        f1_filter_param = []
        for i in range(self.fc1.out_features):
            f1_filter_param.append(
                torch.sum(torch.abs(self.fc1.weight[i]))
                + torch.sum(torch.abs(self.fc2.weight[:, i]))
                + torch.abs(self.fc1.bias[i])
            )
        return sorted(
            range(len(f1_filter_param)), key=lambda k: f1_filter_param[k], reverse=False
        )[0:remove_num]

    def _prune_fc_layer(self, remove_index: List[int]):
        new_fc1_weight = []
        new_fc1_bias = []
        for i in range(self.fc1.out_features):
            if i not in remove_index:
                new_fc1_weight.append(self.fc1.weight[i])
                new_fc1_bias.append(self.fc1.bias[i])

        new_fc1_weight = torch.stack(new_fc1_weight).detach()
        new_fc1_weight.requires_grad = True

        new_fc1_bias = torch.stack(new_fc1_bias).detach()
        new_fc1_bias.requires_grad = True

        self.fc1 = quant_noise(
            nn.Linear(self.fc1.in_features, self.fc1.out_features - len(remove_index)),
            p=self.quant_noise,
            block_size=self.quant_noise_block_size,
        )
        self.fc1.weight = torch.nn.Parameter(new_fc1_weight)
        self.fc1.bias = torch.nn.Parameter(new_fc1_bias)

        new_fc2_weight = []
        new_fc2_bias = []
        for i in range(self.fc2.in_features):
            if i not in remove_index:
                new_fc2_weight.append(self.fc2.weight[:, i])
        new_fc2_bias = self.fc2.bias.detach()

        new_fc2_weight = torch.stack(new_fc2_weight, dim=-1).detach()
        new_fc2_weight.requires_grad = True

        new_fc2_bias = self.fc2.bias.detach()
        new_fc2_bias.requires_grad = True

        self.fc2 = quant_noise(
            nn.Linear(self.fc2.in_features - len(remove_index), self.fc2.out_features),
            p=self.quant_noise,
            block_size=self.quant_noise_block_size,
        )
        self.fc2.weight = torch.nn.Parameter(new_fc2_weight)
        self.fc2.bias = torch.nn.Parameter(new_fc2_bias)

    def build_self_attention(self, embed_dim, cfg):
        is_fused = self.attn_implementation.endswith("fused")

        if (
            self.attn_implementation.startswith("fast_gqa")
            and getattr(cfg.encoder, "kv_attention_heads", None) is not None
        ):
            return FastGroupedQueryAttention(
                embed_dim,
                cfg.encoder.attention_heads,
                cfg.encoder.kv_attention_heads,
                dropout=cfg.attention_dropout,
                self_attention=True,
                fused_qkv=is_fused,
                q_noise=self.quant_noise,
                qn_block_size=self.quant_noise_block_size,
                rope_args=getattr(cfg, "rope_args", None),
            )
        elif self.attn_implementation.startswith("fast"):
            return FastMultiheadAttention(
                embed_dim,
                cfg.encoder.attention_heads,
                dropout=cfg.attention_dropout,
                self_attention=True,
                fused_qkv=is_fused,
                q_noise=self.quant_noise,
                qn_block_size=self.quant_noise_block_size,
                rope_args=getattr(cfg, "rope_args", None),
            )
        else:
            return MultiheadAttention(
                embed_dim,
                cfg.encoder.attention_heads,
                dropout=cfg.attention_dropout,
                self_attention=True,
                q_noise=self.quant_noise,
                qn_block_size=self.quant_noise_block_size,
                xformers_att_config=cfg.encoder.xformers_att_config,
            )

    def build_normalization(self, dim, rms=False):
        return LayerNorm(dim, export=self.cfg.export) if not rms else RMSNorm(dim)

    def residual_connection(self, x, residual):
        return residual + x

    def upgrade_state_dict_named(self, state_dict, name):
        """
        Rename layer norm states from `...layer_norms.0.weight` to
        `...self_attn_layer_norm.weight` and `...layer_norms.1.weight` to
        `...final_layer_norm.weight`
        """
        layer_norm_map = {"0": "self_attn_layer_norm", "1": "final_layer_norm"}
        for old, new in layer_norm_map.items():
            for m in ("weight", "bias"):
                k = "{}.layer_norms.{}.{}".format(name, old, m)
                if k in state_dict:
                    state_dict["{}.{}.{}".format(name, new, m)] = state_dict[k]
                    del state_dict[k]

    def forward(
        self,
        x,
        encoder_padding_mask: Optional[Tensor],
        attn_mask: Optional[Tensor] = None,
        self_attn_mask: Optional[Tensor] = None,
    ):
        """
        Args:
            x (Tensor): input to the layer of shape `(seq_len, batch, embed_dim)`
            encoder_padding_mask (ByteTensor): binary ByteTensor of shape
                `(batch, seq_len)` where padding elements are indicated by ``1``.
            attn_mask (ByteTensor): binary tensor of shape `(tgt_len, src_len)`,
                where `tgt_len` is the length of output and `src_len` is the
                length of input, though here both are equal to `seq_len`.
                `attn_mask[tgt_i, src_j] = 1` means that when calculating the
                embedding for `tgt_i`, we exclude (mask out) `src_j`. This is
                useful for strided self-attention.
            self_attn_mask (Tensor): tensor of shape broadcastable to `(
                batch * attn_heads, tgt_len, src_len)`, as an additional mask.

        Returns:
            encoded output of shape `(seq_len, batch, embed_dim)`
        """
        # anything in original attn_mask = 1, becomes -1e8
        # anything in original attn_mask = 0, becomes 0
        # Note that we cannot use -inf here, because at some edge cases,
        # the attention weight (before softmax) for some padded element in query
        # will become -inf, which results in NaN in model parameters
        if attn_mask is not None:
            attn_mask = attn_mask.masked_fill(
                attn_mask.to(torch.bool), torch.finfo(x.dtype).min
            )
            if self_attn_mask is not None:
                attn_mask += self_attn_mask
        elif self_attn_mask is not None:
            attn_mask = self_attn_mask

        residual = x
        if self.normalize_before:
            x = self.self_attn_layer_norm(x)

        x, _ = self.self_attn(
            query=x,
            key=x,
            value=x,
            key_padding_mask=encoder_padding_mask,
            need_weights=False,
            attn_mask=attn_mask,
        )
        x = self.dropout_module(x)
        x = self.residual_connection(x, residual)
        if not self.normalize_before:
            x = self.self_attn_layer_norm(x)

        residual = x
        if self.normalize_before:
            x = self.final_layer_norm(x)

        x = (
            self.glu(x)
            if self.use_glu
            else self.fc2(
                self.activation_dropout_module(self.activation_fn(self.fc1(x)))
            )
        )

        fc_result = x

        x = self.dropout_module(x)
        x = self.residual_connection(x, residual)

        if not self.normalize_before:
            x = self.final_layer_norm(x)

        if self.return_fc and not torch.jit.is_scripting():
            return x, fc_result
        return x


# backward compatible with the legacy argparse format
class TransformerEncoderLayer(TransformerEncoderLayerBase):
    def __init__(self, args):
        super().__init__(TransformerConfig.from_namespace(args))
        self.args = args

    def build_self_attention(self, embed_dim, args):
        return super().build_self_attention(
            embed_dim, TransformerConfig.from_namespace(args)
        )


class TransformerDecoderLayerBase(nn.Module):
    """Decoder layer block.

    In the original paper each operation (multi-head attention, encoder
    attention or FFN) is postprocessed with: `dropout -> add residual ->
    layernorm`. In the tensor2tensor code they suggest that learning is more
    robust when preprocessing each layer with layernorm and postprocessing with:
    `dropout -> add residual`. We default to the approach in the paper, but the
    tensor2tensor approach can be enabled by setting
    *cfg.decoder.normalize_before* to ``True``.

    Args:
        args (argparse.Namespace): parsed command-line arguments
        no_encoder_attn (bool, optional): whether to attend to encoder outputs
            (default: False).
    """

    def __init__(
        self, cfg, no_encoder_attn=False, add_bias_kv=False, add_zero_attn=False
    ):
        super().__init__()
        self.cfg = cfg
        self.embed_dim = cfg.decoder.embed_dim
        self.dropout_module = FairseqDropout(
            cfg.dropout, module_name=self.__class__.__name__
        )
        self.quant_noise = cfg.quant_noise.pq
        self.quant_noise_block_size = cfg.quant_noise.pq_block_size

        self.cross_self_attention = cfg.cross_self_attention
        self.attn_implementation = getattr(cfg, "attn_implementation", "fairseq")

        self.self_attn = self.build_self_attention(
            self.embed_dim,
            cfg,
            add_bias_kv=add_bias_kv,
            add_zero_attn=add_zero_attn,
        )
        self.nh = self.self_attn.num_heads
        self.head_dim = self.self_attn.head_dim

        self.activation_dropout_module = FairseqDropout(
            float(cfg.activation_dropout), module_name=self.__class__.__name__
        )
        self.normalize_before = cfg.decoder.normalize_before

        self.self_attn_layer_norm = self.build_normalization(
            self.embed_dim, rms=cfg.decoder.use_rmsnorm
        )

        if no_encoder_attn:
            self.encoder_attn = None
            self.encoder_attn_layer_norm = None
        else:
            self.encoder_attn = self.build_encoder_attention(self.embed_dim, cfg)
            self.encoder_attn_layer_norm = self.build_normalization(
                self.embed_dim, rms=cfg.decoder.use_rmsnorm
            )

        self.use_glu = getattr(cfg.decoder, "use_glu", False)
        self.activation_fn = utils.get_activation_fn(activation=cfg.activation_fn)

        if self.use_glu:
            self.glu = self.build_glu(
                self.embed_dim, cfg.decoder.ffn_embed_dim, self.activation_fn
            )
        else:
            self.fc1 = self.build_fc1(
                self.embed_dim,
                cfg.decoder.ffn_embed_dim,
                self.quant_noise,
                self.quant_noise_block_size,
            )
            self.fc2 = self.build_fc2(
                cfg.decoder.ffn_embed_dim,
                self.embed_dim,
                self.quant_noise,
                self.quant_noise_block_size,
            )

        self.final_layer_norm = self.build_normalization(
            self.embed_dim, rms=cfg.decoder.use_rmsnorm
        )

        self.need_attn = True
        self.onnx_trace = False

    def build_fc1(self, input_dim, output_dim, q_noise, qn_block_size):
        return quant_noise(nn.Linear(input_dim, output_dim), q_noise, qn_block_size)

    def build_fc2(self, input_dim, output_dim, q_noise, qn_block_size):
        return quant_noise(nn.Linear(input_dim, output_dim), q_noise, qn_block_size)

    def build_glu(self, input_dim, intermediate_dim, activation_fn, bias=False):
        return GLU(input_dim, intermediate_dim, activation_fn=activation_fn, bias=bias)

    def build_self_attention(
        self, embed_dim, cfg, add_bias_kv=False, add_zero_attn=False
    ):
        is_fused = self.attn_implementation.endswith("fused")

        if (
            self.attn_implementation.startswith("fast_gqa")
            and getattr(cfg.decoder, "kv_attention_heads", None) is not None
        ):
            return FastGroupedQueryAttention(
                embed_dim,
                cfg.decoder.attention_heads,
                cfg.decoder.kv_attention_heads,
                dropout=cfg.attention_dropout,
                add_bias_kv=add_bias_kv,
                add_zero_attn=add_zero_attn,
                self_attention=True,
                is_decoder=True,
                fused_qkv=is_fused,
                q_noise=self.quant_noise,
                qn_block_size=self.quant_noise_block_size,
                rope_args=getattr(cfg, "rope_args", None),
            )
        elif self.attn_implementation.startswith("fast"):
            return FastMultiheadAttention(
                embed_dim,
                cfg.decoder.attention_heads,
                dropout=cfg.attention_dropout,
                add_bias_kv=add_bias_kv,
                add_zero_attn=add_zero_attn,
                self_attention=True,
                is_decoder=True,
                fused_qkv=is_fused,
                q_noise=self.quant_noise,
                qn_block_size=self.quant_noise_block_size,
                rope_args=getattr(cfg, "rope_args", None),
            )
        else:
            return MultiheadAttention(
                embed_dim,
                cfg.decoder.attention_heads,
                dropout=cfg.attention_dropout,
                add_bias_kv=add_bias_kv,
                add_zero_attn=add_zero_attn,
                self_attention=True,
                q_noise=self.quant_noise,
                qn_block_size=self.quant_noise_block_size,
                xformers_att_config=cfg.decoder.xformers_att_config,
            )

    def build_encoder_attention(self, embed_dim, cfg):
        is_fused = self.attn_implementation.endswith("fused")
        # GQA is not supported with encoder-decoder attention
        # it only works for self-attention
        if self.attn_implementation.startswith("fast"):
            return FastMultiheadAttention(
                embed_dim,
                cfg.decoder.attention_heads,
                kdim=cfg.encoder.embed_dim,
                vdim=cfg.encoder.embed_dim,
                dropout=cfg.attention_dropout,
                encoder_decoder_attention=True,
                is_decoder=True,
                fused_qkv=is_fused,
                q_noise=self.quant_noise,
                qn_block_size=self.quant_noise_block_size,
            )
        else:
            return MultiheadAttention(
                embed_dim,
                cfg.decoder.attention_heads,
                kdim=cfg.encoder.embed_dim,
                vdim=cfg.encoder.embed_dim,
                dropout=cfg.attention_dropout,
                encoder_decoder_attention=True,
                q_noise=self.quant_noise,
                qn_block_size=self.quant_noise_block_size,
                xformers_att_config=cfg.encoder.xformers_att_config,
            )

    def prepare_for_onnx_export_(self):
        self.onnx_trace = True

    def residual_connection(self, x, residual):
        return residual + x

    def build_normalization(self, dim, rms=False):
        return LayerNorm(dim, export=self.cfg.export) if not rms else RMSNorm(dim)

    def forward(
        self,
        x,
        encoder_out: Optional[torch.Tensor] = None,
        encoder_padding_mask: Optional[torch.Tensor] = None,
        incremental_state: Optional[Dict[str, Dict[str, Optional[Tensor]]]] = None,
        prev_self_attn_state: Optional[List[torch.Tensor]] = None,
        prev_attn_state: Optional[List[torch.Tensor]] = None,
        self_attn_mask: Optional[torch.Tensor] = None,
        self_attn_padding_mask: Optional[torch.Tensor] = None,
        need_attn: bool = False,
        need_head_weights: bool = False,
    ):
        """
        Args:
            x (Tensor): input to the layer of shape `(seq_len, batch, embed_dim)`
            encoder_padding_mask (ByteTensor, optional): binary
                ByteTensor of shape `(batch, src_len)` where padding
                elements are indicated by ``1``.
            need_attn (bool, optional): return attention weights
            need_head_weights (bool, optional): return attention weights
                for each head (default: return average over heads).

        Returns:
            encoded output of shape `(seq_len, batch, embed_dim)`
        """
        if need_head_weights:
            need_attn = True

        residual = x
        if self.normalize_before:
            x = self.self_attn_layer_norm(x)

        if prev_self_attn_state is not None:
            prev_key, prev_value = prev_self_attn_state[:2]
            saved_state: Dict[str, Optional[Tensor]] = {
                "prev_key": prev_key,
                "prev_value": prev_value,
            }
            if len(prev_self_attn_state) >= 3:
                saved_state["prev_key_padding_mask"] = prev_self_attn_state[2]
            assert incremental_state is not None
            self.self_attn._set_input_buffer(incremental_state, saved_state)
        _self_attn_input_buffer = self.self_attn._get_input_buffer(incremental_state)

        if self.cross_self_attention and not (
            incremental_state is not None
            and _self_attn_input_buffer is not None
            and "prev_key" in _self_attn_input_buffer
        ):
            if self_attn_mask is not None:
                assert encoder_out is not None
                self_attn_mask = torch.cat(
                    (x.new_zeros(x.size(0), encoder_out.size(0)), self_attn_mask), dim=1
                )
            if self_attn_padding_mask is not None:
                if encoder_padding_mask is None:
                    assert encoder_out is not None
                    encoder_padding_mask = self_attn_padding_mask.new_zeros(
                        encoder_out.size(1), encoder_out.size(0)
                    )
                self_attn_padding_mask = torch.cat(
                    (encoder_padding_mask, self_attn_padding_mask), dim=1
                )
            assert encoder_out is not None
            y = torch.cat((encoder_out, x), dim=0)
        else:
            y = x

        x, attn = self.self_attn(
            query=x,
            key=y,
            value=y,
            key_padding_mask=self_attn_padding_mask,
            incremental_state=incremental_state,
            need_weights=False,
            attn_mask=self_attn_mask,
        )
        x = self.dropout_module(x)
        x = self.residual_connection(x, residual)
        if not self.normalize_before:
            x = self.self_attn_layer_norm(x)

        if self.encoder_attn is not None and encoder_out is not None:
            residual = x
            if self.normalize_before:
                x = self.encoder_attn_layer_norm(x)
            if prev_attn_state is not None:
                prev_key, prev_value = prev_attn_state[:2]
                saved_state: Dict[str, Optional[Tensor]] = {
                    "prev_key": prev_key,
                    "prev_value": prev_value,
                }
                if len(prev_attn_state) >= 3:
                    saved_state["prev_key_padding_mask"] = prev_attn_state[2]
                assert incremental_state is not None
                self.encoder_attn._set_input_buffer(incremental_state, saved_state)

            x, attn = self.encoder_attn(
                query=x,
                key=encoder_out,
                value=encoder_out,
                key_padding_mask=encoder_padding_mask,
                incremental_state=incremental_state,
                static_kv=True,
                need_weights=need_attn or (not self.training and self.need_attn),
                need_head_weights=need_head_weights,
            )
            x = self.dropout_module(x)
            x = self.residual_connection(x, residual)
            if not self.normalize_before:
                x = self.encoder_attn_layer_norm(x)

        residual = x
        if self.normalize_before:
            x = self.final_layer_norm(x)

        x = (
            self.glu(x)
            if self.use_glu
            else self.fc2(
                self.activation_dropout_module(self.activation_fn(self.fc1(x)))
            )
        )

        x = self.dropout_module(x)
        x = self.residual_connection(x, residual)

        if not self.normalize_before:
            x = self.final_layer_norm(x)

        if self.onnx_trace and incremental_state is not None:
            saved_state = self.self_attn._get_input_buffer(incremental_state)
            assert saved_state is not None
            if self_attn_padding_mask is not None:
                self_attn_state = [
                    saved_state["prev_key"],
                    saved_state["prev_value"],
                    saved_state["prev_key_padding_mask"],
                ]
            else:
                self_attn_state = [saved_state["prev_key"], saved_state["prev_value"]]
            return x, attn, self_attn_state
        return x, attn, None

    def make_generation_fast_(self, need_attn: bool = False, **kwargs):
        self.need_attn = need_attn


# backward compatible with the legacy argparse format
class TransformerDecoderLayer(TransformerDecoderLayerBase):
    def __init__(
        self, args, no_encoder_attn=False, add_bias_kv=False, add_zero_attn=False
    ):
        super().__init__(
            TransformerConfig.from_namespace(args),
            no_encoder_attn=no_encoder_attn,
            add_bias_kv=add_bias_kv,
            add_zero_attn=add_zero_attn,
        )
        self.args = args

    def build_self_attention(
        self, embed_dim, args, add_bias_kv=False, add_zero_attn=False
    ):
        return super().build_self_attention(
            embed_dim,
            TransformerConfig.from_namespace(args),
            add_bias_kv=add_bias_kv,
            add_zero_attn=add_zero_attn,
        )

    def build_encoder_attention(self, embed_dim, args):
        return super().build_encoder_attention(
            embed_dim,
            TransformerConfig.from_namespace(args),
        )
