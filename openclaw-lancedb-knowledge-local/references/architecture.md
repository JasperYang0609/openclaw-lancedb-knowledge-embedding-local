# Qwen local architecture

Source Markdown is redacted and chunked locally, embedded through the authenticated llama.cpp sidecar on `127.0.0.1`, and stored in a Qwen-specific 768-dimensional LanceDB table. Native 2,560-dimensional vectors are truncated to 768 and L2-normalized.

The managed runtime identity is Qwen3-Embedding-4B Q5_K_M at immutable revision `f4602530...` plus official llama.cpp `b10625`. Artifact downloads are the only required network operation. Runtime embedding has no cloud fallback.

Managed runtime files live under the dedicated Qwen root. Index, cache and state live under Qwen-specific paths. The installer never reads or writes another provider's table, cache, configuration or schedule.

Trust boundaries are the public source repository, pinned upstream artifacts, local installer, loopback sidecar, source documents and local LanceDB. Checksums, restricted credentials, archive validation, manifest identity, PID identity and fail-closed uninstall enforce those boundaries.
