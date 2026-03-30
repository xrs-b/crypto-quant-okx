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
from core.regime_policy import ADAPTIVE_POLICY_VERSION, resolve_regime_policy, build_validation_baseline_snapshot, build_validation_effective_snapshot, build_risk_effective_snapshot, build_execution_effective_snapshot


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


class TestExecutionEffectiveSnapshotStep1(unittest.TestCase):
    def test_execution_snapshot_default_keeps_hints_disabled(self):
        cfg = Config()
        snapshot = build_execution_effective_snapshot(cfg, 'BTC/USDT')
        self.assertEqual(snapshot['effective_state'], 'disabled')
        self.assertEqual(snapshot['baseline'], snapshot['effective'])
        self.assertTrue(snapshot['observe_only'])

    def test_execution_snapshot_applies_only_conservative_execution_hints(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': False,
                'layering_profile_enforcement_enabled': False,
                'enforce_conservative_only': True,
            },
            'regimes': {
                'high_vol': {
                    'execution_overrides': {
                        'layer_ratios': [0.05, 0.05, 0.03],
                        'layer_max_total_ratio': 0.13,
                        'max_layers_per_signal': 1,
                        'min_add_interval_seconds': 600,
                        'profit_only_add': True,
                        'leverage_cap': 20,
                    }
                }
            }
        }
        snapshot = build_execution_effective_snapshot(
            cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        )
        self.assertEqual(snapshot['effective_state'], 'hints_only')
        self.assertEqual(snapshot['effective']['layer_ratios'], [0.05, 0.05, 0.03])
        self.assertEqual(snapshot['effective']['layer_max_total_ratio'], 0.13)
        self.assertEqual(snapshot['effective']['min_add_interval_seconds'], 600)
        self.assertTrue(snapshot['effective']['profit_only_add'])
        self.assertIn('layer_ratios', snapshot['applied_overrides'])
        self.assertTrue(any(item['key'] == 'leverage_cap' and item['reason'] == 'non_conservative_override' for item in snapshot['ignored_overrides']))
        self.assertIn('WOULD_REDUCE_LAYER_RATIOS', snapshot['hint_codes'])
        self.assertIn('IGNORED_NON_CONSERVATIVE_OVERRIDE', snapshot['hint_codes'])

    def test_execution_snapshot_rollout_miss_stays_hints_only_and_serializable(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'rollout_symbols': ['ETH/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'execution_overrides': {'min_add_interval_seconds': 600}
                }
            }
        }
        snapshot = build_execution_effective_snapshot(
            cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        )
        self.assertEqual(snapshot['effective_state'], 'hints_only')
        self.assertFalse(snapshot['rollout_match'])
        self.assertTrue(any(item['reason'] == 'rollout_symbol_not_matched' for item in snapshot['ignored_overrides']))
        import json
        json.dumps(snapshot, ensure_ascii=False)

    def test_execution_snapshot_step2_enforces_only_guardrail_fields_by_default(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'enforce_conservative_only': True,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'execution_overrides': {
                        'layer_ratios': [0.05, 0.05, 0.03],
                        'layer_max_total_ratio': 0.13,
                        'max_layers_per_signal': 1,
                        'min_add_interval_seconds': 600,
                        'profit_only_add': True,
                    }
                }
            }
        }
        snapshot = build_execution_effective_snapshot(
            cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        )
        self.assertEqual(snapshot['effective_state'], 'effective')
        self.assertEqual(snapshot['enforced_profile']['layer_max_total_ratio'], 0.13)
        self.assertEqual(snapshot['enforced_profile']['max_layers_per_signal'], 1)
        self.assertEqual(snapshot['enforced_profile']['min_add_interval_seconds'], 600)
        self.assertTrue(snapshot['enforced_profile']['profit_only_add'])
        self.assertEqual(snapshot['enforced_profile']['layer_ratios'], snapshot['baseline']['layer_ratios'])
        self.assertNotIn('layer_ratios', snapshot['enforced_fields'])
        self.assertTrue(snapshot['execution_profile_really_enforced'])

    def test_execution_snapshot_step3_default_safe_keeps_guarded_layering_disabled(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_hints_enabled': True,
                'layering_profile_enforcement_enabled': False,
                'layering_plan_shape_enforcement_enabled': False,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'execution_overrides': {
                        'layer_max_total_ratio': 0.13,
                        'min_add_interval_seconds': 600,
                        'profit_only_add': True,
                    }
                }
            }
        }
        snapshot = build_execution_effective_snapshot(
            cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        )
        self.assertEqual(snapshot['effective_state'], 'effective')
        self.assertFalse(snapshot['layering_profile_really_enforced'])
        self.assertFalse(snapshot['plan_shape_really_enforced'])
        self.assertEqual(snapshot['live'], snapshot['baseline'])
        self.assertIn('layer_max_total_ratio', snapshot['hinted_only_fields'])

    def test_execution_snapshot_step3_enforces_guardrail_fields_without_layer_ratios(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_hints_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': False,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'execution_overrides': {
                        'layer_ratios': [0.05, 0.05, 0.03],
                        'layer_max_total_ratio': 0.13,
                        'max_layers_per_signal': 1,
                        'min_add_interval_seconds': 600,
                        'profit_only_add': True,
                    }
                }
            }
        }
        snapshot = build_execution_effective_snapshot(
            cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        )
        self.assertTrue(snapshot['layering_profile_really_enforced'])
        self.assertFalse(snapshot['plan_shape_really_enforced'])
        self.assertEqual(snapshot['live']['layer_max_total_ratio'], 0.13)
        self.assertEqual(snapshot['live']['min_add_interval_seconds'], 600)
        self.assertTrue(snapshot['live']['profit_only_add'])
        self.assertEqual(snapshot['live']['layer_ratios'], snapshot['baseline']['layer_ratios'])
        self.assertIn('layer_ratios', snapshot['hinted_only_fields'])
        layer_ratio = next(item for item in snapshot['field_decisions'] if item['field'] == 'layer_ratios')
        self.assertEqual(layer_ratio['reason'], 'layering_plan_shape_enforcement_disabled')

    def test_execution_snapshot_step3_never_live_layer_ratios_without_shape_switch(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': False,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {'execution_overrides': {'layer_ratios': [0.05, 0.05, 0.03]}}
            }
        }
        snapshot = build_execution_effective_snapshot(
            cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        )
        self.assertEqual(snapshot['effective']['layer_ratios'], [0.05, 0.05, 0.03])
        self.assertEqual(snapshot['live']['layer_ratios'], snapshot['baseline']['layer_ratios'])
        self.assertFalse(snapshot['plan_shape_really_enforced'])

    def test_execution_snapshot_step2_never_loosens_baseline_and_exposes_live_field_decisions(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {
                'high_vol': {
                    'execution_overrides': {
                        'layer_max_total_ratio': 0.30,
                        'max_layers_per_signal': 9,
                        'min_add_interval_seconds': 0,
                        'profit_only_add': False,
                        'allow_same_bar_multiple_adds': True,
                    }
                }
            }
        }
        snapshot = build_execution_effective_snapshot(
            cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动')
        )
        self.assertEqual(snapshot['effective_state'], 'effective')
        self.assertFalse(snapshot['execution_profile_really_enforced'])
        self.assertEqual(snapshot['enforced_profile'], snapshot['baseline'])
        self.assertTrue(any(item['key'] == 'layer_max_total_ratio' and item['reason'] == 'non_conservative_override' for item in snapshot['ignored_overrides']))
        min_add = next(item for item in snapshot['field_decisions'] if item['field'] == 'min_add_interval_seconds')
        self.assertEqual(min_add['live'], snapshot['baseline']['min_add_interval_seconds'])
        self.assertEqual(min_add['decision'], 'unchanged')

    def test_layering_shape_snapshot_keeps_layer_ratios_hints_only_when_shape_disabled(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': False,
                'rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {'high_vol': {'execution_overrides': {'layer_ratios': [0.05, 0.05, 0.03], 'layer_max_total_ratio': 0.13, 'max_layers_per_signal': 1, 'min_add_interval_seconds': 600, 'profit_only_add': True}}}
        }
        snapshot = build_execution_effective_snapshot(cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动'))
        self.assertEqual(snapshot['effective']['layer_ratios'], [0.05, 0.05, 0.03])
        self.assertEqual(snapshot['live']['layer_ratios'], [0.06, 0.06, 0.04])
        self.assertFalse(snapshot['plan_shape_really_enforced'])
        self.assertEqual(snapshot['live_layer_shape_source'], 'baseline')
        self.assertEqual(snapshot['plan_shape_validation']['reason'], 'layering_plan_shape_enforcement_disabled')

    def test_layering_shape_snapshot_derives_layer_count_from_ratios(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': True,
                'layering_plan_shape_rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {'high_vol': {'execution_overrides': {'layer_ratios': [0.05, 0.05], 'layer_max_total_ratio': 0.13, 'max_layers_per_signal': 1, 'min_add_interval_seconds': 600, 'profit_only_add': True, 'layer_count': 9}}}
        }
        snapshot = build_execution_effective_snapshot(cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动'))
        self.assertEqual(snapshot['baseline']['layer_count'], 3)
        self.assertEqual(snapshot['effective']['layer_count'], 2)
        self.assertEqual(snapshot['live']['layer_count'], 2)
        self.assertTrue(any(item['key'] == 'layer_count' and item['reason'] == 'layer_count_derived_only' for item in snapshot['ignored_overrides']))

    def test_layering_shape_snapshot_rejects_non_conservative_layer_ratios(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': True,
                'layering_plan_shape_rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {'high_vol': {'execution_overrides': {'layer_ratios': [0.07, 0.05, 0.03], 'layer_max_total_ratio': 0.15, 'max_layers_per_signal': 1, 'min_add_interval_seconds': 600, 'profit_only_add': True}}}
        }
        snapshot = build_execution_effective_snapshot(cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动'))
        self.assertEqual(snapshot['live']['layer_ratios'], snapshot['baseline']['layer_ratios'])
        self.assertFalse(snapshot['plan_shape_really_enforced'])
        self.assertIn('layer_ratios', snapshot['plan_shape_ignored_fields'])

    def test_layering_shape_snapshot_rejects_expanding_layer_count(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': True,
                'layering_plan_shape_rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {'high_vol': {'execution_overrides': {'layer_ratios': [0.05, 0.05, 0.03, 0.01], 'layer_max_total_ratio': 0.14, 'max_layers_per_signal': 1, 'min_add_interval_seconds': 600, 'profit_only_add': True}}}
        }
        snapshot = build_execution_effective_snapshot(cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动'))
        self.assertEqual(snapshot['effective']['layer_count'], 3)
        self.assertEqual(snapshot['live']['layer_count'], 3)
        self.assertTrue(any(item['key'] == 'layer_ratios' and item['reason'] in {'layer_ratio_length_expands', 'non_conservative_override'} for item in snapshot['ignored_overrides']))

    def test_layering_shape_snapshot_respects_live_total_ratio_cap(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': True,
                'layering_plan_shape_rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {'high_vol': {'execution_overrides': {'layer_ratios': [0.05, 0.05, 0.03], 'layer_max_total_ratio': 0.12, 'max_layers_per_signal': 1, 'min_add_interval_seconds': 600, 'profit_only_add': True}}}
        }
        snapshot = build_execution_effective_snapshot(cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动'))
        self.assertFalse(snapshot['plan_shape_really_enforced'])
        self.assertEqual(snapshot['plan_shape_validation']['reason'], 'layer_ratio_exceeds_total_cap')
        self.assertEqual(snapshot['live']['layer_ratios'], snapshot['baseline']['layer_ratios'])

    def test_layering_shape_snapshot_keeps_baseline_live_on_guardrail_not_live(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': False,
                'layering_plan_shape_enforcement_enabled': True,
                'layering_plan_shape_rollout_symbols': ['BTC/USDT'],
            },
            'regimes': {'high_vol': {'execution_overrides': {'layer_ratios': [0.05, 0.05], 'layer_max_total_ratio': 0.13, 'max_layers_per_signal': 1, 'min_add_interval_seconds': 600, 'profit_only_add': True}}}
        }
        snapshot = build_execution_effective_snapshot(cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动'))
        self.assertFalse(snapshot['layering_profile_really_enforced'])
        self.assertFalse(snapshot['plan_shape_really_enforced'])
        self.assertEqual(snapshot['plan_shape_validation']['reason'], 'layering_profile_enforcement_disabled')
        self.assertEqual(snapshot['live']['layer_ratios'], snapshot['baseline']['layer_ratios'])

    def test_layering_shape_snapshot_rollout_fraction_controls_live_shape(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {
            'enabled': True,
            'mode': 'guarded_execute',
            'guarded_execute': {
                'execution_profile_hints_enabled': True,
                'execution_profile_enforcement_enabled': True,
                'layering_profile_enforcement_enabled': True,
                'layering_plan_shape_enforcement_enabled': True,
                'layering_plan_shape_rollout_fraction': 1.0,
            },
            'regimes': {'high_vol': {'execution_overrides': {'layer_ratios': [0.05, 0.05], 'layer_max_total_ratio': 0.13, 'max_layers_per_signal': 1, 'min_add_interval_seconds': 600, 'profit_only_add': True}}}
        }
        snapshot = build_execution_effective_snapshot(cfg, 'BTC/USDT', regime_snapshot=build_regime_snapshot('high_vol', 0.9, {'volatility': 0.05}, '高波动'))
        self.assertTrue(snapshot['shape_live_rollout_match'])
        self.assertTrue(snapshot['plan_shape_really_enforced'])
        self.assertEqual(snapshot['live']['layer_ratios'], [0.05, 0.05])


class TestAdaptiveRegimeConfigAndPolicy(unittest.TestCase):
    def test_adaptive_regime_defaults_are_safe(self):
        cfg = Config()
        cfg._config['adaptive_regime'] = {}
        adaptive = cfg.get_adaptive_regime_config()
        self.assertFalse(adaptive['enabled'])
        self.assertEqual(adaptive['mode'], 'observe_only')
        guarded = adaptive.get('guarded_execute') or {}
        self.assertFalse(guarded.get('layering_profile_hints_enabled', True))
        self.assertFalse(guarded.get('layering_profile_enforcement_enabled', True))
        self.assertFalse(guarded.get('layering_plan_shape_enforcement_enabled', True))
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
        self.assertTrue(policy['policy_version'])
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


    def test_get_signal_regime_snapshot_falls_back_to_policy_snapshot(self):
        from core.regime_policy import get_signal_regime_snapshot
        # signal has adaptive_policy_snapshot with regime info but no direct regime_snapshot
        policy_snapshot = {
            'mode': 'guarded_execute',
            'regime_name': 'high_vol_up',
            'regime_confidence': 0.87,
            'regime_family': 'vol',
            'regime_direction': 'up',
            'regime_confidence': 0.87,
            'stability_score': 0.41,
            'transition_risk': 0.33,
            'regime_details': 'reconstructed from adaptive policy',
            'regime_indicators': {'volatility': 0.06},
        }
        result = get_signal_regime_snapshot(signal=None, regime_snapshot=None, policy_snapshot=policy_snapshot)
        self.assertEqual(result['name'], 'high_vol_up')
        self.assertEqual(result['confidence'], 0.87)
        self.assertEqual(result['family'], 'vol')
        self.assertEqual(result['direction'], 'up')
        self.assertEqual(result['stability_score'], 0.41)
        self.assertEqual(result['transition_risk'], 0.33)
        self.assertNotEqual(result['regime'], 'unknown')

    def test_get_signal_regime_snapshot_prefers_explicit_regime_snapshot(self):
        from core.regime_policy import get_signal_regime_snapshot
        explicit_regime = build_regime_snapshot('trend', 0.91, {'ema_gap': 0.03})
        policy_snapshot = {
            'mode': 'guarded_execute',
            'regime_name': 'high_vol',
            'regime_confidence': 0.55,
        }
        result = get_signal_regime_snapshot(signal=None, regime_snapshot=explicit_regime, policy_snapshot=policy_snapshot)
        self.assertEqual(result['name'], 'trend')
        self.assertEqual(result['confidence'], 0.91)
        self.assertEqual(result['regime'], 'trend')

    def test_get_signal_regime_snapshot_missing_all_returns_unknown(self):
        from core.regime_policy import get_signal_regime_snapshot
        result = get_signal_regime_snapshot(signal=None, regime_snapshot=None, policy_snapshot=None)
        self.assertEqual(result['regime'], 'unknown')
        self.assertEqual(result['confidence'], 0.0)

    def test_build_observe_only_payload_uses_policy_snapshot_fallback(self):
        from core.regime_policy import build_observe_only_payload
        policy_snapshot = {
            'mode': 'guarded_execute',
            'regime_name': 'range_bound',
            'regime_confidence': 0.76,
            'regime_family': 'range',
            'regime_direction': 'sideways',
            'stability_score': 0.50,
            'transition_risk': 0.25,
        }
        cfg = Config()
        payload = build_observe_only_payload(cfg, 'BTC/USDT', signal=None, policy_snapshot=policy_snapshot)
        self.assertEqual(payload['regime_snapshot']['name'], 'range_bound')
        self.assertEqual(payload['regime_snapshot']['confidence'], 0.76)
        self.assertNotEqual(payload['regime_observe_only']['summary'], 'unknown[unknown] conf=0.00')


if __name__ == '__main__':
    unittest.main()
