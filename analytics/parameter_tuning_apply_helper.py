from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from difflib import unified_diff
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

import yaml

from analytics.parameter_tuning_patch import build_parameter_tuning_patch_payload


@dataclass(frozen=True)
class PatchConflict:
    path: str
    first_scope: str
    second_scope: str
    first_value: Any
    second_value: Any


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _flatten_patch(data: Dict[str, Any], prefix: str = '') -> Dict[str, Any]:
    flattened: Dict[str, Any] = {}
    for key, value in (data or {}).items():
        path = f'{prefix}.{key}' if prefix else str(key)
        if isinstance(value, dict):
            flattened.update(_flatten_patch(value, path))
        else:
            flattened[path] = value
    return flattened


def resolve_target_config_path(config_path: str | None = None, target_config_path: str | None = None) -> Path:
    if target_config_path:
        return Path(target_config_path).expanduser().resolve()
    base_path = Path(config_path).expanduser().resolve() if config_path else (Path(__file__).resolve().parent.parent / 'config' / 'config.yaml')
    return base_path.with_name('config.local.yaml')


def collect_symbol_patch_draft(
    payload: Dict[str, Any],
    *,
    symbols: Optional[Sequence[str]] = None,
    scopes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    selected_symbols = list(symbols or payload.get('symbols') or [])
    selected_scopes = set(scopes or (payload.get('views') or {}).keys())
    merged_patch: Dict[str, Any] = {}
    flat_seen: Dict[str, tuple[str, Any]] = {}
    conflicts: List[PatchConflict] = []
    applied_items: List[Dict[str, Any]] = []

    for scope_name, scope_payload in (payload.get('views') or {}).items():
        if scope_name not in selected_scopes:
            continue
        for symbol_payload in scope_payload.get('symbols') or []:
            symbol = symbol_payload.get('symbol')
            if symbol not in selected_symbols:
                continue
            yaml_patch = symbol_payload.get('yaml_patch') or {}
            if not yaml_patch:
                continue
            flat_patch = _flatten_patch(yaml_patch)
            symbol_has_conflict = False
            for path, value in flat_patch.items():
                previous = flat_seen.get(path)
                if previous is None:
                    flat_seen[path] = (scope_name, value)
                    continue
                previous_scope, previous_value = previous
                if previous_value != value:
                    conflicts.append(
                        PatchConflict(
                            path=path,
                            first_scope=previous_scope,
                            second_scope=scope_name,
                            first_value=previous_value,
                            second_value=value,
                        )
                    )
                    symbol_has_conflict = True
            if symbol_has_conflict:
                continue
            merged_patch = _deep_merge(merged_patch, yaml_patch)
            applied_items.append(
                {
                    'scope': scope_name,
                    'symbol': symbol,
                    'yaml_patch': yaml_patch,
                    'change_reviews': [
                        item for item in (symbol_payload.get('change_reviews') or []) if item.get('changed')
                    ],
                }
            )

    return {
        'symbols': selected_symbols,
        'scopes': sorted(selected_scopes),
        'patch': merged_patch,
        'items': applied_items,
        'conflicts': [
            {
                'path': item.path,
                'first_scope': item.first_scope,
                'second_scope': item.second_scope,
                'first_value': item.first_value,
                'second_value': item.second_value,
            }
            for item in conflicts
        ],
    }


def render_patch_selection_text(selection: Dict[str, Any]) -> str:
    lines = [
        'Parameter tuning apply helper',
        f"Scopes: {', '.join(selection.get('scopes') or []) or '(none)'}",
        f"Symbols: {', '.join(selection.get('symbols') or []) or '(none)'}",
    ]
    conflicts = selection.get('conflicts') or []
    if conflicts:
        lines.append('Conflicts detected across scopes:')
        for conflict in conflicts:
            lines.append(
                f"  - {conflict['path']}: {conflict['first_scope']}={conflict['first_value']} vs {conflict['second_scope']}={conflict['second_value']}"
            )
        lines.append('Refine --view / --symbol before applying anything.')
        return '\n'.join(lines)

    if not selection.get('patch'):
        lines.append('No concrete patch entries selected.')
        return '\n'.join(lines)

    for item in selection.get('items') or []:
        lines.append(f"[{item['scope']}] {item['symbol']}")
        for review in item.get('change_reviews') or []:
            lines.append(
                f"  - {review['parameter']}: {review['current']} -> {review['suggested']} ({review['patch_path']})"
            )
    lines.append('Merged patch YAML:')
    for patch_line in yaml.safe_dump(selection.get('patch') or {}, allow_unicode=True, sort_keys=False).rstrip().splitlines():
        lines.append(f'  {patch_line}')
    return '\n'.join(lines)


def build_apply_plan(
    db_path: str,
    *,
    config_path: str | None = None,
    target_config_path: str | None = None,
    view: str = 'both',
    hours: float = 24.0,
    limit: int = 50,
    symbols: Optional[Sequence[str]] = None,
    fetch_limit: Optional[int] = None,
    scopes: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    payload = build_parameter_tuning_patch_payload(
        db_path,
        config_path=config_path,
        view=view,
        hours=hours,
        limit=limit,
        symbols=symbols,
        fetch_limit=fetch_limit,
    )
    selection = collect_symbol_patch_draft(payload, symbols=symbols, scopes=scopes)
    target_path = resolve_target_config_path(payload.get('config_path'), target_config_path)
    return {
        'payload': payload,
        'selection': selection,
        'target_config_path': str(target_path),
    }


def _dump_yaml(data: Dict[str, Any]) -> str:
    dumped = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    return dumped if dumped.endswith('\n') else dumped + '\n'


def _read_yaml_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding='utf-8'))
    return loaded or {}


