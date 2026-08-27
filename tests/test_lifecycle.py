import pytest
from src.lifecycle.llama_server_manager import LlamaServerManager
from unittest.mock import patch, MagicMock

def test_port_in_use():
    manager = LlamaServerManager(model_path="dummy.gguf", port=8080)
    with patch('socket.socket') as mock_sock:
        mock_instance = mock_sock.return_value.__enter__.return_value
        mock_instance.connect_ex.return_value = 0 # Port in use
        
        with pytest.raises(RuntimeError, match="already in use"):
            manager.start()

@patch('requests.get')
@patch('subprocess.Popen')
def test_start_success(mock_popen, mock_get):
    manager = LlamaServerManager(model_path="dummy.gguf", port=8080)
    
    # Mock network port free
    with patch.object(manager, '_is_port_in_use', return_value=False):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_get.return_value = mock_response
        
        assert manager.start() is True
        mock_popen.assert_called_once()
