import IPython
if IPython.get_ipython() is not None:
    IPython.get_ipython().magic('load_ext autoreload')
    IPython.get_ipython().magic('autoreload 2')
        
from typing import Literal, List, Tuple, Dict, Any, Optional, Union, Callable, TypeVar, Iterable, Set
from acdc import HookedTransformer, HookedTransformerConfig
import warnings
from collections import OrderedDict
import einops
import torch
import numpy as np
from functools import partial
from acdc.acdc_utils import kl_divergence
from tracr.rasp import rasp
from tracr.compiler import compiling
import torch.nn.functional as F

bos = "BOS"

def get_tracr_model_input_and_tl_model(task: Literal["reverse", "proportion"], return_im = False, sixteen_heads=False):
    """
    This function adapts Neel's TransformerLens porting of tracr
    """

    # Loads an example RASP program model. This program reverses lists. The model takes as input a list of pre-tokenization elements (here `["BOS", 1, 2, 3]`), these are tokenized (`[3, 0, 1, 2]`), the transformer is applied, and then an argmax is taken over the output and it is detokenized - this can be seen on the `out.decoded` attribute of the output

    def make_length():
        all_true_selector = rasp.Select(rasp.tokens, rasp.tokens, rasp.Comparison.TRUE)
        return rasp.SelectorWidth(all_true_selector)

    if task == "reverse":
        length = make_length()  # `length` is not a primitive in our implementation.
        opp_index = length - rasp.indices - 1
        flip = rasp.Select(rasp.indices, opp_index, rasp.Comparison.EQ)
        reverse = rasp.Aggregate(flip, rasp.tokens)
        model = compiling.compile_rasp_to_model(
            reverse,
            vocab={1, 2, 3},
            max_seq_len=5,
            compiler_bos=bos,
        )
        out = model.apply([bos, 1, 2, 3])

    elif task == "proportion":
        from tracr.compiler.lib import make_frac_prevs
        model = compiling.compile_rasp_to_model(
            make_frac_prevs(rasp.tokens == "x"),
            vocab={"w", "x", "y", "z"},
            max_seq_len=5,
            compiler_bos="BOS",
        )

        out = model.apply(["BOS", "w", "x", "y", "z"])

    else:
        raise ValueError(f"Unknown task {task}")

    # Extract the model config from the Tracr model, and create a blank HookedTransformer object

    n_heads = model.model_config.num_heads
    n_layers = model.model_config.num_layers
    d_head = model.model_config.key_size
    d_mlp = model.model_config.mlp_hidden_size
    act_fn = "relu"
    normalization_type = "LN"  if model.model_config.layer_norm else None
    attention_type = "causal"  if model.model_config.causal else "bidirectional"


    n_ctx = model.params["pos_embed"]['embeddings'].shape[0]
    # Equivalent to length of vocab, with BOS and PAD at the end
    d_vocab = model.params["token_embed"]['embeddings'].shape[0]
    # Residual stream width, I don't know of an easy way to infer it from the above config.
    d_model = model.params["token_embed"]['embeddings'].shape[1]

    # Equivalent to length of vocab, WITHOUT BOS and PAD at the end because we never care about these outputs
    d_vocab_out = model.params["token_embed"]['embeddings'].shape[0] - 2

    cfg = HookedTransformerConfig(
        n_layers=n_layers,
        d_model=d_model,
        d_head=d_head,
        n_ctx=n_ctx,
        d_vocab=d_vocab,
        d_vocab_out=d_vocab_out,
        d_mlp=d_mlp,
        n_heads=n_heads,
        act_fn=act_fn,
        attention_dir=attention_type,
        normalization_type=normalization_type,
        use_global_cache=True,
        sixteen_heads=sixteen_heads,
        use_attn_result=True,
        use_split_qkv_input=True,
    )
    tl_model = HookedTransformer(cfg)
    # Extract the state dict, and do some reshaping so that everything has a n_heads dimension
    sd = {}
    sd["pos_embed.W_pos"] = model.params["pos_embed"]['embeddings']
    sd["embed.W_E"] = model.params["token_embed"]['embeddings']
    # Equivalent to max_seq_len plus one, for the BOS

    # The unembed is just a projection onto the first few elements of the residual stream, these store output tokens
    # This is a NumPy array, the rest are Jax Arrays, but w/e it's fine.
    sd["unembed.W_U"] = np.eye(d_model, d_vocab_out)

    for l in range(n_layers):
        sd[f"blocks.{l}.attn.W_K"] = einops.rearrange(
            model.params[f"transformer/layer_{l}/attn/key"]["w"],
            "d_model (n_heads d_head) -> n_heads d_model d_head",
            d_head = d_head,
            n_heads = n_heads
        )
        sd[f"blocks.{l}.attn.b_K"] = einops.rearrange(
            model.params[f"transformer/layer_{l}/attn/key"]["b"],
            "(n_heads d_head) -> n_heads d_head",
            d_head = d_head,
            n_heads = n_heads
        )
        sd[f"blocks.{l}.attn.W_Q"] = einops.rearrange(
            model.params[f"transformer/layer_{l}/attn/query"]["w"],
            "d_model (n_heads d_head) -> n_heads d_model d_head",
            d_head = d_head,
            n_heads = n_heads
        )
        sd[f"blocks.{l}.attn.b_Q"] = einops.rearrange(
            model.params[f"transformer/layer_{l}/attn/query"]["b"],
            "(n_heads d_head) -> n_heads d_head",
            d_head = d_head,
            n_heads = n_heads
        )
        sd[f"blocks.{l}.attn.W_V"] = einops.rearrange(
            model.params[f"transformer/layer_{l}/attn/value"]["w"],
            "d_model (n_heads d_head) -> n_heads d_model d_head",
            d_head = d_head,
            n_heads = n_heads
        )
        sd[f"blocks.{l}.attn.b_V"] = einops.rearrange(
            model.params[f"transformer/layer_{l}/attn/value"]["b"],
            "(n_heads d_head) -> n_heads d_head",
            d_head = d_head,
            n_heads = n_heads
        )
        sd[f"blocks.{l}.attn.W_O"] = einops.rearrange(
            model.params[f"transformer/layer_{l}/attn/linear"]["w"],
            "(n_heads d_head) d_model -> n_heads d_head d_model",
            d_head = d_head,
            n_heads = n_heads
        )
        sd[f"blocks.{l}.attn.b_O"] = model.params[f"transformer/layer_{l}/attn/linear"]["b"]

        sd[f"blocks.{l}.mlp.W_in"] = model.params[f"transformer/layer_{l}/mlp/linear_1"]["w"]
        sd[f"blocks.{l}.mlp.b_in"] = model.params[f"transformer/layer_{l}/mlp/linear_1"]["b"]
        sd[f"blocks.{l}.mlp.W_out"] = model.params[f"transformer/layer_{l}/mlp/linear_2"]["w"]
        sd[f"blocks.{l}.mlp.b_out"] = model.params[f"transformer/layer_{l}/mlp/linear_2"]["b"]
    print(sd.keys())

    # Convert weights to tensors and load into the tl_model

    for k, v in sd.items():
        # I cannot figure out a neater way to go from a Jax array to a numpy array lol
        sd[k] = torch.tensor(np.array(v))

    tl_model.load_state_dict(sd, strict=False)


    # Create helper functions to do the tokenization and de-tokenization

    INPUT_ENCODER = model.input_encoder
    OUTPUT_ENCODER = model.output_encoder

    def create_model_input(input, input_encoder=INPUT_ENCODER, device="cuda"):
        encoding = input_encoder.encode(input)
        return torch.tensor(encoding).unsqueeze(dim=0).to(device)

    if task == "reverse": # this doesn't make sense for proportion
        def decode_model_output(logits, output_encoder=OUTPUT_ENCODER, bos_token=INPUT_ENCODER.bos_token):
            max_output_indices = logits.squeeze(dim=0).argmax(dim=-1)
            decoded_output = output_encoder.decode(max_output_indices.tolist())
            decoded_output_with_bos = [bos_token] + decoded_output[1:]
            return decoded_output_with_bos
    # We can now run the model!
    if task == "reverse":
        input = [bos, 1, 2, 3]
        out = model.apply(input)
        print("Original Decoding:", out.decoded)

        input_tokens_tensor = create_model_input(input)
        logits = tl_model(input_tokens_tensor)
        decoded_output = decode_model_output(logits)
        print("TransformerLens Replicated Decoding:", decoded_output)

    elif task == "proportion":
        input = [bos, "x", "w", "w", "x"]
        out = model.apply(input)
        print("Original Decoding:", out.decoded)

        input_tokens_tensor = create_model_input(input)
        logits = tl_model(input_tokens_tensor)
        # decoded_output = decode_model_output(logits)
        # print("TransformerLens Replicated Decoding:", decoded_output)


    else:
        raise ValueError("Task must be either 'reverse' or 'proportion'")

    # Lets cache all intermediate activations in the model, and check that they're the same:

    logits, cache = tl_model.run_with_cache(input_tokens_tensor)

    for layer in range(tl_model.cfg.n_layers):
        print(f"Layer {layer} Attn Out Equality Check:", np.isclose(cache["attn_out", layer].detach().cpu().numpy(), np.array(out.layer_outputs[2*layer])).all())
        print(f"Layer {layer} MLP Out Equality Check:", np.isclose(cache["mlp_out", layer].detach().cpu().numpy(), np.array(out.layer_outputs[2*layer+1])).all())


    # Look how pretty and ordered the final residual stream is!
    # 
    # (The logits are the first 3 dimensions of the residual stream, and we can see that they're flipped!)

    import plotly.express as px
    im = cache["resid_post", -1].detach().cpu().numpy()[0]
    # px.imshow(im, color_continuous_scale="Blues", labels={"x":"Residual Stream", "y":"Position"}, y=[str(i) for i in input]).show()

    if return_im: 
        return im

    else:
        return create_model_input, tl_model

