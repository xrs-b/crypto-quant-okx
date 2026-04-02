from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from analytics.parameter_tuning_advice import (
    build_parameter_tuning_advice_payload,
    format_parameter_tuning_advice,
)
from analytics.mtf_breakout_report import analyze_mtf_breakout_report, format_mtf_breakout_report
from analytics.parameter_tuning_patch import (
    build_parameter_tuning_patch_payload,
    format_parameter_tuning_patch_text,
)
from scripts.outcome_issue_summary import (
    DEFAULT_HOURS,
    DEFAULT_LIMIT,
    DEFAULT_SYMBOLS,
    build_outcome_issue_summary_payload,
)


def build_parameter_tuning_overview_payload(
    db_path: str,
    *,
    config_path: str | None = None,
    view: str = 'both',
    hours: float = DEFAULT_HOURS,
    limit: int = DEFAULT_LIMIT,
    symbols: Optional[Sequence[str]] = None,
    fetch_limit: Optional[int] = None,
) -> Dict[str, Any]:
    resolved_symbols = list(symbols or DEFAULT_SYMBOLS)
    issue_summary = build_outcome_issue_summary_payload(
        db_path,
        view=view,
        hours=hours,
        limit=limit,
        symbols=resolved_symbols,
        fetch_limit=fetch_limit,
    )
    advice = build_parameter_tuning_advice_payload(
        db_path,
        view=view,
        hours=hours,
        limit=limit,
        symbols=resolved_symbols,
        fetch_limit=fetch_limit,
    )
    patch_preview = build_parameter_tuning_patch_payload(
        db_path,
        config_path=config_path,
        view=view,
        hours=hours,
        limit=limit,
        symbols=resolved_symbols,
        fetch_limit=fetch_limit,
    )
    mtf_breakout_summary = analyze_mtf_breakout_report(
        db_path,
        limit=limit,
        hours=hours if view in {'both', 'hours'} else None,
        symbols=resolved_symbols,
        fetch_limit=fetch_limit,
    )
    return {
        'schema_version': 'parameter_tuning_overview_v1',
        'mode': 'read_only_overview',
        'db_path': db_path,
        'config_path': patch_preview.get('config_path'),
        'view': view,
        'hours': hours,
        'limit': limit,
        'symbols': resolved_symbols,
        'fetch_limit': fetch_limit,
        'issue_summary': issue_summary,
        'parameter_advice': advice,
        'patch_preview': patch_preview,
        'mtf_breakout_summary': mtf_breakout_summary,
        'text': format_parameter_tuning_overview_text(
            issue_summary=issue_summary,
            advice=advice,
            patch_preview=patch_preview,
            mtf_breakout_summary=mtf_breakout_summary,
        ),
    }


def format_parameter_tuning_overview_text(*, issue_summary: Dict[str, Any], advice: Dict[str, Any], patch_preview: Dict[str, Any], mtf_breakout_summary: Dict[str, Any]) -> str:
    return '\n\n'.join(
        [
            'A. 问题摘要 / Issue summary\n' + (issue_summary.get('text') or '(no issue summary)'),
            'B. 参数建议 / Parameter advice\n' + format_parameter_tuning_advice(advice),
            'C. Patch 预览 / Patch preview\n' + format_parameter_tuning_patch_text(patch_preview),
            'D. MTF Breakout 观察摘要 / Observe-only summary\n' + format_mtf_breakout_report(mtf_breakout_summary),
        ]
    )
