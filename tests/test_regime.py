"""Regime Layer v1 / adaptive regime M0 测试"""
import os
import sys
import tempfile
import unittest
from datetime import datetime

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import Config
from core.regime import (
    REGIME_DETECTOR_VERSION,
    Regime,
    RegimeDetector,
    RegimeResult,
    build_regime_snapshot,
    detect_regime,
    normalize_regime_snapshot,
)
from core.regime_policy import ADAPTIVE_POLICY_VERSION, resolve_regime_policy, build_validation_baseline_snapshot, build_validation_effective_snapshot, build_risk_effective_snapshot


def generate_test_data(regime_type: str, n: int = 100) -> pd.DataFrame:
    np.random.seed(42)
    dates = pd.date_range(end=datetime.now(), periods=n, freq='1h')

    if regime_type == 'trend_up':
        base = 1000
        trend = np.linspace(0, 200, n)
        noise = np.random.normal(0, 10, n)
        close = base + trend + noise
    elif regime_type == 'trend_down':
        base = 1200
        trend = np.linspace(0, -200, n)
        noise = np.random.normal(0, 10, n)
        close = base + trend + noise
    elif regime_type == 'range':
        base = 1000
        cycle = np.sin(np.linspace(0, 10 * np.pi, n)) * 50
        noise = np.random.normal(0, 15, n)
        close = base + cycle + noise
    elif regime_type == 'high_vol':
        base = 1000
        trend = np.linspace(0, 50, n)
        noise = np.random.normal(0, 80, n)
        close = base + trend + noise
    elif regime_type == 'low_vol':
        base = 1000
        trend = np.sin(np.linspace(0, 2 * np.pi, n)) * 20
        noise = np.random.normal(0, 3, n)
        close = base + trend + noise
    else:
        close = np.random.uniform(900, 1100, n) + np.random.normal(0, 5, n)

    data = {
        'close': close,
        'open': close * (1 + np.random.uniform(-0.01, 0.01, n)),
        'high': close * (1 + np.abs(np.random.uniform(0, 0.02, n))),
        'low': close * (1 - np.abs(np.random.uniform(0, 0.02, n))),
        'volume': np.random.uniform(1000, 5000, n),
    }
    return pd.DataFrame(data, index=dates)


class TestRegimeDetector(unittest.TestCase):
    def setUp(self):
        self.detector = RegimeDetector()

    def test_trend_up(self):
        result = self.detector.detect(generate_test_data('trend_up', 100))
        self.assertIn(result.regime, [Regime.TREND, Regime.HIGH_VOL])

    def test_trend_down(self):
        result = self.detector.detect(generate_test_data('trend_down', 100))
        self.assertIn(result.regime, [Regime.TREND, Regime.HIGH_VOL])

    def test_range(self):
        result = self.detector.detect(generate_test_data('range', 100))
        self.assertIn(result.regime, [Regime.RANGE, Regime.LOW_VOL, Regime.TREND])

    def test_high_vol(self):
        result = self.detector.detect(generate_test_data('high_vol', 100))
        self.assertIn(result.regime, [Regime.HIGH_VOL, Regime.RISK_ANOMALY])

    def test_low_vol(self):
        result = self.detector.detect(generate_test_data('low_vol', 100))
        self.assertIn(result.regime, [Regime.LOW_VOL, Regime.RANGE])

    def test_insufficient_data(self):
        result = self.detector.detect(generate_test_data('trend_up', 10))
        self.assertEqual(result.regime, Regime.UNKNOWN)

    def test_empty_data(self):
        result = self.detector.detect(pd.DataFrame())
        self.assertEqual(result.regime, Regime.UNKNOWN)

    def test_convenience_function(self):
        result = detect_regime(generate_test_data('trend_up', 100))
        self.assertIsInstance(result.regime, Regime)
        self.assertIsInstance(result.confidence, float)


class TestRegimeSnapshotSchema(unittest.TestCase):
    def test_result_to_dict_contains_legacy_and_new_snapshot_fields(self):
        result = RegimeResult(
            regime=Regime.TREND,
            confidence=0.78,
            indicators={'ema_gap': 0.023456, 'volatility': 0.01234, 'ema_direction': 1},
            details='趋势上涨',
        )
        snapshot = result.to_dict()
        self.assertEqual(snapshot['regime'], 'trend')
        self.assertEqual(snapshot['name'], 'trend')
        self.assertEqual(snapshot['family'], 'trend')
        self.assertEqual(snapshot['direction'], 'up')
        self.assertIn('stability_score', snapshot)
        self.assertIn('transition_risk', snapshot)
        self.assertIn('features', snapshot)
        self.assertEqual(snapshot['features']['ema_gap'], 0.02346)
        self.assertEqual(snapshot['detector_version'], REGIME_DETECTOR_VERSION)
        self.assertTrue(snapshot['detected_at'])

    def test_unknown_snapshot_normalization_is_backward_compatible(self):
        legacy = {'regime': 'unknown', 'confidence': 0.0, 'indicators': {}, 'details': 'fallback'}
        snapshot = normalize_regime_snapshot(legacy)
        self.assertEqual(snapshot['name'], 'unknown')
        self.assertEqual(snapshot['family'], 'unknown')
        self.assertEqual(snapshot['direction'], 'unknown')
        self.assertEqual(snapshot['detector_version'], REGIME_DETECTOR_VERSION)

    def test_build_regime_snapshot_accepts_legacy_regime_string(self):
        snapshot = build_regime_snapshot(
            regime='high_vol',
            confidence=0.7,
            indicators={'ema_direction': -1, 'volatility': 0.05},
            details='legacy string input',
        )
        self.assertEqual(snapshot['regime'], 'high_vol')
        self.assertEqual(snapshot['family'], 'vol')
        self.assertEqual(snapshot['direction'], 'down')


