#%% [markdown]
# Arthur investigation into dropout
from copy import deepcopy
import torch

from easy_transformer.experiments import get_act_hook
from utils_induction import *

assert torch.cuda.device_count() == 1
from tqdm import tqdm
import matplotlib.pyplot as plt
import pandas as pd
import torch
import torch as t
from easy_transformer.EasyTransformer import (
    EasyTransformer,
)
from time import ctime
from functools import partial

import numpy as np
from tqdm import tqdm
import pandas as pd
import plotly.express as px
import plotly.io as pio
import plotly.graph_objects as go
import random
import einops
from IPython import get_ipython
from copy import deepcopy
from ioi_dataset import (
    IOIDataset,
)
from ioi_utils import (
    path_patching,
    max_2d,
    CLASS_COLORS,
    e,
    show_pp,
    show_attention_patterns,
    scatter_attention_and_contribution,
)
from random import randint as ri
from easy_transformer.experiments import get_act_hook
from ioi_circuit_extraction import (
    do_circuit_extraction,
    get_heads_circuit,
    CIRCUIT,
)
import random as rd
from ioi_utils import logit_diff, probs
from ioi_utils import get_top_tokens_and_probs as g

ipython = get_ipython()
if ipython is not None:
    ipython.magic("load_ext autoreload")
    ipython.magic("autoreload 2")
#%% [markdown]
# Make models

gpt2 = EasyTransformer.from_pretrained("gpt2").cuda()
gpt2.set_use_attn_result(True)

opt = EasyTransformer.from_pretrained("facebook/opt-125m").cuda()
opt.set_use_attn_result(True)

neo = EasyTransformer.from_pretrained("EleutherAI/gpt-neo-125M").cuda()
neo.set_use_attn_result(True)

solu = EasyTransformer.from_pretrained("solu-10l-old").cuda()
solu.set_use_attn_result(True)

model_names = ["gpt2", "opt", "neo", "solu"]
model_name = "gpt2"
model = eval(model_name)

saved_tensors = []
#%% [markdown]
# Make induction dataset

seq_len = 10
batch_size = 5
interweave = 10  # have this many things before a repeat

rand_tokens = torch.randint(1000, 10000, (batch_size, seq_len))
rand_tokens_repeat = torch.zeros(
    size=(batch_size, seq_len * 2)
).long()  # einops.repeat(rand_tokens, "batch pos -> batch (2 pos)")

