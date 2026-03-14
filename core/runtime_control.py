"""运行时控制：重启 bot / dashboard"""
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Dict


class RuntimeController:
    def __init__(self, project_dir: str = '/Volumes/MacHD/Projects/crypto-quant-okx'):
        self.project_dir = Path(project_dir)

    def restart_bot(self) -> Dict:
        cmd = (
            f"cd {self.project_dir} && "
            f"(pkill -f \"python3 .*bot/main.py\" 2>/dev/null || true) && "
            f"sleep 1 && nohup python3 bot/main.py > /tmp/okx-bot.log 2>&1 & echo $!"
        )
        out = subprocess.check_output(['/bin/zsh', '-lc', cmd], text=True).strip()
        return {'ok': True, 'pid': out}

    def restart_dashboard(self, port: int = 8050) -> Dict:
        cmd = (
            f"cd {self.project_dir} && "
            f"(ps aux | grep \"bot/run.py --dashboard --port {port}\" | grep -v grep | awk '{{print $2}}' | xargs -I{{}} kill {{}} 2>/dev/null || true) && "
            f"sleep 1 && nohup python3 bot/run.py --dashboard --port {port} > /tmp/okx-dashboard.log 2>&1 & echo $!"
        )
        out = subprocess.check_output(['/bin/zsh', '-lc', cmd], text=True).strip()
        return {'ok': True, 'pid': out, 'port': port}
