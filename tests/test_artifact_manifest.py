from src.installer.artifacts import LLAMA_CPP, QWEN_MODEL, validate_manifest


def test_pinned_artifact_manifest() -> None:
    validate_manifest()
    assert QWEN_MODEL.size == 2_888_936_736
    assert QWEN_MODEL.sha256 == "9fd05563211c2d69d74abb8769fa92983a102d11575b2517a119b0037dff217c"
    assert QWEN_MODEL.revision in QWEN_MODEL.url
    assert LLAMA_CPP.size == 10_955_118
    assert LLAMA_CPP.sha256 == "f13c74d104c1ff2e37a14ecb2025afe5c9c4c148064badfd8116376018dd5159"
    assert "/b10625/" in LLAMA_CPP.url
