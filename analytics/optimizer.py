"""参数优化与币种分层建议"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import yaml

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
        focused_sets = self._run_focused_symbol_sets(best)
        symbol_specific = self._run_symbol_specific_experiments(best)
        promotions = self._evaluate_candidate_promotions(symbol_specific, symbol_advice, focused_sets)
        presets = self._write_presets(best, symbol_specific)

        self._record_candidate_reviews(promotions)

        result = {
            'best_experiment': best,
            'experiments': rows,
            'symbol_advice': symbol_advice,
            'strategy_advice': strategy_advice,
            'focused_sets': focused_sets,
            'symbol_specific': symbol_specific,
            'candidate_promotions': promotions,
            'presets': presets,
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

    def _run_focused_symbol_sets(self, best_experiment: Optional[Dict]) -> List[Dict]:
        patch = best_experiment['patch'] if best_experiment else {}
        sets = [
            {'name': 'btc_only', 'symbols': ['BTC/USDT'], 'description': '只跑 BTC'},
            {'name': 'xrp_only', 'symbols': ['XRP/USDT'], 'description': '只跑 XRP'},
            {'name': 'btc_xrp', 'symbols': ['BTC/USDT', 'XRP/USDT'], 'description': '跑 BTC + XRP'},
        ]
        rows = []
        for item in sets:
            cfg = Config(self.config.config_path)
            merged = self._deep_merge(deepcopy(self.config.all), patch)
            merged.setdefault('symbols', {})['watch_list'] = item['symbols']
            cfg._config = merged
            summary = StrategyBacktester(cfg).run_all(item['symbols'], use_cache=False)['summary']
            rows.append({
                'name': item['name'],
                'description': item['description'],
                'symbols': item['symbols'],
                'summary': summary,
                'score': round(self._score(summary), 4),
            })
        rows.sort(key=lambda x: x['score'], reverse=True)
        return rows

    def _run_symbol_specific_experiments(self, best_experiment: Optional[Dict]) -> Dict:
        base_patch = best_experiment['patch'] if best_experiment else {}
        suites = {
            'BTC/USDT': [
                {
                    'name': 'btc_safe',
                    'description': 'BTC稳健型',
                    'patch': {'trading': {'take_profit': 0.03, 'trailing_stop': 0.012}, 'strategies': {'composite': {'min_strength': 30, 'min_strategy_count': 2}}}
                },
                {
                    'name': 'btc_trend',
                    'description': 'BTC趋势跟随加强',
                    'patch': {'strategies': {'macd': {'strength_weight': 26}, 'ma_cross': {'strength_weight': 30}, 'rsi': {'strength_weight': 32}, 'composite': {'min_strength': 26, 'min_strategy_count': 2}}}
                },
            ],
            'XRP/USDT': [
                {
                    'name': 'xrp_candidate',
                    'description': 'XRP候选观察',
                    'patch': {'trading': {'take_profit': 0.035, 'trailing_stop': 0.014}, 'market_filters': {'min_volatility': 0.005}, 'strategies': {'composite': {'min_strength': 30, 'min_strategy_count': 2}}}
                },
                {
                    'name': 'xrp_fast',
                    'description': 'XRP高波动快进快出',
                    'patch': {'trading': {'stop_loss': 0.018, 'take_profit': 0.028, 'trailing_stop': 0.01}, 'strategies': {'volume': {'strength_weight': 32}, 'macd': {'strength_weight': 24}, 'composite': {'min_strength': 24, 'min_strategy_count': 1}}}
                },
            ],
        }
        result = {}
        for symbol, experiments in suites.items():
            rows = []
            for exp in experiments:
                cfg = Config(self.config.config_path)
                merged = self._deep_merge(deepcopy(self.config.all), base_patch)
                merged = self._deep_merge(merged, exp['patch'])
                merged.setdefault('symbols', {})['watch_list'] = [symbol]
                cfg._config = merged
                summary = StrategyBacktester(cfg).run_all([symbol], use_cache=False)['summary']
                rows.append({
                    'symbol': symbol,
                    'name': exp['name'],
                    'description': exp['description'],
                    'patch': exp['patch'],
                    'summary': summary,
                    'score': round(self._score(summary), 4),
                })
            rows.sort(key=lambda x: x['score'], reverse=True)
            result[symbol] = rows
        return result

    def _write_presets(self, best_experiment: Optional[Dict], symbol_specific: Dict) -> List[Dict]:
        presets_dir = Path(self.config.config_path).parent / 'presets'
        presets_dir.mkdir(parents=True, exist_ok=True)
        written = []

        presets = {
            'btc-focused.yaml': self._build_preset_config(best_experiment, ['BTC/USDT'], ['XRP/USDT'], ['ETH/USDT', 'SOL/USDT', 'HYPE/USDT']),
            'xrp-candidate.yaml': self._build_preset_config(best_experiment, ['XRP/USDT'], ['BTC/USDT'], ['ETH/USDT', 'SOL/USDT', 'HYPE/USDT']),
            'safe-mode.yaml': self._build_preset_config(best_experiment, ['BTC/USDT'], [], ['XRP/USDT', 'ETH/USDT', 'SOL/USDT', 'HYPE/USDT'], extra_patch={'trading': {'position_size': 0.08}, 'strategies': {'composite': {'min_strength': 30, 'min_strategy_count': 2}}}),
        }

        # BTC focused 保持当前最佳主运行配置，不叠加更差专项 patch
        if symbol_specific.get('XRP/USDT'):
            presets['xrp-candidate.yaml'] = self._deep_merge(presets['xrp-candidate.yaml'], symbol_specific['XRP/USDT'][0]['patch'])

        for filename, data in presets.items():
            path = presets_dir / filename
            with open(path, 'w', encoding='utf-8') as f:
                yaml.dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
            written.append({'name': filename.replace('.yaml', ''), 'path': str(path)})
        return written

    def _build_preset_config(self, best_experiment: Optional[Dict], watch_list: List[str], candidate: List[str], paused: List[str], extra_patch: Optional[Dict] = None) -> Dict:
        base = deepcopy(self.config.all)
        if best_experiment:
            base = self._deep_merge(base, best_experiment['patch'])
        if extra_patch:
            base = self._deep_merge(base, extra_patch)
        base.setdefault('symbols', {})['selection_mode'] = 'focused'
        base['symbols']['watch_list'] = watch_list
        base['symbols']['candidate_watch_list'] = candidate
        base['symbols']['paused_watch_list'] = paused
        if 'api' in base:
            base['api']['key'] = 'your_api_key'
            base['api']['secret'] = 'your_api_secret'
            base['api']['passphrase'] = 'your_passphrase'
        return base

    def _record_candidate_reviews(self, promotions: List[Dict]):
        for row in promotions:
            self.db.record_candidate_review(
                symbol=row['symbol'],
                decision=row['decision'],
                best_variant=row.get('best_variant'),
                score=row.get('score'),
                reason=row.get('reason'),
                details={'focused_score': row.get('focused_score'), 'summary': row.get('summary')}
            )

    def _evaluate_candidate_promotions(self, symbol_specific: Dict, symbol_advice: List[Dict], focused_sets: List[Dict]) -> List[Dict]:
        current_watch = set(self.config.symbols)
        advice_map = {row['symbol']: row for row in symbol_advice}
        focused_map = {row['name']: row for row in focused_sets}
        results = []
        for symbol, rows in symbol_specific.items():
            if symbol in current_watch:
                continue
            best_row = rows[0] if rows else None
            advice = advice_map.get(symbol, {})
            focused_score = None
            if symbol == 'XRP/USDT' and focused_map.get('xrp_only'):
                focused_score = focused_map['xrp_only']['score']
            decision = 'reject'
            reason = '当前专项结果不足以升级'
            if best_row:
                ret = float(best_row['summary'].get('total_return_pct', -999) or -999)
                dd = abs(float(best_row['summary'].get('max_drawdown_pct', 0) or 0))
                win = float(best_row['summary'].get('win_rate', 0) or 0)
                quality = float(advice.get('avg_quality_pct', -999) or -999)
                if ret > -2.5 and win >= 45 and dd <= 6 and quality >= -0.03:
                    decision = 'promote'
                    reason = '回测与质量均接近可接受，可升入主池试跑'
                elif ret > -5 and win >= 40 and quality >= -0.05:
                    decision = 'keep_candidate'
                    reason = '已有潜力，但仍需继续观察'
            results.append({
                'symbol': symbol,
                'best_variant': best_row['name'] if best_row else None,
                'decision': decision,
                'reason': reason,
                'score': best_row['score'] if best_row else None,
                'focused_score': focused_score,
                'summary': best_row['summary'] if best_row else None,
            })
        return results

    def _build_strategy_advice(self, best_experiment: Optional[Dict]) -> List[Dict]:
        name = best_experiment['name'] if best_experiment else 'baseline'
        if name == 'strict_quality':
            return [
                {'topic': 'min_strength', 'advice': '建议提高到 28，减少噪音信号'},
                {'topic': 'min_strategy_count', 'advice': '建议提高到 2，避免单一策略误触发'},
                {'topic': 'symbol_focus', 'advice': '建议先收缩到 BTC 主池，XRP 保留候选观察'},
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
