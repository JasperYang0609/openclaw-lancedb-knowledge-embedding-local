import subprocess
import time
import requests
import socket
import os
import signal

class LlamaServerManager:
    def __init__(self, model_path, port=8080):
        self.model_path = model_path
        self.port = port
        self.process = None

    def _is_port_in_use(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('127.0.0.1', self.port)) == 0

    def start(self):
        if self._is_port_in_use():
            raise RuntimeError(f"Port {self.port} is already in use.")
        
        # Security: Loopback only, embeddings only
        cmd = [
            "./llama-server",
            "-m", self.model_path,
            "--port", str(self.port),
            "--host", "127.0.0.1",
            "--embedding",
            "--pooling", "none" # Use last token for Qwen if supported, or standard
        ]
        
        self.process = subprocess.Popen(
            cmd, 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL
        )
        
        # Wait for health check
        for _ in range(30):
            if self.is_healthy():
                return True
            time.sleep(1)
        
        self.stop()
        raise RuntimeError("Server failed to become healthy within 30 seconds.")

    def is_healthy(self):
        try:
            resp = requests.get(f"http://127.0.0.1:{self.port}/health", timeout=1)
            return resp.status_code == 200
        except:
            return False

    def stop(self):
        if self.process:
            self.process.send_signal(signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            self.process = None
