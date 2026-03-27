import copy
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml

from core.config import Config, DEFAULT_ADAPTIVE_REGIME_CONFIG
from core.regime import build_regime_snapshot, normalize_regime_snapshot
from core.regime_policy import resolve_regime_policy, build_risk_effective_snapshot, build_execution_effective_snapshot
from signals import Signal, EntryDecider, SignalValidator


SUPPORTED_CASE_TYPES = {"shadow_signal", "shadow_execution"}
SUPPORTED_MODES = {"baseline", "adaptive", "guarded_execute", "decision_only", "observe_only", "full"}


@dataclass
class ValidationCase:
    raw: Dict[str, Any]

    @property
    def case_id(self) -> str:
        return str(self.raw.get("case_id") or "unknown-case")

    @property
    def case_type(self) -> str:
        return str(self.raw.get("case_type") or "shadow_execution")


class ValidationCaseError(ValueError):
    pass


class ShadowConfig:
    def __init__(self, base: Config = None, overrides: Dict[str, Any] = None):
        self._base = base or Config()
        self._config = copy.deepcopy(getattr(self._base, "all", {}) or {})
        if overrides:
            self._deep_merge_inplace(self._config, overrides)
        self.all = self._config
        self.db_path = getattr(self._base, "db_path", "data/trading.db")

    def _deep_merge_inplace(self, base: Dict[str, Any], override: Dict[str, Any]):
        for key, value in (override or {}).items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                self._deep_merge_inplace(base[key], value)
            else:
                base[key] = copy.deepcopy(value)

    def get(self, key: str, default=None):
        value = self._config
        for part in (key or '').split('.'):
            if not part:
                continue
            if not isinstance(value, dict):
                return default
            value = value.get(part)
            if value is None:
                return default
        return value

    def get_symbol_value(self, symbol: str, key: str, default=None):
        overrides = ((self._config.get('symbol_overrides') or {}).get(symbol) or {}) if isinstance(self._config, dict) else {}
        value = overrides
        for part in (key or '').split('.'):
            if not part:
                continue
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(part)
            if value is None:
                break
        if value is not None:
            return value
        return self.get(key, default)

    def get_symbol_overrides(self, symbol: str) -> Dict[str, Any]:
        overrides = self._config.get('symbol_overrides') or {}
        if not isinstance(overrides, dict):
            return {}
        return copy.deepcopy(overrides.get(symbol) or {})

    def get_symbol_section(self, symbol: str, key: str) -> Dict[str, Any]:
        base_section = copy.deepcopy(self.get(key, {}) or {})
        overrides = self.get_symbol_overrides(symbol)
        override_section = overrides
        for part in (key or '').split('.'):
            if not part:
                continue
            if not isinstance(override_section, dict):
                override_section = {}
                break
            override_section = override_section.get(part)
            if override_section is None:
                override_section = {}
                break
        if isinstance(override_section, dict):
            self._deep_merge_inplace(base_section, override_section)
        return base_section

    def get_adaptive_regime_config(self, symbol: str = None) -> Dict[str, Any]:
        merged = copy.deepcopy(DEFAULT_ADAPTIVE_REGIME_CONFIG)
        self._deep_merge_inplace(merged, self._config.get('adaptive_regime') or {})
        if symbol:
            symbol_adaptive = ((self._config.get('symbol_overrides') or {}).get(symbol) or {}).get('adaptive_regime') or {}
            if isinstance(symbol_adaptive, dict):
                self._deep_merge_inplace(merged, symbol_adaptive)
        return merged


def _load_case_raw(path: str) -> Dict[str, Any]:
    content = Path(path).read_text(encoding="utf-8")
    suffix = Path(path).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        data = yaml.safe_load(content) or {}
    else:
        data = json.loads(content)
    if not isinstance(data, dict):
        raise ValidationCaseError("validation case must be an object")
    return data


def load_validation_case(path: str) -> ValidationCase:
    data = _load_case_raw(path)
    case = ValidationCase(raw=data)
    _validate_case(case.raw)
    return case


def _validate_case(data: Dict[str, Any]):
    if not data.get("case_id"):
        raise ValidationCaseError("case_id is required")
    case_type = str(data.get("case_type") or "")
    if case_type not in SUPPORTED_CASE_TYPES:
        raise ValidationCaseError(f"unsupported case_type: {case_type}")
    mode = str(data.get("mode") or "guarded_execute")
    if mode not in SUPPORTED_MODES:
        raise ValidationCaseError(f"unsupported mode: {mode}")
    signal = data.get("input", {}).get("signal") or {}
    if not signal.get("symbol"):
        raise ValidationCaseError("input.signal.symbol is required")
    if signal.get("signal_type") not in {"buy", "sell", "hold"}:
        raise ValidationCaseError("input.signal.signal_type must be buy/sell/hold")


