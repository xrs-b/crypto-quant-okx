"""参数优化与币种分层建议"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from typing import Dict, List, Optional

from core.config import Config
from core.database import Database
from analytics.backtest import StrategyBacktester, SignalQualityAnalyzer


class ParameterOptimizer:
    def __init__(self, config: Config, db: Optional[Database] = None):
        self.config = config
        self.db = db or Database(config.db_path)
        self._cache = None
        self._cache_at = None

    def run(self, use_cache: bool = True) -> Dict:
        now = datetime.now()
        if use_cache and self._cache is not None and self._cache_at and (now - self._cache_at).total_seconds() < 300:
            return self._cache

        experiments = self._build_experiments()
        rows = []
        for exp in experiments:
            cfg = Config(self.config.config_path)
            cfg._config = self._deep_merge(deepcopy(self.config.all), exp['patch'])
            backtester = StrategyBacktester(cfg)
            result = backtester.run_all(cfg.symbols, use_cache=False)
            summary = result['summary']
            score = self._score(summary)
            rows.append({
                'name': exp['name'],
                'description': exp['description'],
                'score': round(score, 4),
                'summary': summary,
                'patch': exp['patch'],
            })

        rows.sort(key=lambda x: x['score'], reverse=True)
        best = rows[0] if rows else None
        symbol_advice = self._build_symbol_advice(best)
        strategy_advice = self._build_strategy_advice(best)

        result = {
            'best_experiment': best,
            'experiments': rows,
            'symbol_advice': symbol_advice,
            'strategy_advice': strategy_advice,
        }
        self._cache = result
        self._cache_at = now
        return result

    def _deep_merge(self, base: Dict, patch: Dict) -> Dict:
        out = deepcopy(base)
        for k, v in patch.items():
            if isinstance(v, dict) and isinstance(out.get(k), dict):
                out[k] = self._deep_merge(out[k], v)
            else:
                out[k] = v
        return out

    def _build_experiments(self) -> List[Dict]:
        return [
            {
                'name': 'baseline',
                'description': '当前基线参数',
                'patch': {},
            },
            {
                'name': 'strict_quality',
                'description': '提高最小强度与策略确认数，减少噪音交易',
                'patch': {
                    'strategies': {'composite': {'min_strength': 28, 'min_strategy_count': 2}},
                    'market_filters': {'min_volatility': 0.0045},
                },
            },
            {
                'name': 'tighter_risk',
                'description': '更紧止损 + 更快止盈',
                'patch': {
                    'trading': {'stop_loss': 0.015, 'take_profit': 0.03, 'trailing_stop': 0.012},
                },
            },
            {
                'name': 'trend_bias',
                'description': '偏趋势跟随，弱化均值回归噪音',
                'patch': {
                    'strategies': {
                        'rsi': {'strength_weight': 28},
                        'bollinger': {'strength_weight': 10},
                        'macd': {'strength_weight': 26},
                        'ma_cross': {'strength_weight': 30},
                        'volume': {'strength_weight': 30},
                    },
                    'market_filters': {'block_counter_trend': True, 'min_volatility': 0.0035},
                },
            },
            {
                'name': 'xrp_btc_focus',
                'description': '维持全局参数但预设后续偏向 BTC/XRP 保留',
                'patch': {
                    'strategies': {'composite': {'min_strength': 24, 'min_strategy_count': 1}},
                    'trading': {'take_profit': 0.035},
                },
            },
        ]

    def _score(self, summary: Dict) -> float:
        total_return = float(summary.get('total_return_pct', 0) or 0)
        win_rate = float(summary.get('win_rate', 0) or 0)
        max_drawdown = abs(float(summary.get('max_drawdown_pct', 0) or 0))
        total_trades = float(summary.get('total_trades', 0) or 0)
        trade_factor = min(total_trades / 20.0, 1.0)
        return total_return * 1.8 + win_rate * 0.35 - max_drawdown * 1.5 + trade_factor * 5

    def _build_symbol_advice(self, best_experiment: Optional[Dict]) -> List[Dict]:
        quality = SignalQualityAnalyzer(self.config, self.db).analyze(use_cache=False)
        backtest = StrategyBacktester(self.config).run_all(use_cache=False)
        quality_map = {x['symbol']: x for x in quality.get('by_symbol', [])}
        backtest_map = {x['symbol']: x for x in backtest.get('symbols', [])}
        symbols = sorted(set(list(quality_map.keys()) + list(backtest_map.keys())))
        advice = []
        for symbol in symbols:
            q = quality_map.get(symbol, {})
            b = backtest_map.get(symbol, {})
            quality_score = float(q.get('avg_quality_pct', -999))
            backtest_return = float(b.get('total_return_pct', -999))
            win_rate = float(b.get('win_rate', 0))
            if backtest_return > -5 and quality_score > -0.05:
                tier = 'keep'
                action = '建议保留，继续重点观察'
            elif backtest_return > -10 and quality_score > -0.10:
                tier = 'watch'
                action = '建议观察，先降权而不是删除'
            else:
                tier = 'pause'
                action = '建议暂停或大幅降权'
            advice.append({
                'symbol': symbol,
                'tier': tier,
                'action': action,
                'backtest_return_pct': round(backtest_return, 4) if backtest_return > -900 else None,
                'win_rate': round(win_rate, 2),
                'avg_quality_pct': round(quality_score, 4) if quality_score > -900 else None,
                'best_experiment': best_experiment['name'] if best_experiment else None,
            })
        tier_rank = {'keep': 0, 'watch': 1, 'pause': 2}
        advice.sort(key=lambda x: (tier_rank.get(x['tier'], 9), -(x.get('backtest_return_pct') or -999)))
        return advice

    def _build_strategy_advice(self, best_experiment: Optional[Dict]) -> List[Dict]:
        name = best_experiment['name'] if best_experiment else 'baseline'
        if name == 'strict_quality':
            return [
                {'topic': 'min_strength', 'advice': '建议提高到 28，减少噪音信号'},
                {'topic': 'min_strategy_count', 'advice': '建议提高到 2，避免单一策略误触发'},
            ]
        if name == 'tighter_risk':
            return [
                {'topic': 'stop_loss', 'advice': '建议收紧至 1.5%'},
                {'topic': 'take_profit', 'advice': '建议下调至 3%，提高兑现效率'},
            ]
        if name == 'trend_bias':
            return [
                {'topic': 'trend_following', 'advice': '增强 MACD / MA_Cross / Volume 权重'},
                {'topic': 'mean_reversion', 'advice': '降低 RSI / Bollinger 权重，减少逆势反转单'},
            ]
        return [
            {'topic': 'baseline', 'advice': '当前基线未见明显优于其他实验，建议继续分币种优化'},
        ]
