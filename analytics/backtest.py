"""回测与信号质量分析模块"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from core.config import Config
from core.database import Database
from signals.detector import SignalDetector
from signals.validator import SignalValidator
from core.regime_policy import normalize_observe_only_view, summarize_observe_only_collection


@dataclass
class BacktestPosition:
    side: str
    entry_price: float
    entry_time: str
    highest_price: float
    lowest_price: float
    signal_strength: int
    regime_snapshot: Optional[Dict] = None
    adaptive_policy_snapshot: Optional[Dict] = None


class MarketDataLoader:
    def __init__(self, data_dir: str = 'ml/data'):
        self.data_dir = Path(data_dir)

    def symbol_to_filename(self, symbol: str) -> str:
        mapping = {
            'BTC/USDT': 'BTC_USDT',
            'ETH/USDT': 'ETH_USDT',
            'SOL/USDT': 'SOL_USDT',
            'XRP/USDT': 'XRP_USDT',
            'HYPE/USDT': 'HYPE_USDT',
        }
        return mapping.get(symbol, symbol.replace('/', '_').replace(':', '_'))

    def load_symbol(self, symbol: str, timeframe: str = '1h') -> Optional[pd.DataFrame]:
        path = self.data_dir / f'{self.symbol_to_filename(symbol)}_{timeframe}.csv'
        if not path.exists():
            return None
        df = pd.read_csv(path)
        if 'datetime' in df.columns:
            df['datetime'] = pd.to_datetime(df['datetime'])
        elif 'timestamp' in df.columns:
            df['datetime'] = pd.to_datetime(df['timestamp'], unit='ms')
        return df


class StrategyBacktester:
    def __init__(self, config: Config):
        self.config = config
        self.detector = SignalDetector(config.all)
        self.validator = SignalValidator(config, None)
        self.loader = MarketDataLoader()
        self._cache = None
        self._cache_at = None

    def run_all(self, symbols: Optional[List[str]] = None, timeframe: str = '1h', use_cache: bool = True) -> Dict:
        now = datetime.now()
        if use_cache and self._cache is not None and self._cache_at and (now - self._cache_at).total_seconds() < 300:
            return self._cache

        symbols = symbols or self.config.symbols
        results = []
        for symbol in symbols:
            df = self.loader.load_symbol(symbol, timeframe)
            if df is None or len(df) < 150:
                continue
            results.append(self._run_symbol(symbol, df))

        summary = self._aggregate_results(results)
        self._cache = summary
        self._cache_at = now
        return summary

    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        close = out['close']
        delta = close.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / (avg_loss + 1e-10)
        out['RSI'] = 100 - (100 / (1 + rs))

        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        out['MACD'] = ema12 - ema26
        out['MACD_signal'] = out['MACD'].ewm(span=9).mean()

        out['BB_mid'] = close.rolling(20).mean()
        std = close.rolling(20).std()
        out['BB_upper'] = out['BB_mid'] + 2 * std
        out['BB_lower'] = out['BB_mid'] - 2 * std
        return out

    def _to_detector_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        frame = pd.DataFrame({
            0: df['timestamp'].values if 'timestamp' in df.columns else range(len(df)),
            1: df['open'].values,
            2: df['high'].values,
            3: df['low'].values,
            4: df['close'].values,
            5: df['volume'].values,
            'RSI': df['RSI'].values,
            'MACD': df['MACD'].values,
            'MACD_signal': df['MACD_signal'].values,
            'BB_mid': df['BB_mid'].values,
            'BB_upper': df['BB_upper'].values,
            'BB_lower': df['BB_lower'].values,
        })
        return frame

    def _run_symbol(self, symbol: str, raw_df: pd.DataFrame) -> Dict:
        df = self._add_indicators(raw_df)
        trades = []
        position: Optional[BacktestPosition] = None
        warmup = max(80, 5 + 20 + 60)
        stop_loss = float(self.config.get('trading', {}).get('stop_loss', 0.02))
        take_profit = float(self.config.get('trading', {}).get('take_profit', 0.04))
        trailing_stop = float(self.config.get('trading', {}).get('trailing_stop', 0.015))

        for i in range(warmup, len(df) - 1):
            window = df.iloc[: i + 1].copy()
            detector_df = self._to_detector_frame(window)
            current_row = window.iloc[-1]
            current_price = float(current_row['close'])
            timestamp = str(current_row['datetime'])
            signal = self.detector.analyze(symbol, detector_df, current_price, None)

            current_positions = {}
            if position:
                current_positions[symbol] = {
                    'symbol': symbol,
                    'side': position.side,
                    'entry_price': position.entry_price,
                    'current_price': current_price,
                    'quantity': 1.0,
                    'leverage': self.config.get('trading', {}).get('leverage', 3),
                }

            passed, _, _ = self.validator.validate(signal, current_positions=current_positions, tracking_data={})

            if position:
                if position.side == 'long':
                    position.highest_price = max(position.highest_price, current_price)
                    pnl = (current_price - position.entry_price) / position.entry_price
                    trailing_hit = current_price <= position.highest_price * (1 - trailing_stop)
                    opposite_hit = signal.signal_type == 'sell' and signal.strength >= max(25, position.signal_strength * 0.8)
                else:
                    position.lowest_price = min(position.lowest_price, current_price)
                    pnl = (position.entry_price - current_price) / position.entry_price
                    trailing_hit = current_price >= position.lowest_price * (1 + trailing_stop)
                    opposite_hit = signal.signal_type == 'buy' and signal.strength >= max(25, position.signal_strength * 0.8)

                exit_reason = None
                if pnl <= -stop_loss:
                    exit_reason = 'stop_loss'
                elif pnl >= take_profit:
                    exit_reason = 'take_profit'
                elif trailing_hit and pnl > 0:
                    exit_reason = 'trailing_stop'
                elif opposite_hit:
                    exit_reason = 'opposite_signal'

                if exit_reason:
                    trades.append({
                        'symbol': symbol,
                        'side': position.side,
                        'entry_time': position.entry_time,
                        'exit_time': timestamp,
                        'entry_price': position.entry_price,
                        'exit_price': current_price,
                        'return_pct': round(pnl * 100, 4),
                        'reason': exit_reason,
                        'regime_tag': ((position.regime_snapshot or {}).get('name') if position.regime_snapshot else None),
                        'policy_tag': ((position.adaptive_policy_snapshot or {}).get('policy_version') if position.adaptive_policy_snapshot else None),
                        'observe_only': normalize_observe_only_view(
                            regime_snapshot=position.regime_snapshot or {},
                            policy_snapshot=position.adaptive_policy_snapshot or {},
                            fallback_summary=(position.adaptive_policy_snapshot or {}).get('summary') or (position.regime_snapshot or {}).get('details'),
                        ),
                    })
                    position = None
                    continue

            if not position and passed and signal.signal_type in ['buy', 'sell']:
                side = 'long' if signal.signal_type == 'buy' else 'short'
                position = BacktestPosition(
                    side=side,
                    entry_price=current_price,
                    entry_time=timestamp,
                    highest_price=current_price,
                    lowest_price=current_price,
                    signal_strength=signal.strength,
                    regime_snapshot=dict(getattr(signal, 'regime_snapshot', {}) or getattr(signal, 'regime_info', {}) or {}),
                    adaptive_policy_snapshot=dict(getattr(signal, 'adaptive_policy_snapshot', {}) or {}),
                )

        if position:
            last_row = df.iloc[-1]
            last_price = float(last_row['close'])
            pnl = ((last_price - position.entry_price) / position.entry_price) if position.side == 'long' else ((position.entry_price - last_price) / position.entry_price)
            trades.append({
                'symbol': symbol,
                'side': position.side,
                'entry_time': position.entry_time,
                'exit_time': str(last_row['datetime']),
                'entry_price': position.entry_price,
                'exit_price': last_price,
                'return_pct': round(pnl * 100, 4),
                'reason': 'end_of_backtest',
                'regime_tag': ((position.regime_snapshot or {}).get('name') if position.regime_snapshot else None),
                'policy_tag': ((position.adaptive_policy_snapshot or {}).get('policy_version') if position.adaptive_policy_snapshot else None),
                'observe_only': normalize_observe_only_view(
                    regime_snapshot=position.regime_snapshot or {},
                    policy_snapshot=position.adaptive_policy_snapshot or {},
                    fallback_summary=(position.adaptive_policy_snapshot or {}).get('summary') or (position.regime_snapshot or {}).get('details'),
                ),
            })

        total_return = sum(t['return_pct'] for t in trades)
        wins = sum(1 for t in trades if t['return_pct'] > 0)
        losses = sum(1 for t in trades if t['return_pct'] < 0)
        equity = 0.0
        peak = 0.0
        max_drawdown = 0.0
        for t in trades:
            equity += t['return_pct']
            peak = max(peak, equity)
            max_drawdown = min(max_drawdown, equity - peak)

        regime_tags = sorted({t.get('regime_tag') for t in trades if t.get('regime_tag')})
        policy_tags = sorted({t.get('policy_tag') for t in trades if t.get('policy_tag')})
        observe_only_tags = sorted({tag for t in trades for tag in ((t.get('observe_only') or {}).get('tags') or []) if tag})
        return {
            'symbol': symbol,
            'trades': len(trades),
            'wins': wins,
            'losses': losses,
            'win_rate': round((wins / len(trades) * 100), 2) if trades else 0.0,
            'total_return_pct': round(total_return, 4),
            'avg_return_pct': round((total_return / len(trades)), 4) if trades else 0.0,
            'max_drawdown_pct': round(max_drawdown, 4),
            'recent_trades': trades[-10:],
            'observe_only_summary_view': summarize_observe_only_collection(trades[-10:]),
            'regime_tags': regime_tags,
            'policy_tags': policy_tags,
            'observe_only_tags': observe_only_tags,
        }

    def _aggregate_results(self, symbol_results: List[Dict]) -> Dict:
        total_trades = sum(x['trades'] for x in symbol_results)
        total_wins = sum(x['wins'] for x in symbol_results)
        total_return = sum(x['total_return_pct'] for x in symbol_results)
        max_drawdown = min([x['max_drawdown_pct'] for x in symbol_results], default=0.0)
        observe_only_tags = sorted({tag for row in symbol_results for tag in (row.get('observe_only_tags') or []) if tag})
        regime_tags = sorted({tag for row in symbol_results for tag in (row.get('regime_tags') or []) if tag})
        policy_tags = sorted({tag for row in symbol_results for tag in (row.get('policy_tags') or []) if tag})
        observe_only_summary_view = summarize_observe_only_collection([
            trade
            for row in symbol_results
            for trade in (row.get('recent_trades') or [])
            if trade.get('observe_only')
        ])
        return {
            'summary': {
                'symbols': len(symbol_results),
                'total_trades': total_trades,
                'win_rate': round((total_wins / total_trades * 100), 2) if total_trades else 0.0,
                'total_return_pct': round(total_return, 4),
                'max_drawdown_pct': round(max_drawdown, 4),
                'observe_only': True,
                'observe_only_tags': observe_only_tags,
                'observe_only_banner': observe_only_summary_view.get('banner'),
                'observe_only_summary_view': observe_only_summary_view,
                'regime_tags': regime_tags,
                'policy_tags': policy_tags,
            },
            'symbols': symbol_results,
        }


class SignalQualityAnalyzer:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.loader = MarketDataLoader()
        self.detector = SignalDetector(config.all)
        self._cache = None
        self._cache_at = None

    def analyze(self, limit: int = 200, use_cache: bool = True, symbols: Optional[List[str]] = None) -> Dict:
        now = datetime.now()
        cache_allowed = use_cache and symbols is None
        if cache_allowed and self._cache is not None and self._cache_at and (now - self._cache_at).total_seconds() < 300:
            return self._cache

        target_symbols = list(dict.fromkeys(symbols or []))
        signals = self.db.get_signals(limit=limit)
        by_symbol = {}
        rows = []
        for signal in signals:
            symbol = signal.get('symbol')
            if target_symbols and symbol not in target_symbols:
                continue
            if symbol not in by_symbol:
                df = self.loader.load_symbol(symbol)
                by_symbol[symbol] = df
            df = by_symbol[symbol]
            if df is None or df.empty:
                continue
            row = self._score_signal(signal, df)
            if row:
                rows.append(row)

        valid_symbols = {r.get('symbol') for r in rows if r.get('avg_quality_pct') is not None}
        missing_symbols = [s for s in target_symbols if s not in valid_symbols]
        if (not rows or not any(r.get('avg_quality_pct') is not None for r in rows)):
            rows = self._analyze_historical_generated_signals(symbols=target_symbols or None)
        elif missing_symbols:
            rows.extend(self._analyze_historical_generated_signals(symbols=missing_symbols))

        summary = self._summarize(rows)
        if cache_allowed:
            self._cache = summary
            self._cache_at = now
        return summary

    def _score_signal(self, signal: Dict, df: pd.DataFrame) -> Optional[Dict]:
        created_at = signal.get('created_at')
        if not created_at:
            return None
        try:
            created_ts = pd.to_datetime(created_at)
        except Exception:
            return None

        timeline = df[['datetime', 'close']].copy()
        timeline['delta'] = (timeline['datetime'] - created_ts).abs()
        nearest = timeline.sort_values('delta').iloc[0]
        if pd.isna(nearest['delta']) or nearest['delta'] > pd.Timedelta(hours=2):
            return None
        idx = timeline.sort_values('delta').index[0]
        base_price = float(nearest['close'])
        direction = signal.get('signal_type')
        if direction not in ['buy', 'sell']:
            return None

        def calc_horizon_ret(steps: int) -> Optional[float]:
            target_idx = idx + steps
            if target_idx >= len(df):
                return None
            future_price = float(df.iloc[target_idx]['close'])
            ret = (future_price - base_price) / base_price
            if direction == 'sell':
                ret = -ret
            return round(ret * 100, 4)

        r1 = calc_horizon_ret(1)
        r3 = calc_horizon_ret(3)
        r6 = calc_horizon_ret(6)
        quality_score = [x for x in [r1, r3, r6] if x is not None]
        avg = round(sum(quality_score) / len(quality_score), 4) if quality_score else None
        return {
            'symbol': signal.get('symbol'),
            'created_at': created_at,
            'signal_type': direction,
            'strength': signal.get('strength', 0),
            'filtered': signal.get('filtered', False),
            'filter_reason': signal.get('filter_reason'),
            'return_1h_pct': r1,
            'return_3h_pct': r3,
            'return_6h_pct': r6,
            'avg_quality_pct': avg,
        }

    def _analyze_historical_generated_signals(self, symbols: Optional[List[str]] = None) -> List[Dict]:
        rows = []
        symbols = symbols or self.config.symbols
        for symbol in symbols:
            df = self.loader.load_symbol(symbol)
            if df is None or len(df) < 150:
                continue
            df = df.copy()
            close = df['close']
            delta = close.diff()
            gain = delta.where(delta > 0, 0)
            loss = -delta.where(delta < 0, 0)
            avg_gain = gain.rolling(14).mean()
            avg_loss = loss.rolling(14).mean()
            rs = avg_gain / (avg_loss + 1e-10)
            df['RSI'] = 100 - (100 / (1 + rs))
            ema12 = close.ewm(span=12).mean()
            ema26 = close.ewm(span=26).mean()
            df['MACD'] = ema12 - ema26
            df['MACD_signal'] = df['MACD'].ewm(span=9).mean()
            df['BB_mid'] = close.rolling(20).mean()
            std = close.rolling(20).std()
            df['BB_upper'] = df['BB_mid'] + 2 * std
            df['BB_lower'] = df['BB_mid'] - 2 * std

            for i in range(80, len(df) - 6):
                window = df.iloc[: i + 1]
                detector_df = pd.DataFrame({
                    0: window['timestamp'].values if 'timestamp' in window.columns else range(len(window)),
                    1: window['open'].values,
                    2: window['high'].values,
                    3: window['low'].values,
                    4: window['close'].values,
                    5: window['volume'].values,
                    'RSI': window['RSI'].values,
                    'MACD': window['MACD'].values,
                    'MACD_signal': window['MACD_signal'].values,
                    'BB_mid': window['BB_mid'].values,
                    'BB_upper': window['BB_upper'].values,
                    'BB_lower': window['BB_lower'].values,
                })
                current_price = float(window.iloc[-1]['close'])
                signal = self.detector.analyze(symbol, detector_df, current_price, None)
                if signal.signal_type not in ['buy', 'sell']:
                    continue
                mock_signal = {
                    'symbol': symbol,
                    'created_at': str(window.iloc[-1]['datetime']),
                    'signal_type': signal.signal_type,
                    'strength': signal.strength,
                    'filtered': False,
                    'filter_reason': None,
                }
                scored = self._score_signal(mock_signal, df)
                if scored:
                    rows.append(scored)
        return rows

    def _summarize(self, rows: List[Dict]) -> Dict:
        valid = [r for r in rows if r.get('avg_quality_pct') is not None]
        positive = sum(1 for r in valid if r['avg_quality_pct'] > 0)
        by_symbol = {}
        for r in valid:
            sym = r['symbol']
            by_symbol.setdefault(sym, []).append(r['avg_quality_pct'])
        symbol_stats = []
        for sym, vals in by_symbol.items():
            symbol_stats.append({
                'symbol': sym,
                'signals': len(vals),
                'avg_quality_pct': round(sum(vals) / len(vals), 4),
                'positive_rate': round(sum(1 for v in vals if v > 0) / len(vals) * 100, 2),
            })
        symbol_stats.sort(key=lambda x: x['avg_quality_pct'], reverse=True)
        valid.sort(key=lambda x: x['created_at'], reverse=True)
        observe_only_summary_view = summarize_observe_only_collection([
            trade
            for row in symbol_results
            for trade in (row.get('recent_trades') or [])
            if trade.get('observe_only')
        ])
        return {
            'summary': {
                'signals_scored': len(valid),
                'positive_rate': round((positive / len(valid) * 100), 2) if valid else 0.0,
                'avg_quality_pct': round(sum(r['avg_quality_pct'] for r in valid) / len(valid), 4) if valid else 0.0,
            },
            'by_symbol': symbol_stats,
            'recent': valid[:50],
        }
