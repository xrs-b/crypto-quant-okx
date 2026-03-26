"""项目路径辅助：支持 PROJECT_DIR 覆盖，默认自动推导仓库根目录。"""
from __future__ import annotations

import os
from pathlib import Path


def get_project_dir() -> Path:
    env_value = (os.getenv('PROJECT_DIR') or '').strip()
    if env_value:
        return Path(env_value).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


PROJECT_DIR = get_project_dir()
DATA_DIR = PROJECT_DIR / 'data'
LOGS_DIR = PROJECT_DIR / 'logs'
CONFIG_DIR = PROJECT_DIR / 'config'