for i in range(seq_len // interweave):
    rand_tokens_repeat[
        :, i * (2 * interweave) : i * (2 * interweave) + interweave
    ] = rand_tokens[:, i * interweave : i * interweave + interweave]
    rand_tokens_repeat[
        :, i * (2 * interweave) + interweave : i * (2 * interweave) + 2 * interweave
    ] = rand_tokens[:, i * interweave : i * interweave + interweave]
rand_tokens_control = torch.randint(1000, 10000, (batch_size, seq_len * 2))

rand_tokens = prepend_padding(rand_tokens, model.tokenizer)
rand_tokens_repeat = prepend_padding(rand_tokens_repeat, model.tokenizer)
rand_tokens_control = prepend_padding(rand_tokens_control, model.tokenizer)


def calc_score(attn_pattern, hook, offset, arr):
    # Pattern has shape [batch, index, query_pos, key_pos]
    stripe = attn_pattern.diagonal(offset, dim1=-2, dim2=-1)
    scores = einops.reduce(stripe, "batch index pos -> index", "mean")
    # Store the scores in a common array
    arr[hook.layer()] = scores.detach().cpu().numpy()
    # return arr
    return attn_pattern

def filter_attn_hooks(hook_name):
    split_name = hook_name.split(".")
    return split_name[-1] == "hook_attn"

arrs = []
#%% [markdown]
# sweeeeeet plot

show_losses(
    models=[eval(model_name) for model_name in model_names],
    model_names=model_names,
    rand_tokens_repeat=rand_tokens_repeat,
    seq_len=seq_len,
    mode="logits",
)
#%% [markdown]
# Induction scores
# Use this to get a "shortlist" of the heads that matter most for ind

def filter_attn_hooks(hook_name):
    split_name = hook_name.split(".")
    return split_name[-1] == "hook_attn"

model.reset_hooks()
more_hooks = []

# for head in [(11, head_idx) for head_idx in range(5)]: # nduct_heads[:5]:
    # more_hooks.append(hooks[head])

def get_induction_scores(model, rand_tokens_repeat, title=""):

    def calc_induction_score(attn_pattern, hook):
        # Pattern has shape [batch, index, query_pos, key_pos]
        induction_stripe = attn_pattern.diagonal(1 - seq_len, dim1=-2, dim2=-1)
        induction_scores = einops.reduce(
            induction_stripe, "batch index pos -> index", "mean"
        )

        # Store the scores in a common arraymlp_ = saved_tensors[-2].clone()
        induction_scores_array[hook.layer()] = induction_scores.detach().cpu().numpy()

    model = eval(model_name)
    induction_scores_array = np.zeros((model.cfg.n_layers, model.cfg.n_heads))
    induction_logits = model.run_with_hooks(
        rand_tokens_repeat, fwd_hooks= more_hooks + [(filter_attn_hooks, calc_induction_score)], # , reset_hooks_start=False,
    )
    induction_scores_array = torch.tensor(induction_scores_array)
    fig = px.imshow(
        induction_scores_array,
        labels={"y": "Layer", "x": "Head"},
        color_continuous_scale="Blues",       
    )
    # add title
    fig.update_layout(
        title_text=f"Induction scores for "+ title,
        title_x=0.5,
        title_font_size=20,
    )
    fig.show()
    return induction_scores_array

induction_scores_array = get_induction_scores(model, rand_tokens_repeat, title=model_name)
#%% [markdown]
# is GPT-Neo behaving right?

logits_and_loss = model(
    rand_tokens_repeat, return_type="both", loss_return_per_token=True
)
logits = logits_and_loss["logits"].cpu()[:, :-1] # remove unguessable next token
loss = logits_and_loss["loss"].cpu()

probs_denoms = torch.sum(torch.exp(logits), dim=-1, keepdim=True)
probs_num = torch.exp(logits)
probs = probs_num / probs_denoms

# probs = torch.softmax(logits, dim=-1)

batch_size, _, vocab_size = logits.shape
seq_indices = einops.repeat(torch.arange(_), "a -> b a", b=batch_size)
batch_indices = einops.repeat(torch.arange(batch_size), "b -> b a", a=_)
probs_on_correct = probs[batch_indices, seq_indices, rand_tokens_repeat[:, 1:]]
log_probs = - torch.log(probs_on_correct)

assert torch.allclose(
    log_probs, loss, rtol=1e-3, atol=1e-3, # torch.exp(log_probs.gather(-1, rand_tokens_repeat[:, 1:].unsqueeze(-1)).squeeze(-1))
)
#%% [markdown]
# make all hooks

def random_patching(z, act, hook):
    b = z.shape[0]
    z[torch.arange(b)] = act[torch.randperm(b)]
    return z

cache = {}
model.reset_hooks()
model.cache_some(
    cache,
    lambda x: "attn.hook_result" in x or "mlp_out" in x,
    suppress_warning=True,
)
logits, loss = model(
    rand_tokens_control, return_type="both", loss_return_per_token=True
).values()

hooks = {}
all_heads_and_mlps = [(layer, head_idx) for layer in range(model.cfg.n_layers) for head_idx in [None] + list(range(model.cfg.n_heads))]

for layer, head_idx in all_heads_and_mlps:
    hook_name = f"blocks.{layer}.attn.hook_result"
    if head_idx is None:
        hook_name = f"blocks.{layer}.hook_mlp_out"

    hooks[(layer, head_idx)] = (
        hook_name,
        get_act_hook(
            random_patching,
            alt_act=cache[hook_name],
            idx=head_idx,
            dim=2 if head_idx is not None else None,
            name=hook_name,
        ),
    )
model.reset_hooks()

#%% [markdown]
# setup

def loss_metric(
    model,
    rand_tokens_repeat,
    seq_len,
):
    cur_loss = model(
        rand_tokens_repeat, return_type="both", loss_return_per_token=True
    )["loss"][:, -seq_len // 2 :].mean()
    return cur_loss.item()

def logits_metric(
    model,
    rand_tokens_repeat,
    seq_len,
):
    """Double implemented from utils_induction..."""
    logits = model(rand_tokens_repeat, return_type="logits")
    # print(logits.shape) # 5 21 50257

    assert len(logits.shape) == 3, logits.shape
    batch_size, _, vocab_size = logits.shape
    seq_indices = einops.repeat(torch.arange(seq_len) + seq_len, "a -> b a", b=batch_size)
    batch_indices = einops.repeat(torch.arange(batch_size), "b -> b a", a=seq_len)
    logits_on_correct = logits[batch_indices, seq_indices, rand_tokens_repeat[:, seq_len + 1:]]

    return logits_on_correct[:, -seq_len // 2 :].mean().item()

def denom_metric(
    model,
    rand_tokens_repeat,
    seq_len,
):
    """Denom of the final softmax"""
    logits = model(rand_tokens_repeat, return_type="logits") # 5 21 50257
    denom = torch.exp(logits)
    denom = denom[:, -seq_len // 2 :].sum(dim=-1).mean()
    return denom.item()

model.reset_hooks()

#%% [markdown]
# use this cell to get a rough grip on which heads matter the most
model.reset_hooks()
both_results = []
the_extra_hooks = None

# initial_logits, initial_loss = model(
#     rand_tokens_repeat, return_type="both", loss_return_per_token=True
# ).values()

metric = logits_metric

for idx, extra_hooks in enumerate([[]]): # , [hooks[((6, 1))]], [hooks[(11, 4)]], the_extra_hooks]):
    if extra_hooks is None:
        break
    results = torch.zeros(size=(model.cfg.n_layers, model.cfg.n_heads))
    mlp_results = torch.zeros(size=(model.cfg.n_layers, 1))
    model.reset_hooks()
    for hook in extra_hooks:
        model.add_hook(*hook)
    # initial_loss = model(
    #     rand_tokens_repeat, return_type="both", loss_return_per_token=True
    # )["loss"][:, -seq_len // 2 :].mean()
    initial_metric = metric(model, rand_tokens_repeat, seq_len)
    print(f"Initial initial_metric: {initial_metric}")

    for source_layer in tqdm(range(model.cfg.n_layers)):
        for source_head_idx in [None] + list(range(model.cfg.n_heads)):
            model.reset_hooks()
            receiver_hooks = []
            receiver_hooks.append((f"blocks.{model.cfg.n_layers-1}.hook_resid_post", None))
            # receiver_hooks.append((f"blocks.11.attn.hook_result", 4))

            # for layer in range(7, model.cfg.n_layers): # model.cfg.n_layers):
            #     for head_idx in list(range(model.cfg.n_heads)) + [None]:
            #         hook_name = f"blocks.{layer}.attn.hook_result"
            #         if head_idx is None:
            #             hook_name = f"blocks.{layer}.hook_mlp_out"
            #         receiver_hooks.append((hook_name, head_idx))

            if False:
                model = path_patching_attribution(
                    model=model,
                    tokens=rand_tokens_repeat,
                    patch_tokens=rand_tokens_control,
                    sender_heads=[(source_layer, source_head_idx)],
                    receiver_hooks=receiver_hooks,
                    start_token=seq_len + 1,
                    end_token=2 * seq_len,
                    device="cuda",
                    freeze_mlps=True,
                    return_hooks=False,
                    extra_hooks=extra_hooks,
                )
                title="Direct"

            else:
                # model.add_hook(*hooks[(6, 1)])
                model.add_hook(*hooks[(source_layer, source_head_idx)])
                title="Indirect"

            # model.reset_hooks()
            # for hook in hooks:
            #     model.add_hook(*hook)
            # loss = model(
            #     rand_tokens_repeat, return_type="both", loss_return_per_token=True
            # )["loss"][:, -seq_len // 2 :].mean()
            cur_metric = metric(model, rand_tokens_repeat, seq_len)

            a = hooks.pop((source_layer, source_head_idx))
            e("a")

            if source_head_idx is None:
                mlp_results[source_layer] = cur_metric - initial_metric
            else:
                results[source_layer][source_head_idx] = cur_metric - initial_metric

            if source_layer == model.cfg.n_layers-1 and source_head_idx == model.cfg.n_heads-1:
                fname = f"svgs/patch_and_freeze_{ctime()}_{ri(2134, 123759)}"
                fig = show_pp(
                    results.T.detach(),
                    title=f"{title} effect of removing heads on {metric} {fname}",
                    # + ("" if idx == 0 else " (with top 3 name movers knocked out)"),
                    return_fig=True,
                    show_fig=False,
                )
                both_results.append(results.clone())
                fig.show()
                show_pp(mlp_results.detach().cpu())
                saved_tensors.append(results.clone().cpu())
                saved_tensors.append(mlp_results.clone().cpu())
#%% [markdown]
# Get top 5 induction heads
warnings.warn("Check that things aren't in decreasing order, maaan")

no_heads = 10
heads_by_induction = max_2d(induction_scores_array, 144)[0]
induct_heads = []
idx = 0
while len(induct_heads) < no_heads:
    head = heads_by_induction[idx]
    idx+=1
    if "results" in dir() and results[head] <= 0:
        # pass
        induct_heads.append(head)
    else:
        print(f" {head} because it's negative, with value {results[head]}")
    # induct_heads.append(head)


# sort the induction heads by their results
if "results" in dir():
    induct_heads = sorted(induct_heads, key=lambda x: -(results[x]), reverse=True)

# have a look at these numbers
for layer, head in induct_heads:
    print(f"Layer: {layer}, Head: {head}, Induction score: {induction_scores_array[layer][head]}, Loss diff: {results[layer][head]}")

print(induct_heads)

#%%

# plot a scatter plot in plotly with labels
fig = go.Figure()
for layer in range(model.cfg.n_layers):
    for head in range(model.cfg.n_heads):
        fig.add_trace(go.Scatter(x=[induction_scores_array[layer][head].item()], y=[results[layer][head].item()], mode='markers', name=f"Layer: {layer}, Head: {head}"))
fig.update_layout(title="Induction score vs loss diff", xaxis_title="Induction score", yaxis_title="Change in logits on correct")
fig.show()


# fig = go.Figure()
# fig.add_trace(go.Scatter(x=induction_scores_array.flatten().cpu().detach(), y=results.flatten().cpu().detach(), mode='markers'))
# fig.show()

#%% [markdown]
# Look at attention patterns of things

my_heads = max_2d(torch.abs(results), k=20)[0]
print(my_heads)

my_heads = [(6, 6), (6, 11)] + induct_heads

for LAYER, HEAD in my_heads:
    model.reset_hooks()
    hook_name = f"blocks.{LAYER}.attn.hook_attn" # 4 12 50 50
    new_cache = {}
    model.cache_some(new_cache, lambda x: hook_name in x)
    # model.add_hook(*hooks[((6, 1))])
    # model.add_hooks(hooks)
    model(rand_tokens_repeat)

    att = new_cache[hook_name]
    mean_att = att[:, HEAD].mean(dim=0)
    show_pp(mean_att, title=f"Mean attention for head {LAYER}.{HEAD}")

#%% [markdown]
# Look into compensation in both cases despite it seeming very different

cache = {}
model.reset_hooks()
model.cache_some(
    cache,
    lambda x: "attn.hook_result" in x or "mlp_out" in x,
    suppress_warning=True,
    # device=device,
)
logits, loss = model(
    rand_tokens_control, return_type="both", loss_return_per_token=True
).values()

# top_heads = [
#     (9, 9),
#     (9, 6),
#     (10, 1),
#     (7, 10),
#     (10, 0),
#     (11, 9),
#     (7, 2),
#     (6, 9),
#     # (10, 6),
#     # (10, 3),
# ]

top_heads = [
    (9, 6),
    (10, 0),
    (7, 2),
    (9, 9),
    (7, 10),
    (9, 1),
    (11, 5),
    (6, 9),
    (10, 1),
    (11, 9),
    (8, 1),
    (10, 6),
    (5, 1),
    (10, 10),
    (10, 3),
]

top_heads = [
    (6, 1),
    (8, 1),
    (6, 6),
    (8, 0),
    (8, 8),
]

top_heads = induct_heads
# top_heads = [(5, 1), (7, 2), (7, 10), (6, 9), (5, 5)]

hooks = {}

# top_heads = [
#     (layer, head_idx)
#     for layer in range(model.cfg.n_layers)
#     for head_idx in [None] + list(range(model.cfg.n_heads))
# ]

skipper = 0
# top_heads = max_2d(results, 20)[0][skipper:]


# def zero_all(z, act, hook):
#     z[:] = 0
#     return z


def random_patching(z, act, hook):
    b = z.shape[0]
    z[torch.arange(b)] = act[torch.randperm(b)]
    return z


for layer, head_idx in top_heads:
    hook_name = f"blocks.{layer}.attn.hook_result"
    if head_idx is None:
        hook_name = f"blocks.{layer}.hook_mlp_out"

    hooks[(layer, head_idx)] = (
        hook_name,
        get_act_hook(
            random_patching,
            alt_act=cache[hook_name],
            idx=head_idx,
            dim=2 if head_idx is not None else None,
            name=hook_name,
        ),
    )
model.reset_hooks()

#%% [markdown]
# Line graph

# reverse the order of the top heads
tot = len(induct_heads) + 1
# tot=5

initial_loss = model(
    rand_tokens_repeat, return_type="both", loss_return_per_token=True
)["loss"][:, -seq_len // 2 :].mean()

# induct_heads = max_2d(torch.tensor(induction_scores_array), tot)[0]
# induct_heads = [(6, 1), (8, 0), (6, 11), (8, 1), (8, 8)]

hooks = {head:hooks[head] for head in induct_heads}

def get_random_subset(l, size):
    return [l[i] for i in sorted(random.sample(range(len(l)), size))]

ys = []
ys2 = []
no_iters = 30
max_len = len(induct_heads)

# metric = loss_metric
metric = logits_metric
# mode = "random subset"
mode = "decreasing"


for subset_size in tqdm(range(len(induct_heads) + 1)):
    model.reset_hooks()

    curv = 0
    curw = initial_loss.item()  # "EXPECTED" increase
    for _ in range(30):
        model.reset_hooks()

        ordered_hook_list = []
        if mode == "random subset":
            ordered_hook_list = get_random_subset(list(hooks.items()), subset_size)
        elif mode == "decreasing":
            ordered_hook_list = list(hooks.items())[:subset_size]
        else:
            raise ValueError()

        for hook in ordered_hook_list:
            model.add_hook(*hook[1])
            # curw += results[hook[0]].item()

        cur_metric = metric(
            model, rand_tokens_repeat, seq_len,
        )
        # print(f"Layer {layer}, head {head_idx}: {loss.mean().item()}")

        curv += cur_metric
    curv /= no_iters
    curw /= no_iters
    ys.append(curv)
    # curw = (
    #     initial_loss.item()
    #     + torch.sum(max_2d(results, 15)[1][skipper : skipper + subset_size]).item()
    # )
    curw = curv
    ys2.append(curw)

# plot the results
fig = go.Figure()
fig.add_trace(
    go.Scatter(
        x=list(range(0, max_len+1)),
        y=ys,
        mode="lines+markers",
        name="Top k heads removed",
        line=dict(color="Black", width=1),
    )
)
# fig.add_trace(
#     go.Scatter(
#         x=list(range(0, max_len+1)),
#         y=ys2,
#         mode="lines+markers",
#         name="Sum of direct effects",
#         line=dict(color="Red", width=1),
#     )
# )

start_x = 0
start_y = ys[0]
end_x = tot - 1
end_y = ys[tot - 1]

if mode == "decreasing":
    contributions = {head:(results[head].item()) for head in induct_heads}
    contributions_sum = sum(contributions.values())
    for head in induct_heads: contributions[head] /= contributions_sum

    expected_x = list(range(tot))
    expected_y = [start_y]
    y_diff = end_y - start_y
    for head in induct_heads:
        expected_y.append(expected_y[-1] + y_diff * contributions[head])

    fig.add_trace(
        go.Scatter(
            x=expected_x,
            y=expected_y,
            mode="lines+markers",
            name="Expected",
            line=dict(color="Blue", width=1),
        )
    )

expected_x_2 = list(range(tot))
expected_y_2 = [start_y]

for head in induct_heads:
    expected_y_2.append(expected_y_2[-1] + results[head])

fig.add_trace(
    go.Scatter(
        x=expected_x_2,
        y=expected_y_2,
        mode="lines+markers",
        name="Sum the independent effects",
        line=dict(color="Green", width=1),
    )
)


# add the line from (0, ys[0]) to (tot-1, ys[tot-1])
fig.add_trace(
    go.Scatter(
        x=[0, max_len],
        y=[ys[0], ys[-1]],
        mode="lines",
        name="Linear from start to end",
        line=dict(color="Blue", width=1),
    )
)

# add x axis labels
fig.update_layout(
    xaxis_title="Number of heads removed (k)",
    yaxis_title="Logits on correct",
    title="Effect of removing heads on correct logits (decreasing importance)",
)


#%% [markdown]

for tens in [froze_results, froze_mlp, flow_results, flow_mlp]:
    print(torch.sum(tens))

#%% [markdown]
# Induction compensation

from ioi_utils import compute_next_tok_dot_prod
import torch.nn.functional as F

IDX = 0


def zero_ablate(hook, z):
    return torch.zeros_like(z)


head_mask = torch.empty((model.cfg.n_layers, model.cfg.n_heads), dtype=torch.bool)
head_mask[:] = False
head_mask[5, 5] = True
head_mask[6, 9] = False

attn_head_mask = head_mask


def filter_value_hooks(name):
    return name.split(".")[-1] == "hook_v"


def compute_logit_probs(rand_tokens_repeat, model):
    induction_logits = model(rand_tokens_repeat)
    induction_log_probs = F.log_softmax(induction_logits, dim=-1)
    induction_pred_log_probs = torch.gather(
        induction_log_probs[:, :-1].cuda(), -1, rand_tokens_repeat[:, 1:, None].cuda()
    )[..., 0]
    return induction_pred_log_probs[:, seq_len:].mean().cpu().detach().numpy()


compute_logit_probs(rand_tokens_repeat, model)

"""
Skipping (6, 6) because it's negative, with vale -0.15283656120300293
Skipping (6, 11) because it's negative, with vale -0.09658721089363098
Layer: 6, Head: 1, Induction score: 0.8501311540603638, Loss diff: 1.0953487157821655
Layer: 8, Head: 1, Induction score: 0.6479660868644714, Loss diff: 0.2444022297859192
Layer: 8, Head: 8, Induction score: 0.5408309698104858, Loss diff: 0.23612505197525024
Layer: 8, Head: 0, Induction score: 0.6423881649971008, Loss diff: 0.21361035108566284
Layer: 6, Head: 0, Induction score: 0.562366783618927, Loss diff: 0.02358981966972351
[(6, 1), (8, 1), (8, 8), (8, 0), (6, 0)]
"""

"""
(6, 6) because it's negative, with vale 1.0968360900878906
 (6, 11) because it's negative, with vale 0.6495914459228516
 (11, 6) because it's negative, with vale 0.6050567626953125
 (7, 2) because it's negative, with vale 0.028009414672851562
 (8, 9) because it's negative, with vale 0.01535797119140625
Layer: 6, Head: 0, Induction score: 0.562366783618927, Loss diff: -0.055957794189453125
Layer: 10, Head: 1, Induction score: 0.21510878205299377, Loss diff: -0.0745391845703125
Layer: 8, Head: 11, Induction score: 0.4771292805671692, Loss diff: -0.1757049560546875
Layer: 8, Head: 6, Induction score: 0.3630002439022064, Loss diff: -0.4016103744506836
Layer: 10, Head: 3, Induction score: 0.275448203086853, Loss diff: -0.4731121063232422
Layer: 8, Head: 2, Induction score: 0.22248125076293945, Loss diff: -0.4885101318359375
Layer: 8, Head: 0, Induction score: 0.6423881649971008, Loss diff: -0.915496826171875
Layer: 8, Head: 1, Induction score: 0.6479660868644714, Loss diff: -0.9298639297485352
Layer: 8, Head: 8, Induction score: 0.5408309698104858, Loss diff: -1.0409841537475586
Layer: 6, Head: 1, Induction score: 0.8501311540603638, Loss diff: -1.8944549560546875
[(6, 0), (10, 1), (8, 11), (8, 6), (10, 3), (8, 2), (8, 0), (8, 1), (8, 8), (6, 1)]
"""