# Qwen3-Embedding-4B on Apple Silicon for the LanceDB plugin

Research date: 2026-08-25 (Asia/Taipei)
Scope: primary/first-party sources only for the main technical claims; no model installation or benchmark was performed.
Target machine observed locally: Apple M1 Pro, 10 CPU cores, 16 GB unified memory.

Source snapshot used for reproducibility: Qwen BF16 revision `5cf2132abc99cad020ac570b19d031efec650f2b`; Qwen GGUF revision `f4602530db1d980e16da9d7d3a70294cf5c190be`; llama.cpp revision `f1357e49980f5462af9783164f3fdec407d90137`.

## Decision summary

**Yes, this Mac can run Qwen3-Embedding-4B locally, provided the production path uses an official quantized GGUF rather than the original BF16 checkpoint.** The best-supported route for this Node/LanceDB project is a local `llama-server` sidecar using Qwen's official GGUF and llama.cpp's Metal backend. Qwen documents both `llama-embedding` and `llama-server --embedding --pooling last`; llama.cpp states that Apple Silicon is a first-class platform and supports Metal. Sources: [Qwen official GGUF model card](https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF), [llama.cpp README](https://github.com/ggml-org/llama.cpp/blob/master/README.md).

Recommended pilot candidates on this 16 GB M1 Pro:

- `Q5_K_M` (2.69 GiB file) as the quality/size-balanced candidate.
- `Q4_K_M` (2.33 GiB) as the lower-memory default/fallback.
- `Q8_0` (3.99 GiB) as a higher-precision comparison point, not the universal default.
- Do not choose F16/BF16 as the packaged default on a 16 GB machine: weights alone are about 7.5 GiB, before the KV cache, Metal/PyTorch buffers, batching buffers, Python/runtime overhead, LanceDB, and the OS.

This quantization recommendation is a capacity recommendation, **not a claim of equal retrieval quality**. Qwen publishes the quantized files but does not publish model-specific MTEB results for each GGUF quantization in the official model card. Q4/Q5/Q8 must therefore be compared on the project's own Traditional Chinese/English retrieval corpus before production selection. Source: [Qwen official GGUF model card and file list](https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF/tree/main).

Full 32K context is a model capability, not a sensible default for this 16 GB product. For knowledge indexing, keep chunks much smaller (for example 512–1,024 tokens), batch by a bounded total token budget, and make 2K–8K the initial runtime context envelope. Long-context and high-concurrency settings require measured peak-memory gates.

## Official model facts

Qwen's official model card reports:

- Text embedding model with approximately 4B parameters; the Hub metadata reports 4,021,774,336 BF16 parameters.
- 36 layers, 32K supported sequence length, and more than 100 languages, explicitly including Simplified Chinese, Traditional Chinese, and Cantonese.
- Native embedding width 2,560, with Matryoshka Representation Learning (MRL) allowing a selected output width from 32 through 2,560.
- Instruction-aware retrieval. Qwen says query instructions usually improve downstream results by about 1%–5% and recommends writing multilingual-task instructions in English because most training instructions were English.

Sources: [Qwen3-Embedding-4B official model card](https://huggingface.co/Qwen/Qwen3-Embedding-4B), [Qwen official repository](https://github.com/QwenLM/Qwen3-Embedding), [official model configuration](https://huggingface.co/Qwen/Qwen3-Embedding-4B/blob/main/config.json).

The official Sentence Transformers configuration is important for compatibility:

- Queries use the stored prefix `Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:`; documents have an empty prompt.
- Pooling is the **last token**, not mean pooling.
- A normalization module follows pooling, and the declared similarity is cosine.
- Qwen's Transformers example also requires left padding, last-token pooling, and L2 normalization.

Sources: [query/document prompt configuration](https://huggingface.co/Qwen/Qwen3-Embedding-4B/blob/main/config_sentence_transformers.json), [pooling configuration](https://huggingface.co/Qwen/Qwen3-Embedding-4B/blob/main/1_Pooling/config.json), [module pipeline](https://huggingface.co/Qwen/Qwen3-Embedding-4B/blob/main/modules.json), [official model-card usage](https://huggingface.co/Qwen/Qwen3-Embedding-4B#usage).

These are index-format invariants. Changing the model revision, quantization, output dimension, prompt text, pooling, or normalization can change vectors and must create a new embedding fingerprint and trigger a controlled re-index. Query and document vectors must always be generated with the same frozen contract.

## Exact artifact sizes and precision choices

The original checkpoint contains two BF16 Safetensors shards totaling 8,043,592,088 bytes (about 7.49 GiB). The Hub configuration declares `torch_dtype: bfloat16`. Sources: [official BF16 repository tree](https://huggingface.co/Qwen/Qwen3-Embedding-4B/tree/main), [official configuration](https://huggingface.co/Qwen/Qwen3-Embedding-4B/blob/main/config.json).

Qwen's official GGUF repository provides these complete files:

| Artifact | Bytes | Approx. GiB |
|---|---:|---:|
| Q4_K_M | 2,496,703,776 | 2.33 |
| Q5_0 | 2,823,134,496 | 2.63 |
| Q5_K_M | 2,888,936,736 | 2.69 |
| Q6_K | 3,305,684,256 | 3.08 |
| Q8_0 | 4,279,660,224 | 3.99 |
| F16 | 8,049,889,824 | 7.50 |

Source: [Qwen official GGUF repository tree](https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF/tree/main).

The official repository labels all of these as Apache-2.0 artifacts, but it does not provide per-quant MTEB/CMTEB scores. Therefore:

- Do not state that Q4/Q5 is "the same quality" as BF16.
- Compare Q4_K_M, Q5_K_M, and Q8_0 against the BF16 or current Gemini baseline on a fixed evaluation set.
- Pin the exact model revision and verify the downloaded LFS SHA-256 before first use. The file tree exposes the SHA-256 object ID for every GGUF artifact.

## Memory estimate for this 16 GB M1 Pro

The model configuration has 36 layers, 8 KV heads, and head dimension 128. llama.cpp currently defaults K and V cache types to F16. Sources: [Qwen model configuration](https://huggingface.co/Qwen/Qwen3-Embedding-4B/blob/main/config.json), [llama.cpp cache defaults](https://github.com/ggml-org/llama.cpp/blob/master/common/common.h).

That gives a first-order F16 KV-cache estimate of:

`tokens × 36 layers × 8 KV heads × 128 values × 2 (K and V) × 2 bytes`

| Configured tokens | Estimated F16 KV cache |
|---:|---:|
| 2,048 | 0.281 GiB |
| 4,096 | 0.562 GiB |
| 8,192 | 1.125 GiB |
| 16,384 | 2.250 GiB |
| 32,768 | 4.500 GiB |

This is only the KV component. Actual peak memory is higher because it also includes model weights, graph/activation buffers, Metal allocations, runtime code, request batching, the embedding output, LanceDB, other OpenClaw processes, and macOS. llama.cpp distinguishes logical batch size (`--batch-size`) from physical micro-batch size (`--ubatch-size`), so increasing `-ub` can materially raise compute-buffer pressure. Source: [llama.cpp runtime arguments](https://github.com/ggml-org/llama.cpp/blob/master/common/arg.cpp).

Practical conclusions for this machine:

- Q4_K_M or Q5_K_M with typical 512–1,024-token chunks has comfortable weight headroom and is very likely to be usable alongside LanceDB.
- Q8_0 should also fit for normal chunk lengths, but must be measured under concurrent indexing and search load.
- Q5_K_M at 8K consumes about 3.82 GiB for file-backed weights plus the estimated F16 KV cache before other buffers. At 32K that subtotal is about 7.19 GiB; the remaining buffers and macOS make full-context operation much less predictable.
- F16 at 32K reaches roughly 12.0 GiB for weights plus the estimated KV cache alone, so it is not a safe packaged configuration on a 16 GB shared-memory system.
- Qwen's official GGUF example uses `-ub 8192`, but that is an example rather than a 16 GB Mac guarantee. Start the pilot at `--ctx-size 2048` or `4096` and `--ubatch-size 512` or `1024`, then raise only with measured RSS/Metal memory and latency.

These are engineering estimates, not measured throughput or peak-RSS results. A local benchmark is still required before any SLA is promised.

## Output dimensions and LanceDB storage impact

Qwen supports 32–2,560 dimensions through MRL, but the project must select and freeze one production width. The official Qwen materials do not publish a quality curve for every truncation width. Source: [Qwen model card](https://huggingface.co/Qwen/Qwen3-Embedding-4B#model-overview).

For float32 vectors, the raw vector payload is:

| Dimensions | Bytes/vector | Raw bytes at 1M vectors |
|---:|---:|---:|
| 2,560 | 10,240 | 10.24 GB (about 9.54 GiB) |
| 1,024 | 4,096 | 4.096 GB (about 3.81 GiB) |
| 768 | 3,072 | 3.072 GB (about 2.86 GiB) |

This excludes metadata and index overhead. Sentence Transformers exposes both `truncate_dim` and `batch_size`; its documentation says `encode()` defaults to batch size 32 and that the optimal value depends on hardware and input data. Source: [SentenceTransformer API](https://sbert.net/docs/package_reference/sentence_transformer/SentenceTransformer.html).

The llama.cpp server currently returns the model's pooled embedding and documents L2 normalization, but its documented embedding endpoint does not expose an MRL `dimensions` request option. If the product uses a smaller dimension with the llama.cpp route, the client must truncate consistently and L2-normalize the truncated vector again, then validate parity against Sentence Transformers before accepting the implementation. Sources: [llama.cpp server embeddings endpoint](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1embeddings-openai-compatible-embeddings-api), [Sentence Transformers truncation API](https://sbert.net/docs/package_reference/sentence_transformer/SentenceTransformer.html).

## Runtime support matrix

### llama.cpp + official GGUF: recommended

This is the strongest first-party path for this use case:

- Qwen publishes official GGUF quantizations and explicit commands using `llama-embedding` and `llama-server --embedding --pooling last`.
- llama.cpp merged explicit Qwen3-Embedding conversion/support work in [PR #15023](https://github.com/ggml-org/llama.cpp/pull/15023).
- llama.cpp identifies Apple Silicon as a first-class target with ARM NEON, Accelerate, and Metal optimization.
- The server exposes an OpenAI-compatible `/v1/embeddings` endpoint, accepts either one string or an array of strings, and returns L2-normalized pooled embeddings. This lets the existing JavaScript plugin use local HTTP without embedding Python into the process.
- llama.cpp supports a dedicated embeddings-only mode and an API key option. Bind the service to loopback and do not expose it to the LAN by default.

Sources: [Qwen official GGUF usage](https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF#llamacpp), [llama.cpp platform support](https://github.com/ggml-org/llama.cpp/blob/master/README.md), [llama.cpp embedding server API](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1embeddings-openai-compatible-embeddings-api), [llama.cpp CLI arguments](https://github.com/ggml-org/llama.cpp/blob/master/common/arg.cpp).

### Transformers / Sentence Transformers on PyTorch MPS: supported, but not the packaging default

Qwen officially documents Transformers `>=4.51.0` and Sentence Transformers `>=2.7.0`; older Transformers can fail with `KeyError: 'qwen3'`. PyTorch officially provides the `mps` device for GPU acceleration on macOS through Metal Performance Shaders. Sources: [Qwen official usage](https://huggingface.co/Qwen/Qwen3-Embedding-4B#usage), [PyTorch MPS documentation](https://docs.pytorch.org/docs/stable/notes/mps.html).

However, the Qwen card's memory-saving `flash_attention_2` suggestion is not a Mac recipe. The official FlashAttention implementation currently requires CUDA or ROCm (and Linux is the supported installation target), so this optimization must not be copied into an Apple Silicon installer. Source: [FlashAttention official README](https://github.com/Dao-AILab/flash-attention/blob/main/README.md#installation-and-features).

The BF16 checkpoint may load on a 16 GB Mac, but its 7.49 GiB weights leave much less operational margin than GGUF, and Qwen does not provide a first-party quantized MPS deployment recipe. Use this route as a correctness/reference runner, not the default always-on sidecar.

### MLX / MLX-LM: promising ecosystem, not a first-party embedding path yet

MLX is Apple's array framework for Apple Silicon and uses unified memory; MLX and MLX-LM are MIT licensed. MLX-LM officially describes itself as a package for **generating text and fine-tuning** LLMs, with model conversion and quantization. Sources: [Apple MLX README](https://github.com/ml-explore/mlx/blob/main/README.md), [Apple MLX-LM README](https://github.com/ml-explore/mlx-lm/blob/main/README.md), [MLX-LM license](https://github.com/ml-explore/mlx-lm/blob/main/LICENSE).

The MLX Community Hub contains `mlx-community/Qwen3-Embedding-4B-4bit-DWQ`, but Qwen's official model card and repository do not document an MLX embedding command or server, and MLX-LM's official interface is generation-oriented. Treat that artifact as an experimental alternative requiring independent pooling, normalization, dimension, and quality-parity validation—not as the default production route. Sources: [MLX Community artifact](https://huggingface.co/mlx-community/Qwen3-Embedding-4B-4bit-DWQ), [Qwen official repository](https://github.com/QwenLM/Qwen3-Embedding), [MLX-LM README](https://github.com/ml-explore/mlx-lm/blob/main/README.md).

The separate `mlx-embeddings` project is not an Apple or Qwen first-party package; it is GPL-3.0 licensed, and its repository has an unresolved long-context attention-mask memory fix under review. That creates both production-maturity and redistribution-review work, so it should not be bundled by default. Sources: [`mlx-embeddings` repository and license](https://github.com/Blaizzy/mlx-embeddings), [long-context memory PR #68](https://github.com/Blaizzy/mlx-embeddings/pull/68).

### TEI and vLLM: official examples, poor fit for this Mac-first bundle

Qwen documents vLLM `>=0.8.5` and Hugging Face Text Embeddings Inference (TEI) containers for NVIDIA GPU or CPU. These remain valid server deployment paths, but the provided examples are not Apple-Metal-native. TEI's CPU container can be evaluated for non-Mac servers, while llama.cpp is the more direct Apple Silicon route. Source: [Qwen official model card usage](https://huggingface.co/Qwen/Qwen3-Embedding-4B#usage).

## Packaging and licensing

Relevant first-party license declarations:

- Qwen3-Embedding-4B and Qwen's GGUF repository: Apache-2.0. Source: [BF16 model card](https://huggingface.co/Qwen/Qwen3-Embedding-4B), [GGUF model card](https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF).
- llama.cpp: MIT. Source: [llama.cpp LICENSE](https://github.com/ggml-org/llama.cpp/blob/master/LICENSE).
- Transformers and Sentence Transformers: Apache-2.0. Sources: [Transformers LICENSE](https://github.com/huggingface/transformers/blob/main/LICENSE), [Sentence Transformers LICENSE](https://github.com/huggingface/sentence-transformers/blob/master/LICENSE).
- MLX/MLX-LM: MIT. Sources: [MLX LICENSE](https://github.com/ml-explore/mlx/blob/main/LICENSE), [MLX-LM LICENSE](https://github.com/ml-explore/mlx-lm/blob/main/LICENSE).
- PyTorch: BSD-style license. Source: [PyTorch LICENSE](https://github.com/pytorch/pytorch/blob/main/LICENSE).

Apache-2.0 and MIT are compatible with commercial redistribution, subject to their notice, license-copy, attribution, and change-notice conditions. This is an engineering summary, not legal advice; the release artifact should receive a license-file audit.

"Pack it into the project" should mean an integrated installer and lifecycle manager, not committing a 2.5–8.0 GB model into Git or an npm archive. Recommended distribution design:

- Ship or fetch a pinned llama.cpp build for the detected OS/architecture.
- Download the chosen official GGUF on first setup, or offer an offline customer bundle.
- Pin both the Hugging Face model revision and file SHA-256; verify before activation.
- Store model files outside the Git checkout/package payload and provide disk-space checks, resumable download, uninstall, upgrade, and rollback.
- Include the Qwen Apache-2.0 and llama.cpp MIT notices in the installed product.
- Bind the local service to `127.0.0.1`, use a generated local API credential, enforce request/body/token limits, and never silently fall back to a cloud embedding API.

Local inference removes document/query transmission to Gemini or another embedding API after installation. The first model download still contacts the configured distribution origin unless an offline bundle is used; installers and telemetry must disclose and control all outbound traffic.

## Minimum pilot and release gates

Before making Qwen the single production embedding path:

- Reproducibility: pin model revision, GGUF hash, llama.cpp version/commit, prompt, pooling, normalization, dimension, context, and quantization in one embedding fingerprint.
- Correctness: verify output length, finite values, norm close to 1, deterministic repeat behavior, query-only instruction formatting, document formatting, and last-token pooling.
- Quality: evaluate Q4_K_M, Q5_K_M, Q8_0, and a full-precision/reference runner on a versioned corpus covering Traditional Chinese, English, code, long documents, near-duplicates, and access-controlled content. Report Recall@K, nDCG@K/MRR, and failure examples; do not rely only on global MTEB.
- Capacity: measure cold start, documents/second, p50/p95 query latency, peak unified memory, thermal throttling, and concurrent indexing/search on the M1 Pro 16 GB. Repeat at 2K, 4K, and 8K context and several micro-batch sizes.
- Compatibility: prove that the local HTTP sidecar handles arrays, partial failures, cancellation, restart, crash recovery, and version upgrades without corrupting or mixing indexes.
- Privacy/security: test with networking disabled after installation; confirm loopback-only binding, API authentication, token/body/concurrency limits, log redaction, no raw-document telemetry, and fail-closed behavior with no cloud fallback.
- Migration: build a new LanceDB table/index; do not append Qwen vectors to a Gemini index. Validate record counts and retrieval quality before atomic cutover, and retain a rollback path until acceptance.
- Licensing/supply chain: generate the final notices/SBOM, verify checksums/signatures where available, scan bundled binaries, and document the uninstaller and model-cache deletion behavior.

## Bottom line

For the current M1 Pro 16 GB machine, the project is technically viable as an all-local product. The evidence-backed implementation choice is **Qwen official GGUF + llama.cpp Metal + loopback OpenAI-compatible embeddings sidecar**. Begin with Q5_K_M and Q4_K_M as candidates, use short chunks and conservative micro-batches, and keep 32K as an opt-in tested ceiling rather than the default. Final quantization and vector dimension remain benchmark decisions, because the official sources do not publish per-quant or per-dimension quality parity data.
