import argparse
import torch
import numpy as np
from torch import nn
from typing import Optional, Union, Tuple
from transformers.models.llama import LlamaPreTrainedModel, LlamaForCausalLM, LlamaConfig
from transformers.models.llama.modeling_llama import LlamaRMSNorm, LlamaRotaryEmbedding
from transformers import Cache
from transformers.modeling_outputs import CausalLMOutputWithPast, BaseModelOutputWithPast
from transformers.activations import ACT2FN
from ...modeling_utils import TPIPreTrainedModel
from ...memory import MemoryManager
from ...split import get_heads_per_node
from ...distributed import DistributedCommPrimitive


def _prepare_4d_causal_attention_mask_with_cache_position(
    attention_mask: torch.Tensor,
    sequence_length: int,
    target_length: int,
    dtype: torch.dtype,
    device: torch.device,
    min_dtype: float,
    cache_position: torch.Tensor,
    batch_size: int,
):
    """
    Creates a causal 4D mask of shape `(batch_size, 1, query_length, key_value_length)` from a 2D mask of shape
    `(batch_size, key_value_length)`, or if the input `attention_mask` is already 4D, do nothing.

    Args:
        attention_mask (`torch.Tensor`):
            A 2D attention mask of shape `(batch_size, key_value_length)` or a 4D attention mask of shape `(batch_size, 1, query_length, key_value_length)`.
        sequence_length (`int`):
            The sequence length being processed.
        target_length (`int`):
            The target length: when generating with static cache, the mask should be as long as the static cache, to account for the 0 padding, the part of the cache that is not filled yet.
        dtype (`torch.dtype`):
            The dtype to use for the 4D attention mask.
        device (`torch.device`):
            The device to plcae the 4D attention mask on.
        min_dtype (`float`):
            The minimum value representable with the dtype `dtype`.
        cache_position (`torch.Tensor`):
            Indices depicting the position of the input sequence tokens in the sequence.
        batch_size (`torch.Tensor`):
            Batch size.
    """
    if attention_mask is not None and attention_mask.dim() == 4:
        # the mask comes already in inverted form
        causal_mask = attention_mask
    else:
        causal_mask = torch.full((sequence_length, target_length), fill_value=min_dtype, dtype=dtype, device=device)
        if sequence_length != 1:
            causal_mask = torch.triu(causal_mask, diagonal=1)
        causal_mask *= torch.arange(target_length, device=device) > cache_position.reshape(-1, 1)
        causal_mask = causal_mask[None, None, :, :].expand(batch_size, 1, -1, -1)
        if attention_mask is not None:
            causal_mask = causal_mask.clone()  # copy to contiguous memory for in-place edit
            mask_length = attention_mask.shape[-1]
            padding_mask = causal_mask[:, :, :, :mask_length] + attention_mask[:, None, None, :]
            padding_mask = padding_mask == 0
            causal_mask[:, :, :, :mask_length] = causal_mask[:, :, :, :mask_length].masked_fill(
                padding_mask, min_dtype
            )
    return causal_mask