def _build_signal(case_data: Dict[str, Any]) -> Signal:
    signal_input = copy.deepcopy(case_data.get("input", {}).get("signal") or {})
    market_context = signal_input.get("market_context") or {}
    regime_snapshot = signal_input.get("regime_snapshot")
    if regime_snapshot:
        normalized = normalize_regime_snapshot(regime_snapshot)
    else:
        regime_name = market_context.get("regime") or "unknown"
        normalized = build_regime_snapshot(
            regime=regime_name,
            confidence=float(market_context.get("regime_confidence", 0.0) or 0.0),
            indicators={
                "volatility": market_context.get("volatility"),
                "ema_direction": 1 if market_context.get("trend") == "bullish" else (-1 if market_context.get("trend") == "bearish" else 0),
            },
            details=market_context.get("regime_details") or "shadow validation case",
        )
    signal_input.setdefault("price", signal_input.get("current_price") or 0)
    signal_input.setdefault("strength", 0)
    signal_input.setdefault("strategies_triggered", [])
    signal_input.setdefault("reasons", [])
    signal_input.setdefault("market_context", market_context)
    signal_input.setdefault("regime_info", {
        "regime": normalized.get("regime", "unknown"),
        "confidence": normalized.get("confidence", 0.0),
    })
    signal_input["regime_snapshot"] = normalized
    signal_input.setdefault("adaptive_policy_snapshot", {})
    return Signal(**signal_input)


def _evaluate_with_config(cfg: ShadowConfig, signal: Signal) -> Dict[str, Any]:
    signal = copy.deepcopy(signal)
    regime_snapshot = getattr(signal, "regime_snapshot", None) or build_regime_snapshot("unknown", 0.0, {}, "shadow")
    policy_snapshot = resolve_regime_policy(cfg, signal.symbol, regime_snapshot)
    signal.regime_snapshot = regime_snapshot
    signal.adaptive_policy_snapshot = policy_snapshot
    signal.regime_info = signal.regime_info or {
        "regime": regime_snapshot.get("regime", "unknown"),
        "confidence": regime_snapshot.get("confidence", 0.0),
    }

    decision = EntryDecider(cfg.all).decide(signal)
    validator_passed, validator_reason, validator_details = SignalValidator(cfg, None).validate(signal)
    risk_snapshot = build_risk_effective_snapshot(cfg, signal.symbol, signal=signal, regime_snapshot=regime_snapshot, policy_snapshot=policy_snapshot)
    execution_snapshot = build_execution_effective_snapshot(cfg, signal.symbol, signal=signal, regime_snapshot=regime_snapshot, policy_snapshot=policy_snapshot)

    return {
        "signal": {
            "symbol": signal.symbol,
            "signal_type": signal.signal_type,
            "strength": signal.strength,
            "strategies_triggered": list(signal.strategies_triggered or []),
        },
        "regime_snapshot": regime_snapshot,
        "adaptive_policy_snapshot": policy_snapshot,
        "decision": decision.to_dict(),
        "validator": {
            "passed": validator_passed,
            "reason": validator_reason,
            "details": validator_details,
        },
        "risk": {
            "effective_state": risk_snapshot.get("effective_state"),
            "observe_only": risk_snapshot.get("observe_only"),
            "baseline": risk_snapshot.get("baseline"),
            "effective": risk_snapshot.get("effective"),
            "enforced_budget": risk_snapshot.get("enforced_budget"),
            "would_tighten": risk_snapshot.get("would_tighten"),
            "would_tighten_fields": risk_snapshot.get("would_tighten_fields"),
            "enforced_fields": risk_snapshot.get("enforced_fields"),
            "hint_codes": risk_snapshot.get("hint_codes"),
        },
        "execution": {
            "effective_state": execution_snapshot.get("effective_state"),
            "observe_only": execution_snapshot.get("observe_only"),
            "baseline": execution_snapshot.get("baseline"),
            "effective": execution_snapshot.get("effective"),
            "live": execution_snapshot.get("live"),
            "enforced_profile": execution_snapshot.get("enforced_profile"),
            "would_tighten": execution_snapshot.get("would_tighten"),
            "would_tighten_fields": execution_snapshot.get("would_tighten_fields"),
            "enforced_fields": execution_snapshot.get("enforced_fields"),
            "execution_profile_really_enforced": execution_snapshot.get("execution_profile_really_enforced"),
            "layering_profile_really_enforced": execution_snapshot.get("layering_profile_really_enforced"),
            "plan_shape_really_enforced": execution_snapshot.get("plan_shape_really_enforced"),
            "hint_codes": execution_snapshot.get("hint_codes"),
        },
    }


