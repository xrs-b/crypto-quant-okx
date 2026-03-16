"""策略治理：升级/降级建议、审批逻辑、日报摘要"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from core.config import Config
from core.database import Database
from core.presets import PresetManager
from analytics.optimizer import ParameterOptimizer
from analytics.backtest import StrategyBacktester, SignalQualityAnalyzer


class GovernanceEngine:
    def __init__(self, config: Config, db: Optional[Database] = None):
        self.config = config
        self.db = db or Database(config.db_path)
        self.optimizer = ParameterOptimizer(config, self.db)
        self.preset_manager = PresetManager(config)
        self.backtester = StrategyBacktester(config)
        self.signal_quality = SignalQualityAnalyzer(config, self.db)

    def evaluate(self, use_cache: bool = True, persist: bool = True) -> Dict:
        optimizer_result = self.optimizer.run(use_cache=use_cache)
        mode = self.preset_manager.status()
        current_preset = mode.get('current_preset', 'manual')
        focused_sets = {x['name']: x for x in optimizer_result.get('focused_sets', [])}
        btc_grid = optimizer_result.get('btc_grid', [])
        promotions = optimizer_result.get('candidate_promotions', [])
        alerts = []

        current_main = focused_sets.get('btc_only') if mode.get('watch_list') == ['BTC/USDT'] else None
        best_btc_grid = btc_grid[0] if btc_grid else None

        upgrade_candidate = self._assess_btc_grid_upgrade(current_preset, current_main, best_btc_grid)
        if upgrade_candidate:
            alerts.append(self._attach_approval_status(upgrade_candidate))

        pool_switch = self._assess_pool_switch(mode, focused_sets, promotions)
        if pool_switch:
            alerts.append(self._attach_approval_status(pool_switch))

        downgrade = self._assess_main_pool_downgrade(mode, current_main)
        if downgrade:
            alerts.append(self._attach_approval_status(downgrade))

        alerts = [self._attach_approval_status(x) for x in alerts if x]
        result = {
            'mode': mode,
            'upgrade_candidate': alerts[0] if len(alerts) > 0 else None,
            'pool_switch_review': next((x for x in alerts if x.get('type') == 'pool_switch'), None),
            'downgrade_review': next((x for x in alerts if x.get('type') == 'main_pool_downgrade'), None),
            'alerts': alerts,
            'generated_at': datetime.now().isoformat(),
        }
        if persist:
            self._record_governance(result)
        return result

    def _assess_btc_grid_upgrade(self, current_preset: str, current_main: Optional[Dict], best_btc_grid: Optional[Dict]) -> Optional[Dict]:
        if not current_main or not best_btc_grid:
            return None
        current_score = float(current_main.get('score', -999) or -999)
        candidate_score = float(best_btc_grid.get('score', -999) or -999)
        candidate_dd = abs(float(best_btc_grid.get('summary', {}).get('max_drawdown_pct', 0) or 0))
        current_dd = abs(float(current_main.get('summary', {}).get('max_drawdown_pct', 0) or 0))
        better_enough = candidate_score > current_score + 1.0
        risk_ok = candidate_dd <= current_dd + 1.0
        if better_enough and risk_ok:
            return {
                'type': 'btc_grid_upgrade',
                'level': 'info',
                'approval_required': True,
                'recommended_preset': 'btc-grid-candidate',
                'message': 'BTC 网格候选优于当前主池基线，可提交升级审批',
                'current_score': round(current_score, 4),
                'candidate_score': round(candidate_score, 4),
            }
        return {
            'type': 'btc_grid_upgrade',
            'level': 'muted',
            'approval_required': False,
            'recommended_preset': None,
            'message': 'BTC 网格候选暂未明显优于当前主池，继续观察',
            'current_score': round(current_score, 4),
            'candidate_score': round(candidate_score, 4),
        }

    def _assess_pool_switch(self, mode: Dict, focused_sets: Dict, promotions: List[Dict]) -> Optional[Dict]:
        current_watch = (mode.get('watch_list') or [])
        candidate_watch = (mode.get('candidate_watch_list') or [])
        current_symbol = current_watch[0] if current_watch else None
        candidate_symbol = candidate_watch[0] if candidate_watch else None
        focused_mode = mode.get('selection_mode') == 'focused'
        if not current_symbol or not candidate_symbol:
            return None

        current_focus = self._find_focused_set_for_symbol(focused_sets, current_symbol)
        candidate_focus = self._find_focused_set_for_symbol(focused_sets, candidate_symbol)
        candidate_promotion = next((x for x in promotions if x.get('symbol') == candidate_symbol), None)
        if not current_focus or not candidate_focus:
            return None

        current_score = float(current_focus.get('score', -999) or -999)
        candidate_score = float(candidate_focus.get('score', -999) or -999)
        score_margin = float(self.config.get('governance.pool_switch.score_margin', 1.0) or 1.0)
        promote_passed = bool(candidate_promotion and candidate_promotion.get('decision') == 'promote')
        score_passed = candidate_score > current_score + score_margin
        candidate_in_pool = candidate_symbol in candidate_watch
        hold_passed, hold_detail, next_recheck_at = self._check_pool_switch_hold_window(mode)
        diagnosis = self._build_pool_switch_diagnosis(
            mode=mode,
            current_symbol=current_symbol,
            candidate_symbol=candidate_symbol,
            current_score=current_score,
            candidate_score=candidate_score,
            candidate_promotion=candidate_promotion,
            promote_passed=promote_passed,
            score_passed=score_passed,
            candidate_in_pool=candidate_in_pool,
            focused_mode=focused_mode,
            hold_passed=hold_passed,
            hold_detail=hold_detail,
            next_recheck_at=next_recheck_at,
            score_margin=score_margin,
        )
        target_preset = 'xrp-candidate' if candidate_symbol == 'XRP/USDT' else 'btc-focused' if candidate_symbol == 'BTC/USDT' else None
        candidate_label = candidate_symbol.replace('/USDT', '') if candidate_symbol else '--'
        current_label = current_symbol.replace('/USDT', '') if current_symbol else '--'
        if promote_passed and score_passed and hold_passed and target_preset:
            return {
                'type': 'pool_switch',
                'level': 'warn',
                'approval_required': True,
                'recommended_preset': target_preset,
                'message': f'候选池 {candidate_label} 已达到升级条件，且单币得分优于 {current_label}，可申请切换主池',
                'current_score': round(current_score, 4),
                'candidate_score': round(candidate_score, 4),
                'score_margin': score_margin,
                'current_symbol': current_symbol,
                'candidate_symbol': candidate_symbol,
                'current_pool': current_watch,
                'candidate_pool': candidate_watch,
                'hold_window_passed': True,
                'next_recheck_at': next_recheck_at,
                'last_change_reason': diagnosis.get('summary'),
                'decision_path': diagnosis,
            }
        return {
            'type': 'pool_switch',
            'level': 'muted',
            'approval_required': False,
            'recommended_preset': None,
            'message': f'当前不建议切换主池，继续维持 {current_label}-focused',
            'current_score': round(current_score, 4),
            'candidate_score': round(candidate_score, 4),
            'score_margin': score_margin,
            'current_symbol': current_symbol,
            'candidate_symbol': candidate_symbol,
            'current_pool': current_watch,
            'candidate_pool': candidate_watch,
            'hold_window_passed': hold_passed,
            'next_recheck_at': next_recheck_at,
            'last_change_reason': diagnosis.get('summary'),
            'decision_path': diagnosis,
        }

    def _find_focused_set_for_symbol(self, focused_sets: Dict, symbol: str) -> Optional[Dict]:
        return next((row for row in focused_sets.values() if row.get('symbols') == [symbol]), None)

    def _check_pool_switch_hold_window(self, mode: Dict, min_hold_hours: int = None):
        min_hold_hours = int(min_hold_hours or self.config.get('governance.pool_switch.min_hold_hours', 6) or 6)
        last_applied_at = mode.get('last_applied_at')
        if not last_applied_at:
            return True, '缺少 last_applied_at，暂按可评估处理', None
        try:
            applied_at = datetime.fromisoformat(str(last_applied_at))
        except Exception:
            return True, 'last_applied_at 无法解析，暂按可评估处理', None
        next_recheck_at = applied_at + timedelta(hours=min_hold_hours)
        now = datetime.now()
        if now >= next_recheck_at:
            held_hours = round((now - applied_at).total_seconds() / 3600, 2)
            return True, f'已持有 {held_hours}h，超过最小观察期 {min_hold_hours}h', next_recheck_at.isoformat()
        remaining_hours = round((next_recheck_at - now).total_seconds() / 3600, 2)
        return False, f'最小观察期未满，还需等待约 {remaining_hours}h', next_recheck_at.isoformat()

    def _build_pool_switch_diagnosis(self, mode: Dict, current_symbol: str, candidate_symbol: str, current_score: float, candidate_score: float, candidate_promotion: Optional[Dict], promote_passed: bool, score_passed: bool, candidate_in_pool: bool, focused_mode: bool, hold_passed: bool, hold_detail: str, next_recheck_at: Optional[str], score_margin: float) -> Dict:
        candidate_label = candidate_symbol.replace('/USDT', '') if candidate_symbol else '--'
        current_label = current_symbol.replace('/USDT', '') if current_symbol else '--'
        promotion_reason = candidate_promotion.get('reason') if candidate_promotion else f'暂无 {candidate_label} 候选审查结果'
        candidate_decision = candidate_promotion.get('decision') if candidate_promotion else 'missing'
        gates = [
            {
                'key': 'candidate_watch_list',
                'label': f'候选池已挂入 {candidate_label}',
                'passed': candidate_in_pool,
                'detail': f'{candidate_label} 已在 candidate_watch_list 中' if candidate_in_pool else f'{candidate_label} 未出现在 candidate_watch_list 中',
            },
            {
                'key': 'selection_mode',
                'label': '当前运行模式允许聚焦切池',
                'passed': focused_mode,
                'detail': f"当前 selection_mode = {mode.get('selection_mode')}" if mode.get('selection_mode') else 'selection_mode 缺失',
            },
            {
                'key': 'hold_window',
                'label': '最小观察期已满足',
                'passed': hold_passed,
                'detail': hold_detail,
            },
            {
                'key': 'candidate_promotion',
                'label': '候选晋升审查通过',
                'passed': promote_passed,
                'detail': f"decision = {candidate_decision} ｜ {promotion_reason}",
            },
            {
                'key': 'score_compare',
                'label': f'{candidate_label} 聚焦得分高于 {current_label} 主池',
                'passed': score_passed,
                'detail': f"{candidate_label} {round(candidate_score, 4)} vs {current_label} {round(current_score, 4)} ｜ 需至少领先 {round(score_margin, 2)}",
            },
        ]
        blocker = next((g for g in gates if not g['passed']), None)
        summary = blocker['detail'] if blocker else '所有切池条件已通过，等待审批执行'
        return {
            'current_preset': mode.get('current_preset'),
            'current_pool': mode.get('watch_list', []),
            'candidate_pool': mode.get('candidate_watch_list', []),
            'current_symbol': current_symbol,
            'candidate_symbol': candidate_symbol,
            'current_score': round(current_score, 4),
            'candidate_score': round(candidate_score, 4),
            'score_margin': round(score_margin, 4),
            'promotion_decision': candidate_decision,
            'promotion_reason': promotion_reason,
            'hold_window_passed': hold_passed,
            'hold_window_hours': int(self.config.get('governance.pool_switch.min_hold_hours', 6) or 6),
            'hold_window_detail': hold_detail,
            'next_recheck_at': next_recheck_at,
            'summary': summary,
            'blocking_gate': blocker['key'] if blocker else None,
            'blocking_label': blocker['label'] if blocker else None,
            'gates': gates,
        }

    def _assess_main_pool_downgrade(self, mode: Dict, current_main: Optional[Dict]) -> Optional[Dict]:
        if not current_main:
            return None
        summary = current_main.get('summary', {})
        total_return = float(summary.get('total_return_pct', 0) or 0)
        drawdown = abs(float(summary.get('max_drawdown_pct', 0) or 0))
        if total_return <= -5 or drawdown >= 6:
            return {
                'type': 'main_pool_downgrade',
                'level': 'danger',
                'approval_required': True,
                'recommended_preset': 'safe-mode',
                'message': '主池表现恶化，建议降级到 safe-mode',
                'total_return_pct': round(total_return, 4),
                'max_drawdown_pct': round(drawdown, 4),
            }
        return {
            'type': 'main_pool_downgrade',
            'level': 'ok',
            'approval_required': False,
            'recommended_preset': None,
            'message': '主池表现暂时可接受，无需降级',
            'total_return_pct': round(total_return, 4),
            'max_drawdown_pct': round(drawdown, 4),
        }

    def _attach_approval_status(self, row: Dict) -> Dict:
        enriched = dict(row)
        approval_type = enriched.get('type')
        target = enriched.get('recommended_preset')
        latest = self.db.get_latest_approval(approval_type, target)
        if latest:
            enriched['approval_status'] = latest.get('decision')
            enriched['approval_last_at'] = latest.get('created_at')
            enriched['approval_pending'] = False if latest.get('decision') in ('approved', 'rejected') else True
        else:
            enriched['approval_status'] = 'pending' if enriched.get('approval_required') else 'not_required'
            enriched['approval_last_at'] = None
            enriched['approval_pending'] = bool(enriched.get('approval_required'))
        return enriched

    def generate_daily_summary(self, force_refresh: bool = False) -> Dict:
        today = datetime.now().strftime('%Y-%m-%d')
        if not force_refresh:
            latest = self.db.get_latest_daily_report(today)
            if latest and latest.get('summary'):
                return latest.get('summary')

        mode = self.preset_manager.status()
        governance = self.evaluate(use_cache=False, persist=False)
        signals = self.db.get_signals(limit=500)
        today_signals = [s for s in signals if str(s.get('created_at', '')).startswith(today)]
        executed_today = sum(1 for s in today_signals if s.get('executed'))
        filtered_today = sum(1 for s in today_signals if s.get('filtered'))
        quality = self.signal_quality.analyze(use_cache=False)
        report = {
            'date': today,
            'preset': mode.get('current_preset'),
            'selection_mode': mode.get('selection_mode'),
            'watch_list': mode.get('watch_list', []),
            'candidate_watch_list': mode.get('candidate_watch_list', []),
            'today_signals': len(today_signals),
            'executed_today': executed_today,
            'filtered_today': filtered_today,
            'candidate_reviews': governance.get('alerts', []),
            'quality_summary': quality.get('summary', {}),
            'generated_at': datetime.now().isoformat(),
        }
        self.db.record_daily_report(today, report)
        return report

    def _record_governance(self, result: Dict):
        for row in result.get('alerts', []):
            self.db.record_governance_decision(
                decision_type=row.get('type'),
                level=row.get('level'),
                approval_required=1 if row.get('approval_required') else 0,
                recommended_preset=row.get('recommended_preset'),
                message=row.get('message'),
                details=row,
            )
