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
    parser.add_argument('--port', type=int, default=8050, help='仪表盘端口')
    
    args = parser.parse_args()
    
    if args.dashboard:
        # 启动仪表盘
        from dashboard.api import run_dashboard
        run_dashboard(port=args.port)
    
    elif args.train:
        # 训练模型
        print("\n🎯 开始训练模型...\n")
        trainer = ModelTrainer(Config().all)
        results = trainer.auto_train_all()
        
        print("\n训练结果:")
        for symbol, success in results.items():
            print(f"   {symbol}: {'✅ 成功' if success else '❌ 失败'}")
    
    elif args.collect:
        # 收集数据
        print("\n📊 开始收集数据...\n")
        config = Config()
        collector = DataCollector(Exchange(config.all), config.all)
        
        for symbol in config.symbols:
            print(f"收集 {symbol}...")
            collector.collect_data(symbol, '1h', 1000)
    
    else:
        # 运行交易
        bot = TradingBot()
        bot.run()


if __name__ == '__main__':
    main()