class TestValidationEffectiveSnapshot(unittest.TestCase):
    def test_validation_snapshot_keeps_baseline_when_no_override(self):
        cfg = Config()
        baseline = build_validation_baseline_snapshot(cfg, 'BTC/USDT')
        snapshot = build_validation_effective_snapshot(cfg, 'BTC/USDT')
        self.assertEqual(snapshot['baseline'], baseline)
        self.assertEqual(snapshot['effective'], baseline)
        self.assertEqual(snapshot['effective_state'], 'hints_only')
        self.assertEqual(snapshot['applied_overrides'], {})

    def test_validation_snapshot_applies_only_conservative_numeric_tightening(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'regimes': {
                'high_vol': {
                    'validation_overrides': {
                        'min_strength': 99,
                        'min_strategy_count': 3,
                    }
                }
            }
        }
        snapshot = build_validation_effective_snapshot(
            cfg,
            'BTC/USDT',
            regime_snapshot=build_regime_snapshot('high_vol', 0.8, {'volatility': 0.06}, '高波动')
        )
        self.assertEqual(snapshot['effective']['min_strength'], max(snapshot['baseline']['min_strength'], 99))
        self.assertEqual(snapshot['effective']['min_strategy_count'], max(snapshot['baseline']['min_strategy_count'], 3))
        self.assertIn('min_strength', snapshot['applied_overrides'])
        self.assertIn('min_strategy_count', snapshot['applied_overrides'])

    def test_validation_snapshot_records_ignored_non_conservative_overrides_reason(self):
        cfg = Config()
        cfg._config['market_filters'] = {'block_counter_trend': True}
        cfg._config['strategies'] = {'composite': {'min_strength': 28, 'min_strategy_count': 2}}
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'regimes': {
                'trend': {
                    'validation_overrides': {
                        'min_strength': 20,
                        'block_counter_trend': False,
                    }
                }
            }
        }
        snapshot = build_validation_effective_snapshot(
            cfg,
            'BTC/USDT',
            regime_snapshot=build_regime_snapshot('trend', 0.8, {'ema_gap': 0.03}, '趋势')
        )
        ignored = snapshot['ignored_overrides']
        self.assertTrue(any(item['key'] == 'min_strength' and item['reason'] == 'non_conservative_override' for item in ignored))
        self.assertTrue(any(item['key'] == 'block_counter_trend' and item['reason'] == 'non_conservative_override' for item in ignored))

    def test_validation_snapshot_includes_regime_and_policy_metadata(self):
        cfg = Config()
        snapshot = build_validation_effective_snapshot(
            cfg,
            'BTC/USDT',
            regime_snapshot=build_regime_snapshot('risk_anomaly', 0.91, {'volatility': 0.12}, '异常'),
        )
        self.assertEqual(snapshot['regime_name'], 'risk_anomaly')
        self.assertEqual(snapshot['policy_mode'], 'observe_only')
        self.assertIn('policy_version', snapshot)
        self.assertIn('stability_score', snapshot)
        self.assertIn('transition_risk', snapshot)


class TestRiskEffectiveSnapshotStep4(unittest.TestCase):
    def test_risk_snapshot_default_keeps_enforcement_disabled(self):
        cfg = Config()
        snapshot = build_risk_effective_snapshot(cfg, 'BTC/USDT')
        self.assertEqual(snapshot['effective_state'], 'disabled')
        self.assertTrue(snapshot['observe_only'])
        self.assertEqual(snapshot['baseline'], snapshot['effective'])
        self.assertEqual(snapshot['enforced_fields'], [])

    def test_risk_snapshot_enforces_only_conservative_fields_when_enabled(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'risk_hints_enabled': True,
                'risk_enforcement_enabled': True,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'risk_overrides': {
                        'base_entry_margin_ratio': 0.05,
                        'symbol_margin_cap_ratio': 0.10,
                        'leverage_cap': 5,
                    }
                }
            }
        }
        snapshot = build_risk_effective_snapshot(
            cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        )
        self.assertEqual(snapshot['effective_state'], 'effective')
        self.assertFalse(snapshot['observe_only'])
        self.assertAlmostEqual(snapshot['effective']['base_entry_margin_ratio'], 0.05, places=6)
        self.assertEqual(snapshot['effective']['leverage_cap'], 5)
        self.assertIn('base_entry_margin_ratio', snapshot['enforced_fields'])
        self.assertTrue(any(row['field'] == 'base_entry_margin_ratio' and row['enforced'] for row in snapshot['field_decisions']))

    def test_risk_snapshot_rollout_miss_keeps_hints_only(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'risk_hints_enabled': True,
                'risk_enforcement_enabled': True,
                'rollout_symbols': ['ETH/USDT'],
            },
            'regimes': {
                'high_vol': {'risk_overrides': {'base_entry_margin_ratio': 0.05}}
            }
        }
        snapshot = build_risk_effective_snapshot(
            cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        )
        self.assertEqual(snapshot['effective_state'], 'hints_only')
        self.assertTrue(snapshot['observe_only'])
        self.assertFalse(snapshot['rollout_match'])
        self.assertTrue(any(row['reason'] == 'rollout_symbol_not_matched' for row in snapshot['ignored_overrides']))


