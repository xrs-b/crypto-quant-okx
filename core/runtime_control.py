"""运行时控制：重启 bot / dashboard"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict

from core.paths import PROJECT_DIR


class RuntimeController:
    def __init__(self, project_dir: str | None = None):
        resolved = (project_dir or os.getenv('PROJECT_DIR') or '').strip()
        self.project_dir = Path(resolved).expanduser().resolve() if resolved else PROJECT_DIR

    def restart_bot(self) -> Dict:
        venv_python = self.project_dir / '.venv/bin/python3'
        cmd = (
            f"cd {self.project_dir} && "
            f"(pkill -f \"bot/run.py --daemon\" 2>/dev/null || true) && "
            f"sleep 1 && nohup {venv_python} {self.project_dir / 'bot/run.py'} --daemon > /tmp/okx-bot.log 2>&1 & echo $!"
        )
        out = subprocess.check_output(['/bin/zsh', '-lc', cmd], text=True).strip()
        return {'ok': True, 'pid': out, 'project_dir': str(self.project_dir)}

    def restart_dashboard(self, port: int = 5555) -> Dict:
        venv_flask = self.project_dir / '.venv/bin/flask'
        cmd = (
            f"cd {self.project_dir} && "
            f"(pkill -f \"flask --app dashboard.api:app run --host 0.0.0.0 --port {port}\" 2>/dev/null || true) && "
            f"sleep 1 && nohup {venv_flask} --app dashboard.api:app run --host 0.0.0.0 --port {port} > /tmp/okx-dashboard.log 2>&1 & echo $!"
        )
        out = subprocess.check_output(['/bin/zsh', '-lc', cmd], text=True).strip()
        return {'ok': True, 'pid': out, 'port': port, 'project_dir': str(self.project_dir)}
