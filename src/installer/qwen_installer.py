import os
import hashlib

class QwenInstaller:
    EXPECTED_SHA256 = "b7f7e91501869e2cbdeef68c37d6e5225e52c8b871c4c1a5e7ec9cd3deee3a65" # Mock hash for test
    
    def __init__(self, target_dir):
        self.target_dir = target_dir
        self.model_path = os.path.join(self.target_dir, "qwen3-embedding-4b-q5_k_m.gguf")
        
    def verify_hash(self, file_path):
        if not os.path.exists(file_path):
            return False
        hasher = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest() == self.EXPECTED_SHA256

    def install(self):
        # In a real scenario, this would download the file.
        # For our 5-day setup script, we will symlink the already downloaded POC model
        poc_model = "/Users/as_openclaw/.openclaw/workspace/poc-qwen/qwen3-embedding-4b-q5_k_m.gguf"
        
        os.makedirs(self.target_dir, exist_ok=True)
        if not os.path.exists(self.model_path):
             if os.path.exists(poc_model):
                 os.symlink(poc_model, self.model_path)
             else:
                 raise FileNotFoundError("POC model not found, cannot proceed without downloading.")
                 
        return True