class TestAdaptiveRegimeConfigAndPolicy(unittest.TestCase):
    def test_adaptive_regime_defaults_are_safe(self):
        cfg = Config()
        adaptive = cfg.get_adaptive_regime_config()
        self.assertFalse(adaptive['enabled'])
        self.assertEqual(adaptive['mode'], 'observe_only')
        self.assertEqual(adaptive['detector']['version'], 'regime_v1_m0')
        self.assertEqual(cfg.get_adaptive_regime_mode(), 'observe_only')
        self.assertFalse(cfg.is_adaptive_regime_enabled())

    def test_symbol_override_can_switch_mode_without_affecting_default(self):
        cfg = Config()
        cfg._config['symbol_overrides'] = {
            'BTC/USDT': {
                'adaptive_regime': {
                    'enabled': True,
                    'mode': 'disabled',
                    'defaults': {'policy_version': 'adaptive_policy_symbol'},
                }
            }
        }
        btc = cfg.get_adaptive_regime_config('BTC/USDT')
        eth = cfg.get_adaptive_regime_config('ETH/USDT')
        self.assertEqual(btc['mode'], 'disabled')
        self.assertEqual(btc['defaults']['policy_version'], 'adaptive_policy_symbol')
        self.assertEqual(eth['mode'], 'observe_only')
        self.assertFalse(cfg.is_adaptive_regime_enabled('BTC/USDT'))

    def test_invalid_adaptive_regime_mode_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = os.path.join(tmpdir, 'config.yaml')
            with open(cfg_path, 'w', encoding='utf-8') as f:
                f.write(
                    "adaptive_regime:\n"
                    "  enabled: true\n"
                    "  mode: turbo\n"
                )
            with self.assertRaises(ValueError):
                Config(cfg_path)

    def test_resolve_regime_policy_returns_neutral_observe_only_snapshot(self):
        cfg = Config()
        regime_snapshot = build_regime_snapshot(
            regime='trend',
            confidence=0.81,
            indicators={'ema_gap': 0.03, 'ema_direction': 1, 'volatility': 0.015},
            details='趋势上涨',
        )
        policy = resolve_regime_policy(cfg, 'BTC/USDT', regime_snapshot)
        self.assertEqual(policy['mode'], 'observe_only')
        self.assertEqual(policy['policy_version'], ADAPTIVE_POLICY_VERSION)
        self.assertEqual(policy['regime_name'], 'trend')
        self.assertFalse(policy['is_effective'])
        self.assertEqual(policy['decision_overrides'], {})
        self.assertEqual(policy['execution_overrides'], {})
        self.assertEqual(policy['phase'], 'observe_only')
        self.assertEqual(policy['state'], 'neutral')
        self.assertIn('policy_mode:observe_only', policy['tags'])
        self.assertIn('m1-observe-only', policy['notes'])

    def test_resolve_regime_policy_exposes_decision_only_overrides_without_touching_execution(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'decision_only',
            'defaults': {
                'policy_version': 'adaptive_policy_v1_m2',
                'decision_overrides': {
                    'allow_score_min': 79,
                    'downgrade_allow_to_watch': True,
                },
            },
            'regimes': {
                'trend': {
                    'decision_overrides': {
                        'allow_score_min': 82,
                    }
                }
            }
        }
        regime_snapshot = build_regime_snapshot(
            regime='trend',
            confidence=0.81,
            indicators={'ema_gap': 0.03, 'ema_direction': 1, 'volatility': 0.015},
            details='趋势上涨',
        )
        policy = resolve_regime_policy(cfg, 'BTC/USDT', regime_snapshot)
        self.assertEqual(policy['mode'], 'decision_only')
        self.assertTrue(policy['is_effective'])
        self.assertEqual(policy['decision_overrides']['allow_score_min'], 82)
        self.assertTrue(policy['decision_overrides']['downgrade_allow_to_watch'])
        self.assertEqual(policy['effective_overrides']['decision']['allow_score_min'], 82)
        self.assertEqual(policy['execution_overrides'], {})


if __name__ == '__main__':
    unittest.main()