def get_all_tracr_things():
    pass

# get some random permutation with no fixed points
def get_perm(n, no_fp = True):
    if no_fp:
        assert n>1
    perm = torch.randperm(n)
    while (perm == torch.arange(n)).any().item():
        perm = torch.randperm(n)
    return perm

def get_tracr_data(tl_model, task: Literal["reverse", "proportion"], return_one_element=True):
    if task == "reverse":
        batch_size = 6
        seq_len = 4
        data_tens = torch.zeros((batch_size, seq_len)).int()

        vals=[0,1,2]
        import itertools
        for perm_idx, perm in enumerate(itertools.permutations(vals)):
            data_tens[perm_idx] = torch.tensor([3, perm[0], perm[1], perm[2]])

        n = len(data_tens)
        data_tens = data_tens.long()
        patch_data_indices = get_perm(n)

        warnings.warn("Test that this only considers the relevant part of the sequence...")

        patch_data_tens = data_tens[patch_data_indices]

        # base_model_logprobs = F.log_softmax(tl_model(data_tens), dim=-1)
        # metric = partial(kl_divergence, base_model_logprobs=base_model_logprobs, mask_repeat_candidates=None, return_one_element=return_one_element)

        def l2_metric_for_reverse(
            model_out: torch.Tensor,
            base_model_vals: torch.Tensor,
            return_one_element: bool = True,
        ):
            ret = (model_out[:, 1:] - base_model_vals[:, 1:]).pow(2).sum(dim=-1)
            if return_one_element:
                return ret.mean()
            else:
                return ret

        model_out = tl_model(data_tens)
        metric = partial(l2_metric_for_reverse, base_model_vals=model_out, return_one_element=return_one_element)

    elif task == "proportion":
        batch_size = 50
        seq_len = 4
        def to_tens(s):
            assert isinstance(s, str) or isinstance(s, list) or isinstance(s, tuple)
            assert len(s)==seq_len
            assert all([c in ["w", "x", "y", "z"] for c in s]), s
            return torch.tensor([ord(c)-ord("w") for c in s]).int()
        data_tens = torch.zeros((batch_size, seq_len)).int()
        alphabet = "wxyz"
        import itertools
        all_things = list(itertools.product(alphabet, repeat=seq_len))
        rand_perm1 = torch.randperm(len(all_things))
        for i in range(batch_size):
            data_tens[i] = to_tens(all_things[rand_perm1[i]])
        data_tens = data_tens.long()
        rand_perm2 = torch.randperm(batch_size)
        patch_data_tens = data_tens[rand_perm2]
        base_model_vals = tl_model(data_tens)[:, 1:, 0]

        def l2_metric( # this is for proportion... it's unclear how to format this tbh sad
            model_out: torch.Tensor,
            base_model_vals: torch.Tensor,
            return_one_element: bool = True,
        ):
            # [1:, 0] shit

            proc = model_out[:, 1:, 0]
            for tens in [proc, base_model_vals]:    
                assert 0<=tens.min()<=tens.max()<=1, (tens.min(), tens.max())
            
            answer = ((proc - base_model_vals)**2).sum(dim=-1) # collapse the L2

            if return_one_element: 
                answer = answer.mean()

            return answer

        metric = partial(l2_metric, base_model_vals = base_model_vals, return_one_element=return_one_element)

    else: 
        raise ValueError(task)

    return data_tens, patch_data_tens, metric

