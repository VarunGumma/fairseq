<p align="center">
  <img src="docs/fairseq_logo.png" width="150">
  <br />
  <br />
  <a href="https://github.com/pytorch/fairseq/blob/main/LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/license-MIT-blue.svg" /></a>
</p>

--------------------------------------------------------------------------------

`Fairseq(-py)` is a sequence modeling toolkit that allows researchers and
developers to train custom models for translation, summarization, language
modeling and other text generation tasks.


# Usage
This clone of fairseq supports `Knowledge Distillation`, `Recurrent Stacking`, `LoRA` `RoPE`, and `ALiBi`, for the `Transformer` model and the `translation` task. You can add the following flags to `fairseq-train`/`fairseq-interactive`/`fairseq-generate` to use them:

| **Name and Citation** | **Description** | **Flags to Activate** | **Source** |
|-----------------------|-----------------------|-----------------------|------------|
| **Knowledge Distillation** ([Hinton _et al_.](https://arxiv.org/abs/1503.02531), [Kim & Rush](https://aclanthology.org/D16-1139), [Wang _et al_.](https://aclanthology.org/2021.acl-long.504), [Gumma _et al_.](https://aclanthology.org/2023.eamt-1.11/)) | Transfers _soft_ information from a pretrained teacher model to a smaller student model (Uses Forward KLD). Please check [here](https://github.com/VarunGumma/fairseq/blob/main/fairseq/criterions/seq2seq_lm_distillation.py) for a detailed description of the arguments. | `--teacher-checkpoint-path $path --task seq2seq_lm_distillation --criterion lm_distillation_loss --kd-args '{"strategy": "on_policy", "lambda": 1.0}'` | [Selective Distillation](https://github.com/LeslieOverfitting/selective_distillation) |
| **Recurrent Stacking** ([Dabre & Fujita](https://ojs.aaai.org/index.php/AAAI/article/view/4590)) | Extreme parameter sharing technique in which all layers in the encoder/decoder are shared | `--encoder-recurrent-stacking 6 --decoder-recurrent-stacking 6` | - |
| **Low-Rank Adaptation (LoRA)** ([Hu _et al_.](https://openreview.net/forum?id=nZeVKeeFYf9)) | Efficient model adaptation technique that modifies a small number of model parameters while freezing the rest. | `--lora-args '{"r": 8, "alpha": 16, "dropout": 0.05, "bias": "none, "target_modules": "k_proj,v_proj", "rank_scaled": false}' --attn-implementation fast --load-checkpoint-liberally` | [LoRA Implementation](https://github.com/microsoft/LoRA) |
| **Rotary Positional Embedding (RoPE)** ([Su _et al_.](https://arxiv.org/abs/2104.09864)) | Encodes absolute position with a rotation matrix and incorporates explicit relative position dependency in self-attention formulation | `--rope-args '{"theta": 10000, "scaling_factor": 1.0}' --attn-implementation fast --no-token-positional-embeddings --load-checkpoint-liberally` | [RoPE Implementation](https://github.com/lucidrains/rotary-embedding-torch/blob/main/rotary_embedding_torch/rotary_embedding_torch.py) |
| **Gated Linear Unit (GLU)** ([Shazeer](https://arxiv.org/abs/2002.05202)) | A better Feed-Forward-Network variant | `--encoder-use-glu --decoder-use-glu` | [GLU Implementation](https://github.com/huggingface/transformers/blob/main/src/transformers/models/mistral/modeling_mistral.py#L160) |
| **RMSNorm** ([Zhang and Sennrich](https://papers.nips.cc/paper_files/paper/2019/hash/1e8a19426224ca89e83cef47f1e7f53b-Abstract.html)) | An efficient normalization technique | `--encoder-use-rmsnorm --decoder-use-rmsnorm` | [RMSNorm Implementation](https://github.com/pytorch/torchtune/blob/main/torchtune/modules/rms_norm.py) |
| **Attention with Linear Biases (ALiBi)** ([Press _et al_.](https://openreview.net/forum?id=R8sQPpGCv0)) | Simple and efficient position method that biases query-key attention scores with a penalty proportional to their distance | `--alibi-args '{"type": "symmetrical"}' --no-token-positional-embeddings --load-checkpoint-liberally` | [ALiBi Implementation](https://github.com/EIFY/fairseq) |
| **Factorized Embedding Parameterization** ([Lan _et al_.](https://openreview.net/forum?id=nZeVKeeFYf9)) | Parameterizes large embeddings by adding an intermediate bottleneck layer | `--encoder-factorized-embed-dim 128 --decoder-factorized-embed-dim 128` | - |
| **Sanity Validation Steps** | Runs a full pass over the validation set at the beginning of training | `--run-sanity-validation-steps` | - |
| **Efficient Multihead Attention (MHA)** | A [torch-functional variant](https://pytorch.org/docs/stable/generated/torch.nn.functional.scaled_dot_product_attention.html) of _MultiHeadAttention_ | `--attn-implementation fast`. By default, the value is `fairseq` | - |
| **Grouped Query Attention (GQA)** ([Ainslie _et al._](https://aclanthology.org/2023.emnlp-main.298/)) | Clusters queries into groups, allowing for more efficient computation and enhanced scalability in processing large sets of queries within transformer models. | `--attn-implementation fast_gqa --encoder-kv-attention-heads 2 --decoder-kv-attention-heads 2` | [GQA Implementation](https://pytorch.org/torchtune/stable/_modules/torchtune/modules/attention.html) |
| **Fused Attention** | Combines the efficiency of Multi-Head Attention (MHA) and Grouped Query Attention (GQA) into a fused operation, providing faster computation. This fused version is not compatible with models trained using the unfused versions of MHA or GQA. | `--attn-implementation fast_fused` or `--attn-implementation fast_gqa_fused` | [Fused Implementation](https://pytorch.org/torchtune/stable/_modules/torchtune/modules/attention.html) |
| **Torch Compile for Inference** | This  flag can now be used in the `interactive` and `generate` methods to enable faster inference by leveraging Torch's JIT compilation features. | `--torch-compile $mode` | - |
| **BF16** | The `--bf16` flag has been decoupled from `--tpu`, allowing independent training with `bfloat16`. Note that for models pretrained using `fp16`, `bf16` inference or fine-tuning may produce **highly** unpredictable results. | `--bf16` | - |




## Upcoming features ($\alpha$-testing)
* `StableAdam` [(Wortsman _et al._)](https://openreview.net/forum?id=sqqASmpA2R) which does not require gradient clipping can be activated with the flag `--adam-stable` and disabling any `--clip-norm`.


# Requirements and Installation

* [PyTorch](http://pytorch.org/) version >= 2.1.0
* Python version >= 3.8, <= 3.12
* For training new models, you'll also need an NVIDIA GPU and [NCCL](https://github.com/NVIDIA/nccl)
* **To install fairseq** and develop locally **(Please do not tamper with the `setup.py`)**:

``` bash
git clone https://github.com/VarunGumma/fairseq
cd fairseq
pip install -e ./
```

or **To install directly**:

```bash
pip install git+https://github.com/VarunGumma/fairseq.git
```

* **For faster training** install NVIDIA's [apex](https://github.com/NVIDIA/apex) library:

``` bash
git clone https://github.com/NVIDIA/apex
cd apex
pip install -v --no-cache-dir --global-option="--cpp_ext" --global-option="--cuda_ext" \
  --global-option="--deprecated_fused_adam" --global-option="--xentropy" \
  --global-option="--fast_multihead_attn" ./
```

* **For large datasets** install [PyArrow](https://arrow.apache.org/docs/python/install.html#using-pip): `pip install pyarrow`
* If you use Docker make sure to increase the shared memory size either with `--ipc=host` or `--shm-size`
 as command line options to `nvidia-docker run`.

# License

`fairseq(-py)` is MIT-licensed.
The license applies to the pre-trained models as well.

# Citation

Please cite as:
``` bibtex
@inproceedings{gumma-etal-2023-empirical,
    title = "An Empirical Study of Leveraging Knowledge Distillation for Compressing Multilingual Neural Machine Translation Models",
    author = "Gumma, Varun  and
      Dabre, Raj  and
      Kumar, Pratyush",
    editor = "Nurminen, Mary  and
      Brenner, Judith  and
      Koponen, Maarit  and
      Latomaa, Sirkku  and
      Mikhailov, Mikhail  and
      Schierl, Frederike  and
      Ranasinghe, Tharindu  and
      Vanmassenhove, Eva  and
      Vidal, Sergi Alvarez  and
      Aranberri, Nora  and
      Nunziatini, Mara  and
      Escart{\'\i}n, Carla Parra  and
      Forcada, Mikel  and
      Popovic, Maja  and
      Scarton, Carolina  and
      Moniz, Helena",
    booktitle = "Proceedings of the 24th Annual Conference of the European Association for Machine Translation",
    month = jun,
    year = "2023",
    address = "Tampere, Finland",
    publisher = "European Association for Machine Translation",
    url = "https://aclanthology.org/2023.eamt-1.11",
    pages = "103--114",
    abstract = "Knowledge distillation (KD) is a well-known method for compressing neural models. However, works focusing on distilling knowledge from large multilingual neural machine translation (MNMT) models into smaller ones are practically nonexistent, despite the popularity and superiority of MNMT. This paper bridges this gap by presenting an empirical investigation of knowledge distillation for compressing MNMT models. We take Indic to English translation as a case study and demonstrate that commonly used language-agnostic and language-aware KD approaches yield models that are 4-5x smaller but also suffer from performance drops of up to 3.5 BLEU. To mitigate this, we then experiment with design considerations such as shallower versus deeper models, heavy parameter sharing, multistage training, and adapters. We observe that deeper compact models tend to be as good as shallower non-compact ones and that fine-tuning a distilled model on a high-quality subset slightly boosts translation quality. Overall, we conclude that compressing MNMT models via KD is challenging, indicating immense scope for further research.",
}
```
```bibtex
@inproceedings{ott2019fairseq,
  title = {fairseq: A Fast, Extensible Toolkit for Sequence Modeling},
  author = {Myle Ott and Sergey Edunov and Alexei Baevski and Angela Fan and Sam Gross and Nathan Ng and David Grangier and Michael Auli},
  booktitle = {Proceedings of NAACL-HLT 2019: Demonstrations},
  year = {2019},
}
```
and please add a footnote url to this repository.

# Final Note

_I will try my best to keep this repo synced with the upstream [fairseq](https://github.com/facebookresearch/fairseq) repository. This clone is very dynamic and can have broken stuff once in a while. So feel free to raise issues or pull requests to clear any bugs or introduce new features._
