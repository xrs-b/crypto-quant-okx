"""Preset 管理与模式状态"""
from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from core.config import Config


class PresetManager:
    def __init__(self, config: Optional[Config] = None):
        self.config = config or Config()
        self.config_path = Path(self.config.config_path)
        self.presets_dir = self.config_path.parent / 'presets'
        self.backups_dir = self.config_path.parent / 'backups'
        self.presets_dir.mkdir(parents=True, exist_ok=True)
        self.backups_dir.mkdir(parents=True, exist_ok=True)

    def list_presets(self) -> List[Dict]:
        rows = []
        for path in sorted(self.presets_dir.glob('*.yaml')):
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f) or {}
                symbols = data.get('symbols', {})
                rows.append({
                    'name': path.stem,
                    'path': str(path),
                    'watch_list': symbols.get('watch_list', []),
                    'candidate_watch_list': symbols.get('candidate_watch_list', []),
                    'paused_watch_list': symbols.get('paused_watch_list', []),
                })
            except Exception:
                rows.append({'name': path.stem, 'path': str(path), 'watch_list': [], 'candidate_watch_list': [], 'paused_watch_list': []})
        return rows

    def apply_preset(self, name: str) -> Dict:
        preset_path = self.presets_dir / f'{name}.yaml'
        if not preset_path.exists():
            raise FileNotFoundError(f'Preset 不存在: {name}')

        timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        backup_path = self.backups_dir / f'config-{timestamp}.yaml'
        if self.config_path.exists():
            shutil.copy2(self.config_path, backup_path)

        with open(preset_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f) or {}

        data.setdefault('runtime_meta', {})['current_preset'] = name
        data['runtime_meta']['last_applied_at'] = datetime.now().isoformat()
        data['runtime_meta']['last_backup'] = str(backup_path)

        with open(self.config_path, 'w', encoding='utf-8') as f:
            yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

        self.config.reload()
        return {
            'applied': name,
            'config_path': str(self.config_path),
            'backup_path': str(backup_path),
            'watch_list': data.get('symbols', {}).get('watch_list', []),
        }

    def status(self) -> Dict:
        self.config.reload()
        symbols = self.config.get('symbols', {})
        return {
            'current_preset': self.config.get('runtime_meta.current_preset', 'manual'),
            'selection_mode': symbols.get('selection_mode', 'broad'),
            'watch_list': symbols.get('watch_list', []),
            'candidate_watch_list': symbols.get('candidate_watch_list', []),
            'paused_watch_list': symbols.get('paused_watch_list', []),
            'last_applied_at': self.config.get('runtime_meta.last_applied_at'),
        }