def get_tracr_proportion_edges():

    # generated from acdc/main.py commit 3a3770bb7

    return OrderedDict([(('blocks.1.hook_resid_post',
               (None,),
               'blocks.1.attn.hook_result',
               (None, None, 0)),
              True),
             (('blocks.1.attn.hook_result',
               (None, None, 0),
               'blocks.1.attn.hook_q',
               (None, None, 0)),
              True),
             (('blocks.1.attn.hook_result',
               (None, None, 0),
               'blocks.1.attn.hook_k',
               (None, None, 0)),
              True),
             (('blocks.1.attn.hook_result',
               (None, None, 0),
               'blocks.1.attn.hook_v',
               (None, None, 0)),
              True),
             (('blocks.1.attn.hook_q',
               (None, None, 0),
               'blocks.1.hook_q_input',
               (None, None, 0)),
              True),
             (('blocks.1.attn.hook_k',
               (None, None, 0),
               'blocks.1.hook_k_input',
               (None, None, 0)),
              True),
             (('blocks.1.attn.hook_v',
               (None, None, 0),
               'blocks.1.hook_v_input',
               (None, None, 0)),
              True),
             (('blocks.1.hook_q_input',
               (None, None, 0),
               'hook_embed',
               (None,)),
              True),
             (('blocks.1.hook_q_input',
               (None, None, 0),
               'hook_pos_embed',
               (None,)),
              True),
             (('blocks.1.hook_k_input',
               (None, None, 0),
               'hook_embed',
               (None,)),
              True),
             (('blocks.1.hook_k_input',
               (None, None, 0),
               'hook_pos_embed',
               (None,)),
              True),
             (('blocks.1.hook_v_input',
               (None, None, 0),
               'blocks.0.hook_mlp_out',
               (None,)),
              True),
             (('blocks.0.hook_mlp_out',
               (None,),
               'blocks.0.hook_resid_mid',
               (None,)),
              True),
             (('blocks.0.hook_resid_mid', (None,), 'hook_embed', (None,)),
              True)])