def _build_diff(baseline: Dict[str, Any], adaptive: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "decision": {
            "baseline": baseline["decision"]["decision"],
            "adaptive": adaptive["decision"]["decision"],
            "changed": baseline["decision"]["decision"] != adaptive["decision"]["decision"],
            "score_delta": adaptive["decision"]["score"] - baseline["decision"]["score"],
        },
        "validator": {
            "baseline": baseline["validator"]["passed"],
            "adaptive": adaptive["validator"]["passed"],
            "changed": baseline["validator"]["passed"] != adaptive["validator"]["passed"],
            "baseline_reason": baseline["validator"]["reason"],
            "adaptive_reason": adaptive["validator"]["reason"],
        },
        "risk": {
            "would_tighten": adaptive["risk"]["would_tighten"],
            "tightened_fields": adaptive["risk"]["would_tighten_fields"],
            "enforced_fields": adaptive["risk"]["enforced_fields"],
            "baseline_entry_margin_ratio": (baseline["risk"]["baseline"] or {}).get("base_entry_margin_ratio"),
            "adaptive_entry_margin_ratio": (adaptive["risk"]["effective"] or {}).get("base_entry_margin_ratio"),
        },
        "execution": {
            "would_tighten": adaptive["execution"]["would_tighten"],
            "tightened_fields": adaptive["execution"]["would_tighten_fields"],
            "enforced_fields": adaptive["execution"]["enforced_fields"],
            "execution_profile_really_enforced": adaptive["execution"]["execution_profile_really_enforced"],
            "layering_profile_really_enforced": adaptive["execution"]["layering_profile_really_enforced"],
            "plan_shape_really_enforced": adaptive["execution"]["plan_shape_really_enforced"],
            "baseline_layer_ratios": (baseline["execution"]["baseline"] or {}).get("layer_ratios"),
            "adaptive_live_layer_ratios": (adaptive["execution"]["live"] or {}).get("layer_ratios"),
        },
    }


def _evaluate_assertions(case_data: Dict[str, Any], adaptive: Dict[str, Any], diff: Dict[str, Any]) -> Tuple[bool, list]:
    expectations = case_data.get("expect") or {}
    results = []
    for key, expected in expectations.items():
        if key == "decision":
            actual = adaptive["decision"]["decision"]
        elif key == "validator_pass":
            actual = adaptive["validator"]["passed"]
        elif key == "risk_would_tighten":
            actual = diff["risk"]["would_tighten"]
        elif key == "execution_profile_really_enforced":
            actual = diff["execution"]["execution_profile_really_enforced"]
        elif key == "layering_profile_really_enforced":
            actual = diff["execution"]["layering_profile_really_enforced"]
        elif key == "plan_shape_really_enforced":
            actual = diff["execution"]["plan_shape_really_enforced"]
        else:
            actual = None
        results.append({"field": key, "expected": expected, "actual": actual, "passed": actual == expected})
    return all(item["passed"] for item in results) if results else True, results


def run_shadow_validation_case(case_path: str, *, base_config: Config = None) -> Dict[str, Any]:
    case = load_validation_case(case_path)
    base_config = base_config or Config()
    signal = _build_signal(case.raw)

    baseline_cfg = ShadowConfig(base=base_config, overrides=case.raw.get("baseline_config_overrides") or {})
    adaptive_cfg = ShadowConfig(base=base_config, overrides=case.raw.get("config_overrides") or {})

    baseline = _evaluate_with_config(baseline_cfg, signal)
    adaptive = _evaluate_with_config(adaptive_cfg, signal)
    diff = _build_diff(baseline, adaptive)
    passed, assertions = _evaluate_assertions(case.raw, adaptive, diff)

    return {
        "case_id": case.case_id,
        "case_type": case.case_type,
        "mode": case.raw.get("mode") or "guarded_execute",
        "status": "pass" if passed else "fail",
        "baseline": baseline,
        "adaptive": adaptive,
        "diff": diff,
        "assertions": assertions,
        "artifacts": {
            "baseline_policy_snapshot": baseline.get("adaptive_policy_snapshot"),
            "adaptive_policy_snapshot": adaptive.get("adaptive_policy_snapshot"),
            "adaptive_regime_snapshot": adaptive.get("regime_snapshot"),
        },
        "audit": {
            "generated_at": datetime.now().isoformat(),
            "real_trade_execution": False,
            "exchange_mode": "shadow",
            "case_path": str(case_path),
        },
    }