def rotate_half(x):
    """Rotates half the hidden dims of the input."""
    x1 = x[..., : x.shape[-1] // 2]
    x2 = x[..., x.shape[-1] // 2 :]
    return torch.cat((-x2, x1), dim=-1)


def apply_rotary_pos_emb(q, k, cos, sin, unsqueeze_dim=1):
    """Applies Rotary Position Embedding to the query and key tensors.

    Args:
        q (`torch.Tensor`): The query tensor.
        k (`torch.Tensor`): The key tensor.
        cos (`torch.Tensor`): The cosine part of the rotary embedding.
        sin (`torch.Tensor`): The sine part of the rotary embedding.
        unsqueeze_dim (`int`, *optional*, defaults to 1):
            The 'unsqueeze_dim' argument specifies the dimension along which to unsqueeze cos[position_ids] and
            sin[position_ids] so that they can be properly broadcasted to the dimensions of q and k. For example, note
            that cos[position_ids] and sin[position_ids] have the shape [batch_size, seq_len, head_dim]. Then, if q and
            k have the shape [batch_size, heads, seq_len, head_dim], then setting unsqueeze_dim=1 makes
            cos[position_ids] and sin[position_ids] broadcastable to the shapes of q and k. Similarly, if q and k have
            the shape [batch_size, seq_len, heads, head_dim], then set unsqueeze_dim=2.
    Returns:
        `tuple(torch.Tensor)` comprising of the query and key tensors rotated using the Rotary Position Embedding.
    """
    cos = cos.unsqueeze(unsqueeze_dim)
    sin = sin.unsqueeze(unsqueeze_dim)
    q_embed = (q * cos) + (rotate_half(q) * sin)
    k_embed = (k * cos) + (rotate_half(k) * sin)
    return q_embed, k_embed


def repeat_kv(hidden_states: torch.Tensor, n_rep: int) -> torch.Tensor:
    """
    This is the equivalent of torch.repeat_interleave(x, dim=1, repeats=n_rep). The hidden states go from (batch,
    num_key_value_heads, seqlen, head_dim) to (batch, num_attention_heads, seqlen, head_dim)
    """
    batch, num_key_value_heads, slen, head_dim = hidden_states.shape
    if n_rep == 1:
        return hidden_states
    hidden_states = hidden_states[:, :, None, :, :].expand(batch, num_key_value_heads, n_rep, slen, head_dim)
    return hidden_states.reshape(batch, num_key_value_heads * n_rep, slen, head_dim)


class TPILlamaAttention(nn.Module):

    def __init__(
        self,
        config: LlamaConfig,
        layer_idx: int,
        num_heads: int,
        num_kv_heads: int,
        head_dim: int
    ):
        super().__init__()
        self.config = config
        self.layer_idx = layer_idx
        self.attention_dropout = config.attention_dropout
        self.hidden_size = config.hidden_size
        self.num_heads = num_heads
        self.head_dim = head_dim
        self.num_key_value_heads = num_kv_heads
        self.num_key_value_groups = self.num_heads // self.num_key_value_heads
        self.max_position_embeddings = config.max_position_embeddings
        self.rope_theta = config.rope_theta
        self.is_causal = True

        self.q_proj = nn.Linear(self.hidden_size, self.num_heads * self.head_dim, bias=config.attention_bias)
        self.k_proj = nn.Linear(self.hidden_size, self.num_key_value_heads * self.head_dim, bias=config.attention_bias)
        self.v_proj = nn.Linear(self.hidden_size, self.num_key_value_heads * self.head_dim, bias=config.attention_bias)
        self.o_proj = nn.Linear(self.num_heads * self.head_dim, self.hidden_size, bias=config.attention_bias)

        self.rotary_emb = LlamaRotaryEmbedding(config=self.config)


class TPILlamaSdpaAttention(TPILlamaAttention):

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        past_key_value: Optional[Cache] = None,
        cache_position: Optional[torch.LongTensor] = None,
        position_embeddings: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        **kwargs,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        bsz, q_len, _ = hidden_states.size()

        query_states = self.q_proj(hidden_states)
        key_states = self.k_proj(hidden_states)
        value_states = self.v_proj(hidden_states)

        query_states = query_states.view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)
        key_states = key_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)
        value_states = value_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)

        cos, sin = position_embeddings
        query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos, sin)

        cache_kwargs = {"sin": sin, "cos": cos, "cache_position": cache_position}
        key_states, value_states = past_key_value.update(key_states, value_states, self.layer_idx, cache_kwargs)

        key_states = repeat_kv(key_states, self.num_key_value_groups)
        value_states = repeat_kv(value_states, self.num_key_value_groups)

        causal_mask = attention_mask
        causal_mask = causal_mask[:, :, :, : key_states.shape[-2]]

        if query_states.device.type == "cuda" and causal_mask is not None:
            query_states = query_states.contiguous()
            key_states = key_states.contiguous()
            value_states = value_states.contiguous()

        is_causal = True if causal_mask is None and q_len > 1 else False

        attn_output = torch.nn.functional.scaled_dot_product_attention(
            query_states,
            key_states,
            value_states,
            attn_mask=causal_mask,
            dropout_p=0.,
            is_causal=is_causal,
        )

        attn_output = attn_output.transpose(1, 2).contiguous()
        attn_output = attn_output.view(bsz, q_len, -1)

        attn_output = self.o_proj(attn_output)

        return attn_output, None, past_key_value


class TPILlamaMLP(nn.Module):

    def __init__(self, config: LlamaConfig, rank: int, split_dim: int):
        super().__init__()
        self.config = config
        self.rank = rank
        self.hidden_size = config.hidden_size
        self.intermediate_size = split_dim
        self.gate_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=config.mlp_bias)
        self.up_proj = nn.Linear(self.hidden_size, self.intermediate_size, bias=config.mlp_bias)
        self.down_proj = nn.Linear(self.intermediate_size, self.hidden_size, bias=config.mlp_bias)
        self.act_fn = ACT2FN[config.hidden_act]

    def forward(self, x):
        return self.down_proj(self.act_fn(self.gate_proj(x)) * self.up_proj(x))


