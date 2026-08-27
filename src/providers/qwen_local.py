import os

class QwenLocalProvider:
    def __init__(self, root_dir=None):
        self.provider_id = "qwen-local"
        self.root_dir = root_dir or os.environ.get("LANCEDB_SHADOW_ROOT")
        if not self.root_dir:
            raise ValueError("LANCEDB_SHADOW_ROOT must be set for QwenLocalProvider to ensure isolation.")
        if "gemini" in str(self.root_dir).lower() or self.root_dir == "/Users/as_openclaw/.openclaw/workspace/lancedb/gemini":
             raise PermissionError("Isolation violation: Attempted to use Production Gemini directory.")

    def embed(self, text):
        return [0.0] * 768

