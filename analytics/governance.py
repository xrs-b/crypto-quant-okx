"""策略治理：升级/降级建议、审批逻辑、日报摘要"""
from __future__ import annotations

from datetime import datetime
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

    def evaluate(self, use_cache: bool = True) -> Dict:
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
            alerts.append(upgrade_candidate)

        pool_switch = self._assess_pool_switch(mode, focused_sets, promotions)
        if pool_switch:
            alerts.append(pool_switch)

        downgrade = self._assess_main_pool_downgrade(mode, current_main)
        if downgrade:
            alerts.append(downgrade)

        result = {
            'mode': mode,
            'upgrade_candidate': upgrade_candidate,
            'pool_switch_review': pool_switch,
            'downgrade_review': downgrade,
            'alerts': alerts,
            'generated_at': datetime.now().isoformat(),
        }
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
        xrp_focus = focused_sets.get('xrp_only')
        btc_focus = focused_sets.get('btc_only')
        xrp_promotion = next((x for x in promotions if x.get('symbol') == 'XRP/USDT'), None)
        if not xrp_focus or not btc_focus:
            return None

        xrp_score = float(xrp_focus.get('score', -999) or -999)
        btc_score = float(btc_focus.get('score', -999) or -999)
        if xrp_promotion and xrp_promotion.get('decision') == 'promote' and xrp_score > btc_score:
            return {
                'type': 'pool_switch',
                'level': 'warn',
                'approval_required': True,
                'recommended_preset': 'xrp-candidate',
                'message': '候选池 XRP 已达到升级条件，且单币得分优于 BTC，可申请切换主池',
                'btc_score': round(btc_score, 4),
                'xrp_score': round(xrp_score, 4),
            }
        return {
            'type': 'pool_switch',
            'level': 'muted',
            'approval_required': False,
            'recommended_preset': None,
            'message': '当前不建议切换主池，继续维持 BTC-focused',
            'btc_score': round(btc_score, 4),
            'xrp_score': round(xrp_score, 4),
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

    def generate_daily_summary(self) -> Dict:
        mode = self.preset_manager.status()
        governance = self.evaluate(use_cache=False)
        signals = self.db.get_signals(limit=500)
        today = datetime.now().strftime('%Y-%m-%d')
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
