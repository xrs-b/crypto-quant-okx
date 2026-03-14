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
import pandas as pd
from datetime import datetime

from core.config import Config
from core.database import Database
from core.exchange import Exchange
from core.logger import logger
from signals import SignalDetector, SignalValidator, SignalRecorder
from trading import TradingExecutor, RiskManager
from ml.engine import MLEngine, ModelTrainer, DataCollector
from analytics import StrategyBacktester, SignalQualityAnalyzer, ParameterOptimizer


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
        
        logger.info("交易机器人初始化完成")
    
    def run(self):
        """运行交易循环"""
        print(f"\n{'='*60}")
        print(f"🤖 OKX量化交易系统 v2.0")
        print(f"   时间: {datetime.now()}")
        print(f"   币种: {', '.join(self.config.symbols)}")
        print(f"{'='*60}\n")
        
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
                signal.filtered = not passed
                signal.filter_reason = reason
                
                if not passed:
                    print(f"   ❌ 信号过滤: {reason}")
                
                # 记录信号
                signal_id = self.recorder.record(signal, (passed, reason, details))
                
                # 如果信号通过且可以开仓
                if passed and signal.signal_type in ['buy', 'sell']:
                    # 风险检查
                    can_open, risk_reason, _ = self.risk_mgr.can_open_position(symbol)
                    
                    if can_open:
                        side = 'long' if signal.signal_type == 'buy' else 'short'
                        
                        # 开仓
                        trade_id = self.executor.open_position(
                            symbol, side, current_price, signal_id
                        )
                        
                        if trade_id:
                            print(f"   ✅ 开{'多' if side == 'long' else '空'}成功! Trade ID: {trade_id}")
                        else:
                            print(f"   ❌ 开仓失败")
                    else:
                        print(f"   ⏸️ 风险检查阻止: {risk_reason}")
                
                print()
                
            except Exception as e:
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
                    self.executor.close_position(symbol, '止损')
                    print(f"   🔴 止损: {symbol}")
                
                # 检查止盈
                elif self.executor.check_take_profit(symbol, current_price):
                    self.executor.close_position(symbol, '止盈')
                    print(f"   🟢 止盈: {symbol}")
                
            except Exception as e:
                logger.error(f"检查持仓{symbol}出错: {e}")
        
        print(f"\n✅ 交易循环完成! {datetime.now()}\n")
    
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
    parser.add_argument('--train', action='store_true', help='训练模型')
    parser.add_argument('--collect', action='store_true', help='收集数据')
    parser.add_argument('--backtest', action='store_true', help='运行回测')
    parser.add_argument('--signal-quality', action='store_true', help='分析信号质量')
    parser.add_argument('--optimize', action='store_true', help='运行参数优化与币种分层')
    parser.add_argument('--port', type=int, default=8050, help='仪表盘端口')
    
    args = parser.parse_args()
    
    if args.dashboard:
        # 启动仪表盘
        from dashboard.api import run_dashboard
        run_dashboard(port=args.port)
    
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
        print("\n预设配置:")
        for preset in result.get('presets', []):
            print(f"  {preset['name']}: {preset['path']}")

    else:
        # 运行交易
        bot = TradingBot()
        bot.run()


if __name__ == '__main__':
    main()
