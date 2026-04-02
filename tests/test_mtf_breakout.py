import unittest
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from signals import Signal, SignalDetector, EntryDecider


def _make_ohlcv(closes, *, freq='1h', volume_base=1000, last_volume=None):
    closes = list(closes)
    dates = pd.date_range('2024-01-01', periods=len(closes), freq=freq)
    volumes = [volume_base] * len(closes)
    if last_volume is not None:
        volumes[-1] = last_volume
    rows = []
    for i, close in enumerate(closes):
        open_price = closes[i - 1] if i > 0 else close * 0.995
        high = max(open_price, close) * 1.002
        low = min(open_price, close) * 0.998
        rows.append([dates[i], open_price, high, low, close, volumes[i]])
    return pd.DataFrame(rows, columns=[0, 1, 2, 3, 4, 5])


class TestMtfBreakout(unittest.TestCase):
    def _make_entry_signal(self, *, symbol='BTC/USDT', strength=72, trend='bullish', reasons=None, strategies=None, direction_score=None, mtf_breakout=None):
        market_context = {
            'trend': trend,
            'volatility': 0.012,
            'atr_ratio': 0.012,
            'volatility_too_low': False,
            'volatility_too_high': False,
        }
        if mtf_breakout is not None:
            market_context['mtf_breakout'] = mtf_breakout
        return Signal(
            symbol=symbol,
            signal_type='buy',
            price=50000,
            strength=strength,
            strategies_triggered=strategies or ['MACD', 'Volume'],
            reasons=reasons or [
                {'strategy': 'MACD', 'action': 'buy', 'strength': 30, 'confidence': 0.8},
                {'strategy': 'Volume', 'action': 'buy', 'strength': 20, 'confidence': 0.7},
            ],
            direction_score=direction_score or {'buy': 38.0, 'sell': 0.0, 'net': 38.0},
            market_context=market_context,
            regime_info={'regime': 'trend', 'confidence': 0.7},
        )

    def setUp(self):
        self.config = {
            'strategies': {
                'rsi': {'enabled': False},
                'macd': {'enabled': False},
                'ma_cross': {'enabled': False},
                'bollinger': {'enabled': False},
                'volume': {'enabled': False},
                'pattern': {'enabled': False},
                'composite': {'min_strength': 1, 'min_strategy_count': 1},
            },
            'mtf_breakout': {
                'enabled': True,
                'observe_only': True,
                'trigger_timeframe': '1h',
                'anchor_timeframe': '4h',
                'breakout_lookback_bars': 20,
                'min_breakout_pct': 0.001,
                'min_volume_ratio': 1.1,
                'require_anchor_trend_alignment': True,
                'min_score': 60,
            },
        }
        self.detector = SignalDetector(self.config)

    def test_detector_generates_structured_mtf_breakout_evidence(self):
        closes_1h = [100 + i * 0.2 for i in range(79)] + [118.0]
        closes_4h = [100 + i * 0.6 for i in range(80)]
        df_1h = _make_ohlcv(closes_1h, freq='1h', volume_base=1000, last_volume=1800)
        df_4h = _make_ohlcv(closes_4h, freq='4h', volume_base=1200, last_volume=1500)

        signal = self.detector.analyze('BTC/USDT', df_1h, closes_1h[-1], None, mtf_frames={'1h': df_1h, '4h': df_4h})

        evidence = signal.market_context['mtf_breakout']
        self.assertTrue(evidence['has_breakout'])
        self.assertEqual(evidence['direction'], 'buy')
        self.assertGreaterEqual(evidence['score'], 60)
        self.assertTrue(evidence['observe_only'])
        self.assertIn('1h 向上突破', evidence['reason'])
        self.assertEqual(signal.market_context['mtf_breakout_score'], evidence['score'])
        self.assertEqual(signal.market_context['mtf_breakout_reason'], evidence['reason'])

    def test_detector_market_context_contains_trigger_anchor_structure(self):
        closes_1h = [100 + i * 0.15 for i in range(79)] + [116.5]
        closes_4h = [100 + i * 0.5 for i in range(80)]
        df_1h = _make_ohlcv(closes_1h, freq='1h', volume_base=900, last_volume=1600)
        df_4h = _make_ohlcv(closes_4h, freq='4h', volume_base=1000, last_volume=1200)

        signal = self.detector.analyze('BTC/USDT', df_1h, closes_1h[-1], None, mtf_frames={'1h': df_1h, '4h': df_4h})
        evidence = signal.market_context['mtf_breakout']

        self.assertEqual(evidence['timeframes']['trigger'], '1h')
        self.assertEqual(evidence['timeframes']['anchor'], '4h')
        self.assertIn('trigger', evidence)
        self.assertIn('anchor', evidence)
        self.assertTrue(evidence['trigger']['available'])
        self.assertTrue(evidence['anchor']['available'])
        self.assertIn('volume_ratio', evidence['trigger'])
        self.assertIn(evidence['anchor']['trend'], {'bullish', 'bearish', 'sideways'})

    def test_entry_decider_breakdown_contains_mtf_breakout_fields(self):
        decider = EntryDecider({})
        signal = Signal(
            symbol='BTC/USDT',
            signal_type='buy',
            price=50000,
            strength=72,
            strategies_triggered=['MACD', 'Volume'],
            reasons=[
                {'strategy': 'MACD', 'action': 'buy', 'strength': 30, 'confidence': 0.8},
                {'strategy': 'Volume', 'action': 'buy', 'strength': 20, 'confidence': 0.7},
            ],
            direction_score={'buy': 38.0, 'sell': 0.0, 'net': 38.0},
            market_context={
                'trend': 'bullish',
                'volatility': 0.012,
                'atr_ratio': 0.012,
                'volatility_too_low': False,
                'volatility_too_high': False,
                'mtf_breakout': {
                    'score': 78,
                    'reason': '1h 向上突破前20根高点；4h anchor bullish 对齐',
                    'observe_only': True,
                },
            },
            regime_info={'regime': 'trend', 'confidence': 0.7},
        )

        result = decider.decide(signal)
        payload = result.to_dict()
        self.assertEqual(payload['breakdown']['mtf_breakout_score'], 78)
        self.assertIn('4h anchor bullish 对齐', payload['breakdown']['mtf_breakout_reason'])
        self.assertTrue(payload['breakdown']['mtf_breakout_observe_only'])

    def test_observe_only_mtf_breakout_does_not_change_baseline_decision(self):
        baseline_signal = Signal(
            symbol='BTC/USDT',
            signal_type='buy',
            price=50000,
            strength=72,
            strategies_triggered=['MACD', 'Volume'],
            reasons=[
                {'strategy': 'MACD', 'action': 'buy', 'strength': 30, 'confidence': 0.8},
                {'strategy': 'Volume', 'action': 'buy', 'strength': 20, 'confidence': 0.7},
            ],
            direction_score={'buy': 38.0, 'sell': 0.0, 'net': 38.0},
            market_context={'trend': 'bullish', 'volatility': 0.012, 'atr_ratio': 0.012, 'volatility_too_low': False, 'volatility_too_high': False},
            regime_info={'regime': 'trend', 'confidence': 0.7},
        )
        observe_signal = Signal(
            **{
                **baseline_signal.to_dict(),
                'market_context': {
                    **baseline_signal.market_context,
                    'mtf_breakout': {
                        'score': 88,
                        'direction': 'buy',
                        'has_breakout': True,
                        'eligible': True,
                        'reason': '1h 向上突破前20根高点；4h anchor bullish 对齐；observe-only',
                        'observe_only': True,
                    },
                },
            }
        )
        baseline = EntryDecider({}).decide(baseline_signal)
        observe = EntryDecider({}).decide(observe_signal)

        self.assertEqual(observe.decision, baseline.decision)
        self.assertEqual(observe.score, baseline.score)
        self.assertEqual(observe.breakdown.mtf_breakout_score, 88)
        self.assertTrue(observe.breakdown.mtf_breakout_observe_only)
        self.assertEqual(observe.breakdown.baseline_score, baseline.score)
        self.assertGreater(observe.breakdown.candidate_adjustment, 0)
        self.assertEqual(observe.breakdown.candidate_score_after_mtf, baseline.score + observe.breakdown.candidate_adjustment)
        self.assertFalse(observe.breakdown.mtf_breakout_effective)
        self.assertEqual(observe.breakdown.mtf_breakout_mode, 'observe_only')
        self.assertEqual(observe.breakdown.mtf_breakout_bias, 'aligned')
        self.assertIn(observe.breakdown.candidate_decision_after_mtf, {'allow', 'watch', 'block'})

        observability = EntryDecider({}).build_mtf_breakout_observability(observe_signal, observe)
        self.assertEqual(observability['baseline_score'], baseline.score)
        self.assertEqual(observability['candidate_adjustment'], observe.breakdown.candidate_adjustment)
        self.assertEqual(observability['candidate_score_after_mtf'], observe.breakdown.candidate_score_after_mtf)
        self.assertEqual(observability['candidate_decision_after_mtf'], observe.breakdown.candidate_decision_after_mtf)
        self.assertEqual(observability['mtf_breakout_mode'], 'observe_only')
        self.assertFalse(observability['mtf_breakout_effective'])
        self.assertEqual(observability['mtf_breakout_action'], 'candidate_boost')

    def test_decision_only_counter_breakout_can_downgrade_live_decision(self):
        signal = self._make_entry_signal(
            mtf_breakout={
                'enabled': True,
                'score': 88,
                'direction': 'sell',
                'has_breakout': True,
                'eligible': True,
                'reason': '1h 向下突破；与做多方向冲突',
                'observe_only': False,
            }
        )

        result = EntryDecider({'entry_decider': {'mtf_breakout_live_mode': 'decision_only'}}).decide(signal)

        self.assertEqual(result.breakdown.baseline_score, 74)
        self.assertEqual(result.breakdown.candidate_score_after_mtf, 65)
        self.assertEqual(result.breakdown.candidate_adjustment, -9)
        self.assertEqual(result.breakdown.candidate_decision_after_mtf, 'watch')
        self.assertEqual(result.score, 65)
        self.assertEqual(result.decision, 'watch')
        self.assertEqual(result.breakdown.mtf_breakout_mode, 'decision_only')
        self.assertTrue(result.breakdown.mtf_breakout_effective)
        self.assertEqual(result.breakdown.mtf_breakout_bias, 'counter')
        self.assertEqual(result.breakdown.mtf_breakout_action, 'candidate_penalty')

    def test_decision_only_positive_promote_is_disabled_by_default(self):
        signal = self._make_entry_signal(
            strength=75,
            trend='sideways',
            strategies=['RSI', 'Bollinger'],
            reasons=[
                {'strategy': 'RSI', 'action': 'buy', 'value': 38, 'strength': 20, 'confidence': 0.7},
                {'strategy': 'Bollinger', 'action': 'buy', 'strength': 18, 'confidence': 0.7},
            ],
            direction_score={'buy': 24.0, 'sell': 0.0, 'net': 24.0},
            mtf_breakout={
                'enabled': True,
                'score': 88,
                'direction': 'buy',
                'has_breakout': True,
                'eligible': True,
                'reason': '1h 向上突破；4h anchor bullish 对齐',
                'observe_only': False,
                'anchor': {'available': True, 'trend': 'bullish'},
                'trigger': {'available': True},
            }
        )

        result = EntryDecider({'entry_decider': {'mtf_breakout_live_mode': 'decision_only'}}).decide(signal)

        self.assertEqual(result.breakdown.baseline_score, 68)
        self.assertGreater(result.breakdown.candidate_score_after_mtf, result.breakdown.baseline_score)
        self.assertEqual(result.breakdown.candidate_decision_after_mtf, 'allow')
        self.assertEqual(result.score, result.breakdown.candidate_score_after_mtf)
        self.assertEqual(result.decision, 'watch')
        self.assertIn('MTF breakout 正向 promote 默认禁用', result.watch_reasons)
        self.assertEqual(result.breakdown.mtf_breakout_bias, 'aligned')
        self.assertEqual(result.breakdown.mtf_breakout_action, 'candidate_boost')

    def test_anchor_conflict_can_force_block_when_enabled(self):
        signal = self._make_entry_signal(
            mtf_breakout={
                'enabled': True,
                'score': 84,
                'direction': 'buy',
                'has_breakout': True,
                'eligible': False,
                'reason': '1h 向上突破，但 4h anchor bearish 冲突',
                'observe_only': False,
                'trigger': {'available': True},
                'anchor': {'available': True, 'trend': 'bearish'},
            }
        )
        decider = EntryDecider({
            'entry_decider': {
                'mtf_breakout_live_mode': 'decision_only',
                'mtf_breakout_anchor_conflict_action': 'block',
            }
        })

        result = decider.decide(signal)

        self.assertEqual(result.decision, 'block')
        self.assertEqual(result.breakdown.mtf_breakout_bias, 'anchor_conflict')
        self.assertEqual(result.breakdown.mtf_breakout_action, 'anchor_conflict_block')
        self.assertIn('MTF breakout anchor 冲突', result.watch_reasons)

    def test_symbol_override_can_disable_counter_penalty_while_global_mode_remains_live(self):
        signal_btc = self._make_entry_signal(
            symbol='BTC/USDT',
            mtf_breakout={
                'enabled': True,
                'score': 88,
                'direction': 'sell',
                'has_breakout': True,
                'eligible': True,
                'reason': '1h 向下突破；与做多方向冲突',
                'observe_only': False,
            }
        )
        signal_eth = self._make_entry_signal(
            symbol='ETH/USDT',
            mtf_breakout={
                'enabled': True,
                'score': 88,
                'direction': 'sell',
                'has_breakout': True,
                'eligible': True,
                'reason': '1h 向下突破；与做多方向冲突',
                'observe_only': False,
            }
        )
        decider = EntryDecider({
            'entry_decider': {
                'mtf_breakout_live_mode': 'decision_only',
                'mtf_breakout_allow_counter_penalty': True,
            },
            'symbol_overrides': {
                'BTC/USDT': {
                    'entry_decider': {
                        'mtf_breakout_allow_counter_penalty': False,
                        'mtf_breakout_live_mode': 'limited_execution',
                    }
                }
            }
        })

        btc_result = decider.decide(signal_btc)
        eth_result = decider.decide(signal_eth)

        self.assertEqual(btc_result.decision, 'allow')
        self.assertEqual(btc_result.score, btc_result.breakdown.baseline_score)
        self.assertEqual(btc_result.breakdown.candidate_adjustment, 0)
        self.assertEqual(btc_result.breakdown.mtf_breakout_action, 'counter_penalty_disabled')
        self.assertEqual(btc_result.breakdown.mtf_breakout_mode, 'limited_execution')

        self.assertEqual(eth_result.decision, 'watch')
        self.assertEqual(eth_result.score, eth_result.breakdown.candidate_score_after_mtf)
        self.assertLess(eth_result.breakdown.candidate_adjustment, 0)
        self.assertEqual(eth_result.breakdown.mtf_breakout_action, 'candidate_penalty')
        self.assertEqual(eth_result.breakdown.mtf_breakout_mode, 'decision_only')


if __name__ == '__main__':
    unittest.main()
