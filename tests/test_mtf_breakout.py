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


if __name__ == '__main__':
    unittest.main()
