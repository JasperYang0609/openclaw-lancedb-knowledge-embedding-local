from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    filename: str
    url: str
    size: int
    sha256: str
    revision: str
    allowed_host: str

    def validate(self) -> None:
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or parsed.hostname != self.allowed_host:
            raise ValueError(f"{self.artifact_id} must use its fixed HTTPS host")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"{self.artifact_id} URL contains forbidden components")
        if self.revision not in parsed.path or parsed.path.endswith(("/latest", "/main")):
            raise ValueError(f"{self.artifact_id} URL must contain the immutable revision")
        if not parsed.path.endswith("/" + self.filename):
            raise ValueError(f"{self.artifact_id} filename does not match the fixed URL")
        if self.size <= 0 or len(self.sha256) != 64:
            raise ValueError(f"{self.artifact_id} has an invalid size or digest")


QWEN_MODEL = Artifact(
    artifact_id="qwen3-embedding-4b-q5-k-m",
    filename="Qwen3-Embedding-4B-Q5_K_M.gguf",
    url=(
        "https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF/resolve/"
        "f4602530db1d980e16da9d7d3a70294cf5c190be/Qwen3-Embedding-4B-Q5_K_M.gguf"
    ),
    size=2_888_936_736,
    sha256="9fd05563211c2d69d74abb8769fa92983a102d11575b2517a119b0037dff217c",
    revision="f4602530db1d980e16da9d7d3a70294cf5c190be",
    allowed_host="huggingface.co",
)

LLAMA_CPP = Artifact(
    artifact_id="llama-cpp-b10625-macos-arm64",
    filename="llama-b10625-bin-macos-arm64.tar.gz",
    url="https://github.com/ggml-org/llama.cpp/releases/download/b10625/llama-b10625-bin-macos-arm64.tar.gz",
    size=10_955_118,
    sha256="f13c74d104c1ff2e37a14ecb2025afe5c9c4c148064badfd8116376018dd5159",
    revision="b10625",
    allowed_host="github.com",
)

ARTIFACTS = (QWEN_MODEL, LLAMA_CPP)


def validate_manifest() -> None:
    seen: set[str] = set()
    for artifact in ARTIFACTS:
        artifact.validate()
        if artifact.artifact_id in seen:
            raise ValueError(f"duplicate artifact id: {artifact.artifact_id}")
        seen.add(artifact.artifact_id)