class TPILlamaDecoderLayer(nn.Module):

    def __init__(
        self,
        config: LlamaConfig,
        layer_idx: int,
        rank: int,
        mem_manager: MemoryManager,
        args: argparse.Namespace
    ):
        super().__init__()
        self.config = config
        self.layer_idx = layer_idx
        self.rank = rank
        self.mem_manager = mem_manager

        self.hidden_size = config.hidden_size
        head_dim = config.hidden_size // config.num_attention_heads
        if (head_dim * config.num_attention_heads) != self.hidden_size:
            raise ValueError(
                f"`hidden_size` must be divisible by the number of attention heads "
                f"(got `hidden_size`: {self.hidden_size} and `num_heads`: {config.num_attention_heads})."
            )

        heads_per_node, kv_heads_per_node = get_heads_per_node(
            world_size=args.world_size,
            ratio=args.ratio,
            num_heads=config.num_attention_heads,
            num_kv_heads=config.num_key_value_heads
        )
        self.self_attn = TPILlamaSdpaAttention(
            config=config,
            layer_idx=layer_idx,
            num_heads=heads_per_node[rank],
            num_kv_heads=kv_heads_per_node[rank],
            head_dim=head_dim,
        )

        split_dims = (np.array(heads_per_node) * config.intermediate_size // sum(heads_per_node)).tolist()
        if sum(split_dims) != config.intermediate_size:
            raise ValueError(
                "The sum of `split_dims` must be equal to `intermediate_size` "
                f"(got `split_dims`: {split_dims}) and `intermediate_size`: {config.intermediate_size}.)"
            )
        self.mlp = TPILlamaMLP(config, rank, split_dims[rank])

        self.input_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(
        self,
        hidden_states: torch.Tensor,
        attention_mask: Optional[torch.Tensor] = None,
        past_key_value: Optional[Cache] = None,
        use_cache: Optional[bool] = False,
        cache_position: Optional[torch.LongTensor] = None,
        position_embeddings: Optional[Tuple[torch.Tensor, torch.Tensor]] = None,
        **kwargs,
    ) -> Tuple[torch.FloatTensor, Optional[Tuple[torch.FloatTensor, torch.FloatTensor]]]:
        self.mem_manager.track(f"self_attn.{self.layer_idx}")

        residual = hidden_states

        hidden_states = self.input_layernorm(hidden_states)

        hidden_states, self_attn_weights, present_key_value = self.self_attn(
            hidden_states=hidden_states,
            attention_mask=attention_mask,
            past_key_value=past_key_value,
            use_cache=use_cache,
            cache_position=cache_position,
            position_embeddings=position_embeddings,
            **kwargs,
        )

        # perform allreduce to sum up hidden_states, meanwhile load next blocks
        comm_handler = DistributedCommPrimitive.allreduce(hidden_states, async_op=True)
        self.mem_manager.track(f"mlp.{self.layer_idx}")
        comm_handler.wait()

        hidden_states = residual + hidden_states

        # Fully Connected
        residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states = self.mlp(hidden_states)

        # perform allreduce to sum up hidden_states, meanwhile load next blocks
        comm_handler = DistributedCommPrimitive.allreduce(hidden_states, async_op=True)
        self.mem_manager.track(f"self_attn.{self.layer_idx + 1}")
        comm_handler.wait()

        hidden_states = residual + hidden_states

        outputs = (hidden_states,)

        if use_cache:
            outputs += (present_key_value,)

        return outputs


class TPILlamaPreTrainedModel(LlamaPreTrainedModel, TPIPreTrainedModel):
    pass


class TPILlamaModel(TPILlamaPreTrainedModel):

    def __init__(
        self,
        config: LlamaConfig,
        rank: int,
        mem_manager: MemoryManager,
        args: argparse.Namespace
    ):
        super().__init__(config)
        self.rank = rank
        self.mem_manager = mem_manager
        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size

        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size, self.padding_idx)
        self.layers = nn.ModuleList([
            TPILlamaDecoderLayer(config, layer_idx, rank, mem_manager, args)
            for layer_idx in range(config.num_hidden_layers)]
        )
        self.norm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.rotary_emb = LlamaRotaryEmbedding(config=config)

    def _update_causal_mask(
        self,
        attention_mask: torch.Tensor,
        input_tensor: torch.Tensor,
        cache_position: torch.Tensor,
        past_key_values: Cache,
    ):
        past_seen_tokens = past_key_values.get_seq_length() if past_key_values is not None else 0
        dtype, device = input_tensor.dtype, input_tensor.device
        min_dtype = torch.finfo(dtype).min
        sequence_length = input_tensor.shape[1]
        target_length = (
            attention_mask.shape[-1]
            if isinstance(attention_mask, torch.Tensor)
            else past_seen_tokens + sequence_length + 1
        )
        # In case the provided `attention` mask is 2D, we generate a causal mask here (4D).
        causal_mask = _prepare_4d_causal_attention_mask_with_cache_position(
            attention_mask,
            sequence_length=sequence_length,
            target_length=target_length,
            dtype=dtype,
            device=device,
            min_dtype=min_dtype,
            cache_position=cache_position,
            batch_size=input_tensor.shape[0],
        )
        return causal_mask

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Cache] = None,
        use_cache: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
    ) -> Union[Tuple, BaseModelOutputWithPast]:
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        inputs_embeds = None
        position_embeddings = None
        causal_mask = None
        if self.rank == 0:
            # notify the memory manager to load input embedding weights.
            self.mem_manager.track("input")
            inputs_embeds = self.embed_tokens(input_ids)
            # tuple of 2 tensors with shape (bs, seq_len, head_dim)
            position_embeddings = self.rotary_emb(inputs_embeds, position_ids)
            causal_mask = self._update_causal_mask(
                attention_mask, inputs_embeds, cache_position, past_key_values)

        # the master node broadcasts inputs_embeds, position_embedding, cache_position,
        # and causal_mask to all other nodes.
        # shape of inputs_embeds: (bs, seq_len, hidden_size)
        # shape of position_embeddings: 2 Tuples of shape (bs, seq_len, head_dim)
        # shape of cache_position: (seq_len,)
        # shape of causal_mask: (1, 1, seq_len, seq_len)
        broadcast_data = [inputs_embeds, position_embeddings, cache_position, causal_mask]
        DistributedCommPrimitive.broadcast(broadcast_data, src=0)
        inputs_embeds, position_embeddings, cache_position, causal_mask = broadcast_data

        # decoder layers
        next_decoder_cache = None
        hidden_states = inputs_embeds
        del inputs_embeds

        for decoder_layer in self.layers:
            layer_outputs = decoder_layer(
                hidden_states,
                attention_mask=causal_mask,
                past_key_value=past_key_values,
                use_cache=use_cache,
                cache_position=cache_position,
                position_embeddings=position_embeddings,
            )

            hidden_states = layer_outputs[0]

            if use_cache:
                next_decoder_cache = layer_outputs[1]

        if self.rank == 0:
            hidden_states = self.norm(hidden_states)

        next_cache = next_decoder_cache

        if not return_dict:
            return tuple(v for v in [hidden_states, next_cache] if v is not None)
        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states, past_key_values=next_cache)


