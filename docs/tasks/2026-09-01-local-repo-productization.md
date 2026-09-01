# Local repository productization task

Status: IMPLEMENTED_PENDING_REVIEW

Scope: unique local skill identity; local-only Qwen runtime; immutable artifact manifest; resumable downloader; secure archive extraction; hardened install and uninstall; single lifecycle CLI; tests, CI, archive and public documentation.

Forbidden: cloud embedding fallback, Gemini credentials or endpoints, production configuration/data mutation, model binaries in Git, unsupported-platform claims, force-push, merge or release publication.

Acceptance: fixture tests cover artifact identity, download argument safety, archive traversal, target boundaries, permissions, manifest identity, lifecycle and CLI redaction; Node/postrun/archive/security gates pass; repository is committed on the feature branch.

Stop conditions: artifact identity mismatch, unsafe target/extraction behavior, P0/P1 issue, secrets/large model in Git, or any need to reduce the signed quality or security gate.