def build_apply_preview(plan: Dict[str, Any]) -> Dict[str, Any]:
    target_path = Path(plan['target_config_path'])
    before_data = _read_yaml_file(target_path)
    patch = plan.get('selection', {}).get('patch') or {}
    after_data = _deep_merge(before_data, patch)
    before_text = _dump_yaml(before_data)
    after_text = _dump_yaml(after_data)
    diff_lines = list(
        unified_diff(
            before_text.splitlines(),
            after_text.splitlines(),
            fromfile=f'{target_path} (before)',
            tofile=f'{target_path} (after)',
            lineterm='',
        )
    )
    return {
        'target_config_path': str(target_path),
        'exists': target_path.exists(),
        'before_data': before_data,
        'after_data': after_data,
        'before_text': before_text,
        'after_text': after_text,
        'diff_text': '\n'.join(diff_lines) if diff_lines else '(no diff)',
        'has_changes': before_data != after_data,
        'patch': patch,
    }


def apply_patch_plan(plan: Dict[str, Any]) -> Dict[str, Any]:
    preview = build_apply_preview(plan)
    target_path = Path(preview['target_config_path'])
    backup_dir = target_path.parent / '.backups'
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    backup_path = backup_dir / f'{target_path.name}.{timestamp}.bak'
    backup_path.write_text(preview['before_text'], encoding='utf-8')

    if preview['has_changes']:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(preview['after_text'], encoding='utf-8')

    result = dict(preview)
    result.update(
        {
            'backup_path': str(backup_path),
            'applied': bool(preview['has_changes']),
        }
    )
    return result


def format_apply_preview(plan: Dict[str, Any], preview: Dict[str, Any], *, apply: bool) -> str:
    selection = plan.get('selection') or {}
    lines = [
        render_patch_selection_text(selection),
        '',
        f"Target config: {preview.get('target_config_path')}",
        f"Mode: {'APPLY' if apply else 'DRY-RUN / PREVIEW'}",
    ]
    if preview.get('patch') and not selection.get('conflicts'):
        lines.append(f"Target existed before apply: {'yes' if preview.get('exists') else 'no'}")
        lines.append(f"Has effective changes: {'yes' if preview.get('has_changes') else 'no'}")
        lines.append('Unified diff:')
        lines.append(preview.get('diff_text') or '(no diff)')
    if apply:
        lines.append(f"Backup path: {preview.get('backup_path')}")
        lines.append(f"Write status: {'updated' if preview.get('applied') else 'no-op'}")
    else:
        lines.append('Safety: nothing was written; rerun with --apply to persist this draft.')
    return '\n'.join(lines)