def get_tracr_reverse_edges():
    return OrderedDict([(('blocks.3.hook_resid_post',
               (None,),
               'blocks.3.attn.hook_result',
               (None, None, 0)),
              True),
             (('blocks.3.attn.hook_result',
               (None, None, 0),
               'blocks.3.attn.hook_q',
               (None, None, 0)),
              True),
             (('blocks.3.attn.hook_result',
               (None, None, 0),
               'blocks.3.attn.hook_k',
               (None, None, 0)),
              True),
             (('blocks.3.attn.hook_result',
               (None, None, 0),
               'blocks.3.attn.hook_v',
               (None, None, 0)),
              True),
             (('blocks.3.attn.hook_q',
               (None, None, 0),
               'blocks.3.hook_q_input',
               (None, None, 0)),
              True),
             (('blocks.3.attn.hook_k',
               (None, None, 0),
               'blocks.3.hook_k_input',
               (None, None, 0)),
              True),
             (('blocks.3.attn.hook_v',
               (None, None, 0),
               'blocks.3.hook_v_input',
               (None, None, 0)),
              True),
             (('blocks.3.hook_q_input',
               (None, None, 0),
               'blocks.2.hook_mlp_out',
               (None,)),
              True),
             (('blocks.3.hook_k_input',
               (None, None, 0),
               'hook_pos_embed',
               (None,)),
              True),
             (('blocks.3.hook_v_input',
               (None, None, 0),
               'hook_embed',
               (None,)),
              True),
             (('blocks.2.hook_mlp_out',
               (None,),
               'blocks.2.hook_resid_mid',
               (None,)),
              True),
             (('blocks.2.hook_resid_mid',
               (None,),
               'blocks.1.hook_mlp_out',
               (None,)),
              True),
             (('blocks.1.hook_mlp_out',
               (None,),
               'blocks.1.hook_resid_mid',
               (None,)),
              True),
             (('blocks.1.hook_resid_mid',
               (None,),
               'blocks.0.hook_mlp_out',
               (None,)),
              True),
             (('blocks.1.hook_resid_mid', (None,), 'hook_embed', (None,)),
              True),
             (('blocks.1.hook_resid_mid', (None,), 'hook_pos_embed', (None,)),
              True),
             (('blocks.0.hook_mlp_out',
               (None,),
               'blocks.0.hook_resid_mid',
               (None,)),
              True),
             (('blocks.0.hook_resid_mid',
               (None,),
               'blocks.0.attn.hook_result',
               (None, None, 0)),
              True),
             (('blocks.0.hook_resid_mid', (None,), 'hook_embed', (None,)),
              True),
             (('blocks.0.attn.hook_result',
               (None, None, 0),
               'blocks.0.attn.hook_q',
               (None, None, 0)),
              True),
             (('blocks.0.attn.hook_result',
               (None, None, 0),
               'blocks.0.attn.hook_k',
               (None, None, 0)),
              True),
             (('blocks.0.attn.hook_result',
               (None, None, 0),
               'blocks.0.attn.hook_v',
               (None, None, 0)),
              True),
             (('blocks.0.attn.hook_v',
               (None, None, 0),
               'blocks.0.hook_v_input',
               (None, None, 0)),
              True),
             (('blocks.0.hook_v_input',
               (None, None, 0),
               'hook_embed',
               (None,)),
              True)])