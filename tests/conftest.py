"""Pytest configuration for test isolation."""
import os
import shutil
from pathlib import Path
import pytest


@pytest.fixture(autouse=True)
def isolate_local_config(monkeypatch):
    """Automatically disable local config override for all tests."""
    # Clear environment variables
    monkeypatch.delenv('CRYPTO_QUANT_OKX_HOME_LOCAL_CONFIG', raising=False)
    monkeypatch.delenv('CRYPTO_QUANT_OKX_ENABLE_HOME_LOCAL', raising=False)
    
    # Temporarily move local config
    local_config_path = Path('config/config.local.yaml')
    backup_path = Path('config/config.local.yaml.test_backup')
    
    if local_config_path.exists():
        shutil.move(str(local_config_path), str(backup_path))
    
    yield
    
    # Restore local config
    if backup_path.exists():
        shutil.move(str(backup_path), str(local_config_path))
