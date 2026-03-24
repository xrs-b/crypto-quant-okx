"""风险预算型仓位控制辅助函数。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


DEFAULT_RISK_BUDGET = {
    'total_margin_cap_ratio': 0.30,
    'total_margin_soft_cap_ratio': 0.25,
    'symbol_margin_cap_ratio': 0.12,
    'base_entry_margin_ratio': 0.08,
    'min_entry_margin_ratio': 0.04,
    'max_entry_margin_ratio': 0.10,
    'add_position_enabled': False,
    'quality_scaling_enabled': False,
    'high_quality_multiplier': 1.15,
    'low_quality_multiplier': 0.75,
}


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def get_risk_budget_config(config: Any, symbol: Optional[str] = None) -> Dict[str, Any]:
    trading = config.get_symbol_section(symbol, 'trading') if symbol and hasattr(config, 'get_symbol_section') else (config.get('trading', {}) or {})
    merged = dict(DEFAULT_RISK_BUDGET)
    merged.update({k: trading.get(k) for k in DEFAULT_RISK_BUDGET.keys() if trading.get(k) is not None})

    # 向后兼容旧字段
    if trading.get('max_exposure') is not None:
        merged['total_margin_cap_ratio'] = float(trading.get('max_exposure'))
    if trading.get('max_position_per_symbol') is not None:
        merged['symbol_margin_cap_ratio'] = float(trading.get('max_position_per_symbol'))
    if trading.get('position_size') is not None:
        merged['base_entry_margin_ratio'] = float(trading.get('position_size'))

    merged['total_margin_cap_ratio'] = float(merged['total_margin_cap_ratio'])
    merged['total_margin_soft_cap_ratio'] = float(merged['total_margin_soft_cap_ratio'])
    merged['symbol_margin_cap_ratio'] = float(merged['symbol_margin_cap_ratio'])
    merged['base_entry_margin_ratio'] = float(merged['base_entry_margin_ratio'])
    merged['min_entry_margin_ratio'] = float(merged['min_entry_margin_ratio'])
    merged['max_entry_margin_ratio'] = float(merged['max_entry_margin_ratio'])
    merged['add_position_enabled'] = bool(merged['add_position_enabled'])
    merged['quality_scaling_enabled'] = bool(merged['quality_scaling_enabled'])
    merged['high_quality_multiplier'] = float(merged['high_quality_multiplier'])
    merged['low_quality_multiplier'] = float(merged['low_quality_multiplier'])

    merged['total_margin_soft_cap_ratio'] = min(merged['total_margin_soft_cap_ratio'], merged['total_margin_cap_ratio'])
    merged['min_entry_margin_ratio'] = min(merged['min_entry_margin_ratio'], merged['max_entry_margin_ratio'])
    merged['base_entry_margin_ratio'] = _clamp(
        merged['base_entry_margin_ratio'],
        merged['min_entry_margin_ratio'],
        merged['max_entry_margin_ratio'],
    )
    return merged


def summarize_margin_usage(positions: List[Dict[str, Any]], symbol: str, mark_price: Optional[float] = None) -> Dict[str, Any]:
    total_margin = 0.0
    symbol_margin = 0.0
    same_side_positions: List[Dict[str, Any]] = []
    normalized_positions: List[Dict[str, Any]] = []

    for pos in positions or []:
        pos_symbol = pos.get('symbol')
        pos_side = str(pos.get('side') or '').lower()
        if pos_side in {'buy', 'long'}:
            pos_side = 'long'
        elif pos_side in {'sell', 'short'}:
            pos_side = 'short'
        qty = float(pos.get('coin_quantity', 0) or 0)
        if qty <= 0:
            contracts = float(pos.get('quantity') or pos.get('contracts') or 0)
            contract_size = float(pos.get('contract_size', 1) or 1)
            qty = contracts * contract_size
        price = float(pos.get('current_price') or pos.get('entry_price') or mark_price or 0)
        leverage = max(1, int(float(pos.get('leverage', 1) or 1)))
        margin_used = (qty * price) / leverage if qty and price else 0.0
        row = {
            'symbol': pos_symbol,
            'side': pos_side,
            'coin_quantity': qty,
            'price': price,
            'leverage': leverage,
            'margin_used': margin_used,
        }
        normalized_positions.append(row)
        total_margin += margin_used
        if pos_symbol == symbol:
            symbol_margin += margin_used
    return {
        'positions': normalized_positions,
        'current_total_margin': total_margin,
        'current_symbol_margin': symbol_margin,
    }


def derive_quality_bucket(signal: Any = None) -> str:
    strength = float(getattr(signal, 'strength', 0) or 0)
    strategies = len(getattr(signal, 'strategies_triggered', []) or [])
    if strength >= 60 or strategies >= 3:
        return 'high'
    if strength <= 30 and strategies <= 1:
        return 'low'
    return 'normal'


def compute_entry_plan(*, total_balance: float, free_balance: float, current_total_margin: float, current_symbol_margin: float,
                       risk_budget: Dict[str, Any], signal: Any = None) -> Dict[str, Any]:
    total_balance = float(total_balance or 0)
    free_balance = max(0.0, float(free_balance or 0))
    current_total_margin = max(0.0, float(current_total_margin or 0))
    current_symbol_margin = max(0.0, float(current_symbol_margin or 0))
    rb = dict(DEFAULT_RISK_BUDGET)
    rb.update(risk_budget or {})

    total_cap = total_balance * rb['total_margin_cap_ratio']
    total_soft_cap = total_balance * rb['total_margin_soft_cap_ratio']
    symbol_cap = total_balance * rb['symbol_margin_cap_ratio']
    remaining_total_cap = max(0.0, total_cap - current_total_margin)
    remaining_symbol_cap = max(0.0, symbol_cap - current_symbol_margin)

    entry_ratio = rb['base_entry_margin_ratio']
    quality_bucket = derive_quality_bucket(signal)
    quality_multiplier = 1.0
    if rb['quality_scaling_enabled']:
        if quality_bucket == 'high':
            quality_multiplier = rb['high_quality_multiplier']
        elif quality_bucket == 'low':
            quality_multiplier = rb['low_quality_multiplier']
        entry_ratio *= quality_multiplier

    if current_total_margin >= total_soft_cap:
        entry_ratio = min(entry_ratio, rb['min_entry_margin_ratio'])

    entry_ratio = _clamp(entry_ratio, rb['min_entry_margin_ratio'], rb['max_entry_margin_ratio'])
    target_margin = total_balance * entry_ratio
    allowed_margin = min(target_margin, remaining_total_cap, remaining_symbol_cap, free_balance)
    if total_balance > 0 and allowed_margin > 0:
        effective_entry_ratio = allowed_margin / total_balance
    else:
        effective_entry_ratio = 0.0

    blocked = False
    block_reason = None
    if total_balance <= 0:
        blocked = True
        block_reason = '账户总资产不可用'
    elif free_balance <= 0:
        blocked = True
        block_reason = '可用保证金不足'
    elif remaining_total_cap <= 0:
        blocked = True
        block_reason = '总保证金硬上限已用尽'
    elif remaining_symbol_cap <= 0:
        blocked = True
        block_reason = '单币种保证金上限已用尽'
    elif allowed_margin <= 0 or effective_entry_ratio <= 0:
        blocked = True
        block_reason = '当前剩余预算不足以开新仓'
    elif effective_entry_ratio + 1e-9 < rb['min_entry_margin_ratio']:
        blocked = True
        block_reason = '剩余风险预算低于最小开仓门槛'

    projected_total_margin = current_total_margin + (0 if blocked else allowed_margin)
    projected_symbol_margin = current_symbol_margin + (0 if blocked else allowed_margin)

    return {
        'risk_budget': rb,
        'quality_bucket': quality_bucket,
        'quality_multiplier': round(quality_multiplier, 4),
        'target_entry_margin_ratio': round(entry_ratio, 6),
        'effective_entry_margin_ratio': round(effective_entry_ratio, 6),
        'target_margin': round(target_margin, 6),
        'allowed_margin': round(allowed_margin, 6),
        'total_balance': round(total_balance, 6),
        'free_balance': round(free_balance, 6),
        'remaining_total_cap': round(remaining_total_cap, 6),
        'remaining_symbol_cap': round(remaining_symbol_cap, 6),
        'current_total_margin': round(current_total_margin, 6),
        'current_symbol_margin': round(current_symbol_margin, 6),
        'current_total_exposure_ratio': round(current_total_margin / total_balance, 6) if total_balance > 0 else 0.0,
        'current_symbol_exposure_ratio': round(current_symbol_margin / total_balance, 6) if total_balance > 0 else 0.0,
        'projected_total_exposure_ratio': round(projected_total_margin / total_balance, 6) if total_balance > 0 else 0.0,
        'projected_symbol_exposure_ratio': round(projected_symbol_margin / total_balance, 6) if total_balance > 0 else 0.0,
        'soft_cap_reached': current_total_margin >= total_soft_cap if total_balance > 0 else False,
        'blocked': blocked,
        'block_reason': block_reason,
    }
