"""
OKX量化交易机器人 - 主程序入口
使用方法:
    python bot/run.py                 # 运行交易
    python bot/run.py --dashboard     # 运行仪表盘
    python bot/run.py --train         # 训练模型
    python bot/run.py --collect        # 收集数据
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import json
import time
import pandas as pd
from datetime import datetime
from pathlib import Path

from core.config import Config
from core.database import Database
from core.exchange import Exchange
from core.logger import logger
from core.notifier import NotificationManager
from core.presets import PresetManager
from signals import SignalDetector, SignalValidator, SignalRecorder
from trading import TradingExecutor, RiskManager
from ml.engine import MLEngine, ModelTrainer, DataCollector
from analytics import StrategyBacktester, SignalQualityAnalyzer, ParameterOptimizer, GovernanceEngine


def build_exchange_diagnostics(cfg: Config, exchange: Exchange) -> dict:
    """构建交易所诊断信息（只读，不下单）"""
    report = {
        'exchange_mode': cfg.exchange_mode,
        'position_mode': cfg.position_mode,
        'symbols': [],
        'balance_error': None,
    }

    available = 0
    try:
        balance = exchange.fetch_balance()
        available = float((balance.get('free') or {}).get('USDT', 0) or 0)
        report['available_usdt'] = round(available, 4)
    except Exception as e:
        report['available_usdt'] = 0
        report['balance_error'] = str(e)

    desired_notional = available * float(cfg.position_size or 0) * float(cfg.leverage or 0)

    for symbol in cfg.symbols:
        row = {'symbol': symbol}
        try:
            row['is_futures_symbol'] = bool(exchange.is_futures_symbol(symbol))
            if row['is_futures_symbol']:
                row['order_symbol'] = exchange.get_order_symbol(symbol)
                ticker = exchange.fetch_ticker(symbol)
                row['last_price'] = ticker.get('last')
                if row['last_price']:
                    row['sample_amount'] = exchange.normalize_contract_amount(symbol, desired_notional, row['last_price'])
                preview = {'tdMode': 'isolated'}
                if str(cfg.position_mode).lower() not in {'oneway', 'one-way', 'net', 'single'}:
                    preview['posSide'] = 'long'
                row['order_params_preview'] = preview
            else:
                row['reason'] = 'not-swap-market'
        except Exception as e:
            row['error'] = str(e)
        report['symbols'].append(row)

    return report


def build_exchange_smoke_plan(cfg: Config, exchange: Exchange, symbol: str = None, side: str = 'long') -> dict:
    """构建最小 testnet 验收计划；默认只预演，不落单"""
    selected_symbol = symbol or (cfg.symbols[0] if cfg.symbols else None)
    plan = {
        'exchange_mode': cfg.exchange_mode,
        'position_mode': cfg.position_mode,
        'symbol': selected_symbol,
        'side': side,
        'execute_ready': False,
        'steps': [
            '读取余额',
            '检查目标是否为 U 本位永续',
            '获取最新价格',
            '换算最小验收仓位数量',
            '预览开仓参数',
            '预览平仓参数',
        ]
    }
    if not selected_symbol:
        plan['error'] = '未配置任何 watch_list 币种'
        return plan
    try:
        balance = exchange.fetch_balance()
        available = float((balance.get('free') or {}).get('USDT', 0) or 0)
        plan['available_usdt'] = round(available, 4)
        plan['is_futures_symbol'] = bool(exchange.is_futures_symbol(selected_symbol))
        if not plan['is_futures_symbol']:
            plan['error'] = '目标币种不是可用合约'
            return plan
        ticker = exchange.fetch_ticker(selected_symbol)
        last_price = float(ticker.get('last') or 0)
        plan['last_price'] = last_price
        smoke_notional = max(5.0, available * 0.01)
        plan['smoke_notional'] = round(smoke_notional, 4)
        plan['sample_amount'] = exchange.normalize_contract_amount(selected_symbol, smoke_notional, last_price)
        preview_open = {'tdMode': 'isolated'}
        preview_close = {'tdMode': 'isolated', 'reduceOnly': True}
        if str(cfg.position_mode).lower() not in {'oneway', 'one-way', 'net', 'single'}:
            preview_open['posSide'] = side
            preview_close['posSide'] = side
        plan['open_preview'] = {
            'symbol': exchange.get_order_symbol(selected_symbol),
            'side': 'buy' if side == 'long' else 'sell',
            'amount': plan['sample_amount'],
            'params': preview_open,
        }
        plan['close_preview'] = {
            'symbol': exchange.get_order_symbol(selected_symbol),
            'side': 'sell' if side == 'long' else 'buy',
            'amount': plan['sample_amount'],
            'params': preview_close,
        }
        plan['execute_ready'] = True
    except Exception as e:
        plan['error'] = str(e)
    return plan


def execute_exchange_smoke(cfg: Config, exchange: Exchange, symbol: str = None, side: str = 'long', db: Database = None) -> dict:
    """执行最小 testnet 开平仓验收。只允许 testnet。"""
    plan = build_exchange_smoke_plan(cfg, exchange, symbol=symbol, side=side)
    result = {'plan': plan, 'opened': False, 'closed': False}
    if plan.get('error'):
        result['error'] = plan['error']
    elif str(cfg.exchange_mode).lower() != 'testnet':
        result['error'] = '只允许在 testnet 模式执行 smoke 验收'
    else:
        try:
            open_side = 'buy' if side == 'long' else 'sell'
            close_side = 'sell' if side == 'long' else 'buy'
            amount = plan['sample_amount']
            open_order = exchange.create_order(plan['symbol'], open_side, amount, posSide=side)
            result['opened'] = True
            result['open_order'] = open_order
            close_order = exchange.close_order(plan['symbol'], close_side, amount, posSide=side)
            result['closed'] = True
            result['close_order'] = close_order
        except Exception as e:
            result['error'] = str(e)

    if db is not None:
        details = {
            'plan': plan,
            'opened': result.get('opened', False),
            'closed': result.get('closed', False),
            'open_order': result.get('open_order'),
            'close_order': result.get('close_order'),
        }
        smoke_run_id = db.record_smoke_run(
            exchange_mode=cfg.exchange_mode,
            position_mode=cfg.position_mode,
            symbol=plan.get('symbol') or symbol or '--',
            side=side,
            amount=plan.get('sample_amount'),
            success=bool(result.get('opened') and result.get('closed') and not result.get('error')),
            error=result.get('error'),
            details=details,
        )
        result['smoke_run_id'] = smoke_run_id
    return result


class RuntimeGuard:
    def __init__(self, lock_path: str = '/tmp/crypto_quant_okx_bot.lock'):
        self.lock_path = Path(lock_path)
        self.locked = False

    def acquire(self) -> bool:
        if self.lock_path.exists():
            try:
                pid = int(self.lock_path.read_text().strip() or 0)
                if pid > 0:
                    os.kill(pid, 0)
                    return False
            except Exception:
                pass
            try:
                self.lock_path.unlink()
            except Exception:
                return False
        self.lock_path.write_text(str(os.getpid()))
        self.locked = True
        return True

    def release(self):
        if self.locked and self.lock_path.exists():
            try:
                self.lock_path.unlink()
            except Exception:
                pass
        self.locked = False


class TradingBot:
    """交易机器人主类"""
    
    def __init__(self):
        self.config = Config()
        self.db = Database(self.config.db_path)
        self.exchange = Exchange(self.config.all)
        self.detector = SignalDetector(self.config.all)
        self.validator = SignalValidator(self.config, self.exchange)
        self.recorder = SignalRecorder(self.db)
        self.executor = TradingExecutor(self.config, self.exchange, self.db)
        self.risk_mgr = RiskManager(self.config, self.db)
        self.ml = MLEngine(self.config.all)
        self.notifier = NotificationManager(self.config, self.db, logger)
        
        logger.info("交易机器人初始化完成")
    
    def run(self):
        """运行交易循环"""
        started_at = datetime.now()
        summary = {'started_at': started_at.isoformat(), 'symbols': len(self.config.symbols), 'signals': 0, 'passed': 0, 'opened': 0, 'closed': 0, 'errors': 0}
        print(f"\n{'='*60}")
        print(f"🤖 OKX量化交易系统 v2.0")
        print(f"   时间: {started_at}")
        print(f"   币种: {', '.join(self.config.symbols)}")
        print(f"{'='*60}\n")
        self.notifier.notify_runtime('start', [f'时间：{started_at.isoformat()}', f'监控币种：{", ".join(self.config.symbols)}'])
        
        # 获取余额
        try:
            balance = self.exchange.fetch_balance()
            available = balance.get('free', {}).get('USDT', 0)
            print(f"💰 账户余额: {available:.2f} USDT\n")
        except Exception as e:
            print(f"⚠️ 获取余额失败: {e}")
            available = 0
        
        # 获取当前持仓
        positions = self.db.get_positions()
        print(f"📊 当前持仓: {len(positions)}个")
        for p in positions:
            print(f"   {p['symbol']} | {p['side']} | {p['quantity']} | "
                  f"开仓: {p['entry_price']:.2f} | 当前: {p.get('current_price', 'N/A')}")
        print()
        
        # 遍历所有监控的币种
        for symbol in self.config.symbols:
            print(f"=== 分析 {symbol} ===")
            
            try:
                if not self.exchange.is_futures_symbol(symbol):
                    print(f"   ⏭️ 跳过: {symbol} 暂无U本位永续合约")
                    print()
                    continue
                # 获取K线数据
                ohlcv = self.exchange.fetch_ohlcv(symbol, '1h', limit=100)
                df = pd.DataFrame(ohlcv)
                df = self._add_indicators(df)
                
                # 获取当前价格
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                
                # 获取ML预测
                ml_pred = None
                if self.ml.enabled:
                    ml_pred = self.ml.predict(symbol, ohlcv)
                
                # 分析信号
                signal = self.detector.analyze(symbol, df, current_price, ml_pred)
                
                print(f"   价格: {current_price:.4f}")
                print(f"   信号: {signal.signal_type.upper()} | 强度: {signal.strength}%")
                print(f"   触发策略: {', '.join(signal.strategies_triggered) or '无'}")
                
                # 详细指标
                indicators = signal.indicators
                if 'RSI' in indicators:
                    print(f"   RSI: {indicators.get('RSI', 'N/A')}")
                if 'MACD' in indicators:
                    print(f"   MACD: {indicators.get('MACD', 'N/A')}")
                
                # 获取当前持仓
                current_positions = {p['symbol']: p for p in positions}
                
                # 验证信号
                passed, reason, details = self.validator.validate(signal, current_positions)
                summary['signals'] += 1
                if passed:
                    summary['passed'] += 1
                signal.filtered = not passed
                signal.filter_reason = reason
                
                if not passed:
                    print(f"   ❌ 信号过滤: {reason}")
                self.notifier.notify_signal(signal, passed, reason, details)
                
                # 记录信号
                signal_id = self.recorder.record(signal, (passed, reason, details))
                
                # 如果信号通过且可以开仓
                if passed and signal.signal_type in ['buy', 'sell']:
                    # 风险检查
                    can_open, risk_reason, risk_details = self.risk_mgr.can_open_position(symbol)
                    self.notifier.notify_decision(signal, can_open, risk_reason, risk_details)
                    
                    if can_open:
                        side = 'long' if signal.signal_type == 'buy' else 'short'
                        
                        # 开仓
                        trade_id = self.executor.open_position(
                            symbol, side, current_price, signal_id
                        )
                        
                        if trade_id:
                            summary['opened'] += 1
                            self.recorder.mark_executed(signal_id, trade_id)
                            self.notifier.notify_trade_open(symbol, side, current_price, self.db.get_latest_open_trade(symbol, side).get('quantity') if self.db.get_latest_open_trade(symbol, side) else 0, trade_id, signal)
                            print(f"   ✅ 开{'多' if side == 'long' else '空'}成功! Trade ID: {trade_id}")
                        else:
                            self.notifier.notify_error('开仓失败', f'{symbol} {side} 开仓未成功', {'signal_id': signal_id})
                            print(f"   ❌ 开仓失败")
                    else:
                        print(f"   ⏸️ 风险检查阻止: {risk_reason}")
                
                print()
                
            except Exception as e:
                summary['errors'] += 1
                self.notifier.notify_error('处理币种出错', f'{symbol}: {e}', {'symbol': symbol})
                logger.error(f"处理{symbol}出错: {e}")
                print(f"   ⚠️ 错误: {e}\n")
        
        # 检查现有持仓的止盈止损
        print("=== 检查持仓 ===")
        positions = self.db.get_positions()
        
        for position in positions:
            symbol = position['symbol']
            
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                
                # 更新持仓价格
                self.db.update_position(
                    symbol, position['side'], position['entry_price'],
                    position['quantity'], position['leverage'], current_price
                )
                
                # 检查止损
                if self.executor.check_stop_loss(symbol, current_price):
                    summary['closed'] += 1
                    self.executor.close_position(symbol, '止损')
                    self.notifier.notify_trade_close(symbol, position['side'], current_price, '止损')
                    print(f"   🔴 止损: {symbol}")
                
                # 检查止盈
                elif self.executor.check_take_profit(symbol, current_price):
                    summary['closed'] += 1
                    self.executor.close_position(symbol, '止盈')
                    self.notifier.notify_trade_close(symbol, position['side'], current_price, '止盈')
                    print(f"   🟢 止盈: {symbol}")
                
            except Exception as e:
                summary['errors'] += 1
                self.notifier.notify_error('检查持仓失败', f'{symbol}: {e}', {'symbol': symbol})
                logger.error(f"检查持仓{symbol}出错: {e}")
        finished_at = datetime.now()
        summary['finished_at'] = finished_at.isoformat()
        self.notifier.notify_runtime('end', [f'开始：{summary["started_at"]}', f'结束：{summary["finished_at"]}', f'信号：{summary["signals"]} ｜ 通过：{summary["passed"]} ｜ 开仓：{summary["opened"]} ｜ 平仓：{summary["closed"]} ｜ 错误：{summary["errors"]}'], summary)
        print(f"\n✅ 交易循环完成! {finished_at}\n")
        return summary
    
    def _add_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """添加技术指标"""
        close = df[4]
        
        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0)
        loss = -delta.where(delta < 0, 0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / (avg_loss + 1e-10)
        df['RSI'] = 100 - (100 / (1 + rs))
        
        # MACD
        ema12 = close.ewm(span=12).mean()
        ema26 = close.ewm(span=26).mean()
        df['MACD'] = ema12 - ema26
        df['MACD_signal'] = df['MACD'].ewm(span=9).mean()
        
        # 布林带
        df['BB_mid'] = close.rolling(20).mean()
        std = close.rolling(20).std()
        df['BB_upper'] = df['BB_mid'] + 2 * std
        df['BB_lower'] = df['BB_mid'] - 2 * std
        
        return df


def main():
    parser = argparse.ArgumentParser(description='OKX量化交易机器人')
    parser.add_argument('--dashboard', action='store_true', help='启动仪表盘')
    parser.add_argument('--daemon', action='store_true', help='守护模式定时运行交易循环')
    parser.add_argument('--interval-seconds', type=int, help='守护模式执行间隔秒数，默认取 config.runtime.interval_seconds')
    parser.add_argument('--train', action='store_true', help='训练模型')
    parser.add_argument('--collect', action='store_true', help='收集数据')
    parser.add_argument('--backtest', action='store_true', help='运行回测')
    parser.add_argument('--signal-quality', action='store_true', help='分析信号质量')
    parser.add_argument('--optimize', action='store_true', help='运行参数优化与币种分层')
    parser.add_argument('--list-presets', action='store_true', help='列出可用预设')
    parser.add_argument('--apply-preset', type=str, help='应用预设配置')
    parser.add_argument('--mode-status', action='store_true', help='显示当前模式状态')
    parser.add_argument('--daily-summary', action='store_true', help='生成日报摘要')
    parser.add_argument('--cleanup-runtime-records', action='store_true', help='清理重复的治理/日报运行记录')
    parser.add_argument('--exchange-diagnose', action='store_true', help='只读诊断交易所/合约参数，不执行下单')
    parser.add_argument('--exchange-smoke', action='store_true', help='生成最小 testnet 验收计划；默认只预演')
    parser.add_argument('--execute', action='store_true', help='配合 smoke 验收命令，显式允许执行 testnet 开平仓')
    parser.add_argument('--symbol', type=str, help='指定 smoke/diagnose 目标币种')
    parser.add_argument('--side', type=str, default='long', choices=['long', 'short'], help='smoke 验收方向')
    parser.add_argument('--dry-run', action='store_true', help='配合清理命令，仅预览不删除')
    parser.add_argument('--port', type=int, default=8050, help='仪表盘端口')
    
    args = parser.parse_args()
    
    if args.dashboard:
        # 启动仪表盘
        from dashboard.api import run_dashboard
        run_dashboard(port=args.port)

    elif args.daemon:
        cfg = Config()
        interval = args.interval_seconds or int(cfg.get('runtime.interval_seconds', 300))
        guard = RuntimeGuard()
        notifier = NotificationManager(cfg, Database(cfg.db_path), logger)
        notifier.notify_runtime('daemon', [f'守护间隔：{interval} 秒', f'监控币种：{", ".join(cfg.symbols)}'])
        print(f"\n🔁 守护模式启动，间隔 {interval} 秒\n")
        while True:
            if not guard.acquire():
                notifier.notify_runtime('skip', ['检测到已有交易周期正在运行，本轮跳过'])
                time.sleep(interval)
                continue
            try:
                bot = TradingBot()
                bot.run()
            except Exception as e:
                notifier.notify_error('守护周期异常', str(e), {'interval': interval})
                logger.error(f'守护周期异常: {e}')
            finally:
                guard.release()
            time.sleep(interval)
    
    elif args.train:
        # 训练模型
        print("\n🎯 开始训练模型...\n")
        cfg = Config()
        exchange = Exchange(cfg.all)
        collector = DataCollector(exchange, cfg.all)
        trainer = ModelTrainer(cfg.all)
        results = {}

        for symbol in cfg.symbols:
            if not exchange.is_futures_symbol(symbol):
                print(f"跳过 {symbol}: 暂无U本位永续合约")
                results[symbol] = False
                continue
            print(f"收集并训练 {symbol}...")
            if collector.collect_data(symbol, '1h', 1000):
                import pandas as pd
                csv_name = symbol.replace('/', '_').replace(':', '_')
                symbol_map = {
                    'BTC/USDT': 'BTC_USDT',
                    'ETH/USDT': 'ETH_USDT',
                    'SOL/USDT': 'SOL_USDT',
                    'XRP/USDT': 'XRP_USDT',
                    'HYPE/USDT': 'HYPE_USDT'
                }
                filename = symbol_map.get(symbol, csv_name)
                df = pd.read_csv(f"ml/data/{filename}_1h.csv")
                results[symbol] = trainer.train(symbol, df)
            else:
                results[symbol] = False

        print("\n训练结果:")
        for symbol, success in results.items():
            print(f"   {symbol}: {'✅ 成功' if success else '❌ 失败'}")

    elif args.collect:
        # 收集数据
        print("\n📊 开始收集数据...\n")
        config = Config()
        exchange = Exchange(config.all)
        collector = DataCollector(exchange, config.all)

        for symbol in config.symbols:
            if not exchange.is_futures_symbol(symbol):
                print(f"跳过 {symbol}: 暂无U本位永续合约")
                continue
            print(f"收集 {symbol}...")
            collector.collect_data(symbol, '1h', 1000)

    elif args.backtest:
        print("\n🧪 开始回测...\n")
        cfg = Config()
        backtester = StrategyBacktester(cfg)
        result = backtester.run_all()
        print("回测总览:")
        print(result['summary'])
        print("\n分币种结果:")
        for row in result['symbols']:
            print(f"  {row['symbol']}: trades={row['trades']} win_rate={row['win_rate']}% return={row['total_return_pct']}% dd={row['max_drawdown_pct']}%")

    elif args.signal_quality:
        print("\n🔎 开始分析信号质量...\n")
        cfg = Config()
        db = Database(cfg.db_path)
        analyzer = SignalQualityAnalyzer(cfg, db)
        result = analyzer.analyze()
        print("信号质量总览:")
        print(result['summary'])
        print("\n分币种质量:")
        for row in result['by_symbol']:
            print(f"  {row['symbol']}: signals={row['signals']} positive_rate={row['positive_rate']}% avg_quality={row['avg_quality_pct']}%")

    elif args.optimize:
        print("\n⚙️ 开始参数优化与币种分层...\n")
        cfg = Config()
        db = Database(cfg.db_path)
        optimizer = ParameterOptimizer(cfg, db)
        result = optimizer.run(use_cache=False)
        print("最佳实验:")
        print(result['best_experiment'])
        print("\n币种分层建议:")
        for row in result['symbol_advice']:
            print(f"  {row['symbol']}: {row['tier']} | backtest={row['backtest_return_pct']}% | quality={row['avg_quality_pct']}% | {row['action']}")
        print("\n单币种专项实验:")
        for symbol, rows in result.get('symbol_specific', {}).items():
            print(f"  [{symbol}]")
            for row in rows:
                print(f"    {row['name']}: score={row['score']} return={row['summary']['total_return_pct']}% win={row['summary']['win_rate']}% dd={row['summary']['max_drawdown_pct']}%")
        print("\n候选晋升判断:")
        for row in result.get('candidate_promotions', []):
            print(f"  {row['symbol']}: {row['decision']} | {row['reason']}")
        print("\n预设配置:")
        for preset in result.get('presets', []):
            print(f"  {preset['name']}: {preset['path']}")

    elif args.list_presets:
        pm = PresetManager(Config())
        print("\n📦 可用预设:\n")
        for row in pm.list_presets():
            print(f"  {row['name']}: watch={row['watch_list']} candidate={row['candidate_watch_list']} paused={row['paused_watch_list']}")

    elif args.apply_preset:
        pm = PresetManager(Config())
        result = pm.apply_preset(args.apply_preset, auto_restart=True)
        print("\n✅ 已应用预设:\n")
        print(result)

    elif args.mode_status:
        pm = PresetManager(Config())
        print("\n🧭 当前模式:\n")
        print(pm.status())

    elif args.daily_summary:
        cfg = Config()
        db = Database(cfg.db_path)
        gov = GovernanceEngine(cfg, db)
        print("\n📰 今日日报:\n")
        print(gov.generate_daily_summary())

    elif args.cleanup_runtime_records:
        cfg = Config()
        db = Database(cfg.db_path)
        print("\n🧹 清理运行期重复记录:\n")
        print(db.cleanup_duplicate_runtime_records(dry_run=args.dry_run))

    elif args.exchange_diagnose:
        cfg = Config()
        exchange = Exchange(cfg.all)
        report = build_exchange_diagnostics(cfg, exchange)
        print("\n🩺 交易所只读诊断:\n")
        print(f"模式: {report['exchange_mode']} | 持仓模式: {report['position_mode']} | 可用USDT: {report.get('available_usdt', 0)}")
        if report.get('balance_error'):
            print(f"余额读取异常: {report['balance_error']}")
        for row in report['symbols']:
            if args.symbol and row['symbol'] != args.symbol:
                continue
            print(f"\n[{row['symbol']}]")
            if row.get('error'):
                print(f"  错误: {row['error']}")
                continue
            print(f"  futures: {'yes' if row.get('is_futures_symbol') else 'no'}")
            if row.get('order_symbol'):
                print(f"  order_symbol: {row['order_symbol']}")
            if row.get('last_price') is not None:
                print(f"  last_price: {row['last_price']}")
            if row.get('sample_amount') is not None:
                print(f"  sample_amount: {row['sample_amount']}")
            if row.get('order_params_preview'):
                print(f"  order_params_preview: {row['order_params_preview']}")
            if row.get('reason'):
                print(f"  reason: {row['reason']}")

    elif args.exchange_smoke:
        cfg = Config()
        exchange = Exchange(cfg.all)
        plan = build_exchange_smoke_plan(cfg, exchange, symbol=args.symbol, side=args.side)
        print("\n🧪 Testnet 最小验收计划:\n")
        if plan.get('error'):
            print(f"错误: {plan['error']}")
        else:
            print(f"模式: {plan['exchange_mode']} | 持仓模式: {plan['position_mode']} | 目标: {plan['symbol']} | 方向: {plan['side']}")
            print(f"可用USDT: {plan.get('available_usdt', 0)} | 最新价: {plan.get('last_price')} | 验收名义价值: {plan.get('smoke_notional')}")
            print(f"样例数量: {plan.get('sample_amount')} | 可执行: {'yes' if plan.get('execute_ready') else 'no'}")
            print('步骤:')
            for step in plan.get('steps', []):
                print(f"  - {step}")
            if plan.get('open_preview'):
                print(f"开仓预览: {plan['open_preview']}")
            if plan.get('close_preview'):
                print(f"平仓预览: {plan['close_preview']}")
        if args.execute:
            print("\n🚨 已显式允许执行 testnet 最小开平仓验收\n")
            db = Database(cfg.db_path)
            result = execute_exchange_smoke(cfg, exchange, symbol=args.symbol, side=args.side, db=db)
            if result.get('error'):
                print(f"执行结果: 失败 | {result['error']}")
            else:
                print(f"执行结果: 开仓 {'成功' if result.get('opened') else '失败'} / 平仓 {'成功' if result.get('closed') else '失败'}")
                if result.get('open_order'):
                    print(f"open_order: {result['open_order']}")
                if result.get('close_order'):
                    print(f"close_order: {result['close_order']}")
            if result.get('smoke_run_id'):
                print(f"smoke_run_id: {result['smoke_run_id']}")

    else:
        # 运行交易
        bot = TradingBot()
        bot.run()


if __name__ == '__main__':
    main()