class TPILlamaForCausalLM(LlamaForCausalLM, TPILlamaPreTrainedModel):
    """
    TPILlamaForCausalLM is an implementation of a causal language model based on Llama architecture,
    integrating TPI (Tensor Parallel Inference) capabilities. Only the master node (rank 0) returns
    output logits.
    """

    def __init__(
        self,
        config: LlamaConfig,
        rank: int,
        args: argparse.Namespace
    ):
        super().__init__(config)
        self.rank = rank
        self.mem_manager = MemoryManager(self, rank, args)
        self.model = TPILlamaModel(config, rank, self.mem_manager, args)

    def forward(
        self,
        input_ids: torch.LongTensor = None,
        attention_mask: Optional[torch.Tensor] = None,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional[Cache] = None,
        use_cache: Optional[bool] = None,
        return_dict: Optional[bool] = None,
        cache_position: Optional[torch.LongTensor] = None,
        **kwargs
    ) -> Union[Tuple, CausalLMOutputWithPast]:
        """
        Perform a forward pass through the model to generate logits for the next token in a sequence.

        Parameters:
            input_ids (torch.LongTensor, optional):
                Tensor of input token IDs of shape `(batch_size, sequence_length)`.
            attention_mask (torch.Tensor, optional):
                Tensor used to avoid performing attention on padding tokens.
            position_ids (torch.LongTensor, optional):
                Tensor of positional indices for each token in the input.
            past_key_values (Cache, optional):
                Contains precomputed key and value states for caching during generation.
            use_cache (bool, optional):
                Whether to use cached key/value states to speed up decoding.
            return_dict (bool, optional):
                If True, returns a `CausalLMOutputWithPast` object instead of a plain tuple.
            cache_position (torch.LongTensor, optional):
                Position indices within the cache, used in distributed settings.
            kwargs (dict, optional): Additional keyword arguments.

        Returns:
            Union[Tuple, CausalLMOutputWithPast]:
                Depending on the `return_dict` flag, returns either a tuple or a `CausalLMOutputWithPast` object.
                If the current process is not the master node (rank != 0), returns `None`.
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        # forward pass through the core model.
        # decoder outputs consists of (dec_features, layer_state, dec_hidden, dec_attn).
        outputs = self.model(
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            use_cache=use_cache,
            return_dict=return_dict,
            cache_position=cache_position,
        )

        my_rank = self.rank

        # non-master nodes return nothing.
        if my_rank != 0:
            return None

        # master node returns output logits, past kv cache, and other info if needed.
        logits = self.lm_head(outputs[0]).float()

        if not return_dict:
            return (logits,) + outputs[1:]

        return CausalLMOutputWithPast(
            logits=logits,
            past_key_values=outputs.past_key_values,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )
