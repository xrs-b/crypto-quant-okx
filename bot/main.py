"""
[LEGACY ENTRYPOINT — DO NOT USE AS CURRENT PRODUCTION ENTRY]

这是历史主程序入口文件，仅为兼容保留与问题排查参考而存在。

当前正式入口与 legacy 边界统一说明见：
- docs/legacy-index.md

不要把本文件作为当前部署或正式运行入口。
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import joblib
import os
from datetime import datetime

from core import config, db, logger, trade_logger, Exchange
from signals import SignalDetector, SignalValidator, SignalRecorder
from trading import TradingExecutor


# 指标计算
def add_indicators(df):
    """添加技术指标"""
    close = df[4]
    
    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(14).mean()
    avg_loss = loss.rolling(14).mean()
    rs = avg_gain / avg_loss
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


def get_ml_prediction(symbol):
    """获取ML预测"""
    # 映射
    symbol_map = {
        'BTC/USDT': 'BTC_USDT',
        'ETH/USDT': 'ETH_USDT',
        'SOL/USDT': 'SOL_USDT',
        'XRP/USDT': 'XRP_USDT',
        'HYPE/USDT': 'HYPE_USDT'
    }
    
    model_file = f"ml/models/{symbol_map.get(symbol, 'SOL_USDT')}_model.pkl"
    
    if not os.path.exists(model_file):
        return None
    
    try:
        model = joblib.load(model_file)
        
        ex = Exchange(config.all)
        ohlcv = ex.fetch_ohlcv(symbol, '1h', limit=50)
        df = pd.DataFrame(ohlcv)
        df = add_indicators(df)
        
        features = df[['RSI', 'MACD', 'MACD_signal', 'BB_upper', 'BB_lower']].iloc[-1:].fillna(0)
        
        pred = model.predict(features)[0]
        prob = model.predict_proba(features)[0]
        
        return pred, prob[1]
    except Exception as e:
        logger.error(f"ML预测错误: {e}")
        return None


def main():
    """主函数"""
    print(f"\n{'='*50}")
    print(f"🤖 OKX量化交易系统 {datetime.now()}")
    print(f"{'='*50}\n")
    
    # 初始化
    ex = Exchange(config.all)
    detector = SignalDetector(config.all)
    validator = SignalValidator(config, ex)
    recorder = SignalRecorder(db)
    executor = TradingExecutor(config, ex, db)
    
    # 获取余额
    balance = ex.fetch_balance()
    available = balance['free'].get('USDT', 0)
    print(f"💰 余额: {available:.2f} USDT\n")
    
    # 获取持仓
    positions = db.get_positions()
    print(f"📊 持仓: {len(positions)}个")
    for p in positions:
        print(f"   {p['symbol']} {p['side']} {p['quantity']}张")
    print()
    
    # 获取交易对列表
    symbols = config.get('symbols.list', [])
    
    # 追踪数据文件
    tracking_file = '/tmp/okx_trading_tracking.json'
    import json
    try:
        with open(tracking_file, 'r') as f:
            tracking = json.load(f)
    except:
        tracking = {}
    
    current_positions = {p['symbol']: p for p in positions}
    
    for symbol in symbols:
        print(f"=== {symbol} ===")
        
        try:
            # 获取K线数据
            ohlcv = ex.fetch_ohlcv(symbol, '1h', limit=50)
            df = pd.DataFrame(ohlcv)
            df = add_indicators(df)
            mtf_frames = {'1h': df}
            try:
                ohlcv_4h = ex.fetch_ohlcv(symbol, '4h', limit=120)
                if ohlcv_4h:
                    mtf_frames['4h'] = add_indicators(pd.DataFrame(ohlcv_4h))
            except Exception as mtf_exc:
                print(f"⚠️ {symbol} 4h anchor 数据获取失败: {mtf_exc}")
            
            # 获取当前合约价格
            current_price = ex.fetch_reference_price(symbol, prefer='last') if hasattr(ex, 'fetch_reference_price') else ex.fetch_ticker(symbol)['last']
            
            # 获取ML预测
            ml_pred = get_ml_prediction(symbol)
            
            # 分析信号
            signal = detector.analyze(symbol, df, current_price, ml_pred, mtf_frames=mtf_frames)
            
            print(f"价格: {current_price:.2f}")
            print(f"信号: {signal.signal_type.upper()} 强度: {signal.strength}%")
            print(f"策略: {signal.strategies_triggered}")
            
            # 验证信号
            passed, reason, details = validator.validate(signal, current_positions, tracking)
            signal.filtered = not passed
            signal.filter_reason = reason
            
            # 记录信号
            signal_id = recorder.record(signal, (passed, reason, details))
            merged_filter_details = dict(signal.filter_details or {})
            merged_filter_details['observability'] = {
                'signal_id': signal_id,
                'root_signal_id': signal_id,
                'layer_no': None,
                'deny_reason': reason if not passed else None,
                'current_symbol_exposure': 0.0,
                'projected_symbol_exposure': 0.0,
                'current_total_exposure': 0.0,
                'projected_total_exposure': 0.0,
            }
            db.update_signal(signal_id, filter_details=json.dumps(merged_filter_details, ensure_ascii=False))
            
            if not passed:
                print(f"❌ 信号被过滤: {reason}")
                continue
            
            # 执行交易
            if signal.signal_type in ['buy', 'sell']:
                side = 'long' if signal.signal_type == 'buy' else 'short'
                
                risk_mgr = RiskManager(config, db)
                can_open, risk_reason, risk_details = risk_mgr.can_open_position(symbol, side=side, signal_id=signal_id, plan_context={'regime_snapshot': getattr(signal, 'regime_snapshot', {}) or getattr(signal, 'regime_info', {}) or {}, 'adaptive_policy_snapshot': getattr(signal, 'adaptive_policy_snapshot', {}) or {}})
                risk_obs = dict((risk_details or {}).get('observability') or {})
                if risk_obs:
                    merged_filter_details = dict(signal.filter_details or {})
                    merged_filter_details['observability'] = {**dict(merged_filter_details.get('observability') or {}), **risk_obs, 'deny_reason': None if can_open else (risk_reason or risk_obs.get('deny_reason'))}
                    db.update_signal(signal_id, filter_details=json.dumps(merged_filter_details, ensure_ascii=False), filter_reason=(None if can_open else risk_reason))
                if not can_open:
                    print(f"⏸️ 风险检查阻止: {risk_reason}")
                    continue
                trade_id = executor.open_position(
                    symbol, side, current_price, signal_id, plan_context={'regime_snapshot': getattr(signal, 'regime_snapshot', {}) or getattr(signal, 'regime_info', {}) or {}, 'adaptive_policy_snapshot': getattr(signal, 'adaptive_policy_snapshot', {}) or {}}, root_signal_id=signal_id
                )
                
                if trade_id:
                    signal.executed = True
                    recorder.mark_executed(signal_id, trade_id)
                    
                    # 更新追踪数据
                    tracking[symbol] = {
                        'last_price': current_price,
                        'entry': current_price,
                        'side': side,
                        'time': datetime.now().isoformat()
                    }
                    with open(tracking_file, 'w') as f:
                        json.dump(tracking, f)
                    
                    print(f"✅ 开{'多' if side == 'long' else '空'}成功!")
                else:
                    print(f"❌ 开仓失败")
        
        except Exception as e:
            logger.error(f"处理{symbol}出错: {e}")
            print(f"错误: {e}")
        
        print()
    
    # 检查现有持仓的止盈止损
    print("=== 检查持仓 ===")
    for position in positions:
        symbol = position['symbol']
        
        try:
            current_price = ex.fetch_reference_price(symbol, prefer='mark') if hasattr(ex, 'fetch_reference_price') else ex.fetch_ticker(symbol)['last']
            
            # 更新持仓价格
            db.update_position(
                symbol, position['side'], position['entry_price'],
                position['quantity'], position['leverage'], current_price
            )
            
            # 检查止损
            if executor.check_stop_loss(symbol, current_price):
                executor.close_position(symbol, '止损')
                print(f"止损: {symbol}")
            
            # 检查止盈
            elif executor.check_take_profit(symbol, current_price):
                executor.close_position(symbol, '止盈')
                print(f"止盈: {symbol}")
        
        except Exception as e:
            logger.error(f"检查持仓{symbol}出错: {e}")
    
    print(f"\n完成! {datetime.now()}\n")


if __name__ == '__main__':
    main()
