#!/usr/bin/env python3
"""OKX合约量化交易机器人 - 集成ML模型"""
import os, yaml, subprocess
from datetime import datetime
import ccxt
import pandas as pd
import joblib

import json

POSITION_FILE = '/tmp/okx_positions.json'

def load_positions_tracking():
    """加载持仓追踪数据"""
    try:
        with open(POSITION_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_positions_tracking(data):
    """保存持仓追踪数据"""
    with open(POSITION_FILE, 'w') as f:
        json.dump(data, f)


PROJECT_ROOT = "/Volumes/MacHD/Projects/crypto-quant-okx"

# 交易参数 (在main()中从config.yaml加载)
# 全局变量声明
TRADING_PAIRS = None
RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9
POSITION_SIZE = 0.1
MAX_EXPOSURE = 0.3
LEVERAGE = 3
STOP_LOSS_PCT = 0.02
TAKE_PROFIT_PCT = 0.04
TRAILING_STOP_PCT = 0.02
MIN_PRICE_CHANGE = 0.02
TREND_CONFIRMATION = True
MULTI_TIMEFRAME = False
VOLATILITY_ADJUST = False
RSI_DIVERGENCE = False
MA_CROSSOVER = False
VOLUME_CONFIRM = False
PARTIAL_TP_LEVELS = [[0.5, 0.04], [0.3, 0.06]]

# 交易日志
TRADE_LOG_FILE = '/tmp/okx_trades.json'

def log_trade(action, symbol, price, amount, pnl=0, note=''):
    """记录交易到日志"""
    import json
    from datetime import datetime
    log_entry = {
        'time': datetime.now().isoformat(),
        'action': action,
        'symbol': symbol,
        'price': price,
        'amount': amount,
        'pnl': pnl,
        'note': note
    }
    try:
        try:
            with open(TRADE_LOG_FILE, 'r') as f:
                logs = json.load(f)
        except:
            logs = []
        logs.append(log_entry)
        with open(TRADE_LOG_FILE, 'w') as f:
            json.dump(logs[-100:], f, indent=2)
    except:
        pass
    return log_entry






def load_config():
    with open(os.path.join(PROJECT_ROOT, 'config/config.yaml')) as f:
        return yaml.safe_load(f)


def send_discord(msg):
    try:
        cfg = load_config()
        ch = cfg.get('discord', {}).get('channel_id', '')
        subprocess.run(f'/opt/homebrew/bin/openclaw message send --channel discord --target "{ch}" --message "{msg}"', shell=True, capture_output=True, timeout=30)
    except:
        pass


def get_exchange():
    c = load_config()
    a = c.get('api', {})
    return ccxt.okx({
        'apiKey': a.get('key', ''),
        'secret': a.get('secret', ''),
        'password': a.get('passphrase', ''),
        'enableRateLimit': True,
        'timeout': 30000,
        'testnet': (load_config().get('mode', 'testnet') == 'testnet'),
    })


def add_features(df):
    """添加技术指标特征"""
    close = df[4]
    
    # RSI
    delta = close.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(RSI_PERIOD).mean()
    avg_loss = loss.rolling(RSI_PERIOD).mean()
    rs = avg_gain / avg_loss
    df['RSI'] = 100 - (100 / (1 + rs))
    
    # MACD
    ema12 = close.ewm(span=MACD_FAST).mean()
    ema26 = close.ewm(span=MACD_SLOW).mean()
    df['MACD'] = ema12 - ema26
    df['MACD_signal'] = df['MACD'].ewm(span=MACD_SIGNAL).mean()
    
    # 布林带
    df['BB_mid'] = close.rolling(20).mean()
    std = close.rolling(20).std()
    df['BB_upper'] = df['BB_mid'] + 2 * std
    df['BB_lower'] = df['BB_mid'] - 2 * std
    
    # 均线
    df['MA5'] = close.rolling(5).mean()
    df['MA20'] = close.rolling(20).mean()
    
    # 波动率
    df['VOLATILITY'] = std / df['BB_mid']
    
    return df


def get_ml_prediction(symbol):
    """获取ML模型预测"""
    # 映射symbol到模型文件
    symbol_map = {
        'SOL-USDT-SWAP': 'SOL_USDT',
        'HYPE/USDT': 'HYPE_USDT'
    }
    
    model_file = f"{PROJECT_ROOT}/ml/{symbol_map.get(symbol, 'SOL_USDT')}_model.pkl"
    
    if not os.path.exists(model_file):
        return None
    
    try:
        # 加载模型
        model = joblib.load(model_file)
        
        # 获取数据
        ex = get_exchange()
        ohlcv = ex.fetch_ohlcv(symbol, '1h', limit=50)
        df = pd.DataFrame(ohlcv)
        df = add_features(df)
        
        # 特征
        features = ['RSI', 'MACD', 'MACD_signal', 'BB_upper', 'BB_lower', 'MA5', 'MA20', 'VOLATILITY']
        latest = df[features].iloc[-1:].fillna(0)
        
        # 预测
        pred = model.predict(latest)[0]
        prob = model.predict_proba(latest)[0]
        
        return pred, prob[1]  # 预测和上涨概率
        
    except Exception as e:
        print(f"ML预测错误: {e}")
        return None


def get_traditional_signal(df):
    """传统技术指标信号"""
    rsi = df['RSI'].iloc[-1]
    macd = df['MACD'].iloc[-1]
    macd_s = df['MACD_signal'].iloc[-1]
    
    if rsi < RSI_OVERSOLD or (macd > macd_s and rsi < 50):
        return 1
    elif rsi > RSI_OVERBOUGHT or (macd < macd_s and rsi > 50):
        return -1
    return 0


def get_futures_balance(ex):
    bal = ex.fetch_balance({'type': 'future'})
    return bal['free'].get('USDT', 0)


def get_positions(ex):
    positions = ex.fetch_positions()
    open_pos = {}
    for p in positions:
        c = float(p.get('contracts', 0) or 0)
        if c > 0:
            open_pos[p['symbol']] = {'contracts': c, 'side': p.get('side', 'long'), 'entry': float(p.get('entryPrice', 0) or 0)}
    return open_pos


def open_position(ex, symbol, side, amount, posSide=None):
    try:
        # Convert to OKX swap format if needed
        if ':' not in symbol:
            symbol = symbol + ':USDT'
        
        params = {}
        if posSide:
            params = {'posSide': posSide}
        
        if side == 'long':
            ex.create_market_buy_order(symbol, amount, params)
        else:
            ex.create_market_sell_order(symbol, amount, params)
        return True
    except:
        return False


def close_position(ex, symbol, side, amount, posSide=None):
    try:
        # Convert to OKX swap format if needed
        if ':' not in symbol:
            symbol = symbol + ':USDT'
        
        if side == 'long':
            ex.create_market_sell_order(symbol, amount)
        else:
            ex.create_market_buy_order(symbol, amount)
        return True
    except:
        return False



def check_trailing_stop(entry_price, current_price, highest_price, lowest_price, side, trailing_pct):
    """检查追踪止损
    
    Args:
        entry_price: 开仓价格
        current_price: 当前价格
        highest_price: 多仓期间最高价
        lowest_price: 空仓期间最低价
        side: long/short
        trailing_pct: 回调比例
    """
    if side == 'long':
        # 多仓: 追踪最高价
        if highest_price is None or current_price > highest_price:
            highest_price = current_price
        
        # 计算追踪止损价
        stop_price = highest_price * (1 - trailing_pct)
        
        # 如果价格从最高点下跌超过trailing_pct，触发止损
        if current_price <= stop_price:
            return True, stop_price
    
    else:  # short
        # 空仓: 追踪最低价
        if lowest_price is None or current_price < lowest_price:
            lowest_price = current_price
        
        # 计算追踪止损价
        stop_price = lowest_price * (1 + trailing_pct)
        
        # 如果价格从最低点上涨超过trailing_pct，触发止损
        if current_price >= stop_price:
            return True, stop_price
    
    return False, 0


def format_price(p):
    return str(round(p, 2)) + " USDT"


def main():
    # 从config.yaml加载所有参数
    global TRADING_PAIRS, RSI_PERIOD, RSI_OVERSOLD, RSI_OVERBOUGHT
    global POSITION_SIZE, MAX_EXPOSURE, LEVERAGE, STOP_LOSS_PCT, TAKE_PROFIT_PCT, TRAILING_STOP_PCT
    global MIN_PRICE_CHANGE, TREND_CONFIRMATION, MULTI_TIMEFRAME, VOLATILITY_ADJUST
    global RSI_DIVERGENCE, MA_CROSSOVER, VOLUME_CONFIRM, PARTIAL_TP_LEVELS, TRAILING_STOP_PCT
    
    try:
        cfg = load_config()
        t = cfg.get('trading', {})
        s = cfg.get('strategy', {})
        
        TRADING_PAIRS = t.get('symbols', ['SOL-USDT-SWAP', 'HYPE/USDT'])
        RSI_PERIOD = s.get('rsi_period', RSI_PERIOD)
        RSI_OVERSOLD = s.get('rsi_oversold', RSI_OVERSOLD)
        RSI_OVERBOUGHT = s.get('rsi_overbought', RSI_OVERBOUGHT)
        POSITION_SIZE = t.get('position_size', POSITION_SIZE)
        MAX_EXPOSURE = t.get('max_exposure', MAX_EXPOSURE)
        LEVERAGE = t.get('leverage', LEVERAGE)
        STOP_LOSS_PCT = t.get('stop_loss', STOP_LOSS_PCT)
        TAKE_PROFIT_PCT = t.get('take_profit', TAKE_PROFIT_PCT)
        TRAILING_STOP_PCT = t.get('trailing_stop', TRAILING_STOP_PCT)
        MIN_PRICE_CHANGE = t.get('min_price_change', MIN_PRICE_CHANGE)
        TREND_CONFIRMATION = t.get('trend_confirmation', TREND_CONFIRMATION)
        MULTI_TIMEFRAME = t.get('multi_timeframe', MULTI_TIMEFRAME)
        VOLATILITY_ADJUST = t.get('volatility_adjust', VOLATILITY_ADJUST)
        RSI_DIVERGENCE = t.get('rsi_divergence', RSI_DIVERGENCE)
        MA_CROSSOVER = t.get('ma_crossover', MA_CROSSOVER)
        VOLUME_CONFIRM = t.get('volume_confirm', VOLUME_CONFIRM)
        PARTIAL_TP_LEVELS = t.get('partial_tp_levels', PARTIAL_TP_LEVELS)
        print(f"✅ 配置已加载:")
        print(f"   交易对: {TRADING_PAIRS}")
        print(f"   止损: {STOP_LOSS_PCT*100}%")
        print(f"   止盈: {TAKE_PROFIT_PCT*100}%")
        print(f"   追踪止损: {TRAILING_STOP_PCT*100}%")
        print(f"   最小价格变动: {MIN_PRICE_CHANGE*100}%")
        print(f"   趋势确认: {TREND_CONFIRMATION}")
        print(f"   多周期确认: {MULTI_TIMEFRAME}")
        print(f"   波动率调整: {VOLATILITY_ADJUST}")
        print(f"   RSI背离: {RSI_DIVERGENCE}")
        print(f"   均线交叉: {MA_CROSSOVER}")
        print(f"   成交量确认: {VOLUME_CONFIRM}")
    except Exception as e:
        print(f"⚠️ 配置加载失败，使用默认值: {e}")
        TRADING_PAIRS = ['SOL-USDT-SWAP', 'HYPE/USDT']
    
    print(f"🤖 OKX合约+ML {datetime.now()}")
    ex = get_exchange()
    
    usdt = get_futures_balance(ex)
    print(f"余额: {format_price(usdt)}")
    
    pos = get_positions(ex)
    print(f"持仓: {list(pos.keys()) if pos else '无'}")
    
    has_activity = False
    
    # 加载追踪数据
    tracking = load_positions_tracking()
    
    for pair in TRADING_PAIRS:
        print(f"\n=== {pair} ===")
        try:
            # 获取数据
            ohlcv = ex.fetch_ohlcv(pair, '1h', limit=50)
            df = pd.DataFrame(ohlcv)
            df = add_features(df)
            
            tk = ex.fetch_ticker(pair)
            price = float(tk['last'])
            
            # 传统信号
            trad_signal = get_traditional_signal(df)
            rsi = df['RSI'].iloc[-1]
            macd = df['MACD'].iloc[-1]
            macd_s = df['MACD_signal'].iloc[-1]
            
            
            # ML信号
            ml_result = get_ml_prediction(pair)
            
            print(f"价格: {format_price(price)}")
            print(f"RSI: {rsi:.1f}")
            
            if ml_result:
                ml_pred, ml_prob = ml_result
                print(f"ML预测: {'涨' if ml_pred == 1 else '跌'} (概率: {str(round(ml_prob*100, 1)) + '%'})")
            else:
                ml_pred, ml_prob = None, 0.5
            
            # 综合信号 (传统 + ML)
            final_signal = 0
            
            # 改进的信号确认: 需要双重确认
            # 买入: 传统信号+ML (>0.7) 或者 传统信号+ML (>0.6) + RSI超卖
            # 卖出: 传统信号+ML (<0.3) 或者 传统信号+ML (<0.4) + RSI超买
            
            ml_strong_buy = ml_prob > 0.75 if ml_result else False
            ml_strong_sell = ml_prob < 0.25 if ml_result else False
            
            if trad_signal == 1 and ml_strong_buy:
                final_signal = 1
                print("信号: 传统买入 + ML强烈确认 = 买入 ✅")
            elif trad_signal == 1 and ml_prob > 0.65 and rsi < 40:
                final_signal = 1
                print("信号: 传统买入 + ML + RSI超卖 = 买入")
            elif trad_signal == -1 and ml_strong_sell:
                final_signal = -1
                print("信号: 传统卖出 + ML强烈确认 = 卖出 ✅")
            elif trad_signal == -1 and ml_prob < 0.35 and rsi > 60:
                final_signal = -1
                print("信号: 传统卖出 + ML + RSI超买 = 卖出")
            else:
                final_signal = trad_signal
                print(f"信号: 传统信号 = {'买入' if final_signal == 1 else '卖出' if final_signal == -1 else '观望'}")
            
            # 检查持仓
            p = pos.get(pair)
            if p:
                entry, side, cnt = p['entry'], p['side'], p['contracts']
                pnl_pct = ((price-entry)/entry*100) if side=='long' else ((entry-price)/entry*100)
                pnl_pct_leveraged = pnl_pct * LEVERAGE  # 杠杆后盈亏
                
                # 追踪止损检查 - 从持久化文件加载
                tracked = tracking.get(pair, {})
                highest = tracked.get('highest', entry)
                lowest = tracked.get('lowest', entry)
                
                # 更新最高/最低价/最后价格并保存
                if side == 'long':
                    if highest is None or price > highest:
                        highest = price
                else:
                    if lowest is None or price < lowest:
                        lowest = price
                
                # 更新最后交易价格
                tracked['last_price'] = price
                
                # 保存追踪数据
                tracking[pair] = {
                    'highest': highest,
                    'lowest': lowest,
                    'entry': entry,
                    'side': side,
                    'contracts': cnt,
                    'partial_tp_done': tracked.get('partial_tp_done', False)
                }
                save_positions_tracking(tracking)
                
                # 检查追踪止损
                triggered, stop_price = check_trailing_stop(entry, price, highest, lowest, side, TRAILING_STOP_PCT)
                
                if triggered:
                    print(f"追踪止损触发! 止盈: {stop_price:.2f}")
                    close_position(ex, pair, side, cnt, posSide=side)
                    send_discord(f"🔴 追踪止损 {pair} 止盈: {format_price(price)}")
                    has_activity = True
                elif pnl_pct_leveraged <= -STOP_LOSS_PCT*100:
                    close_position(ex, pair, side, cnt, posSide=side)
                    send_discord(f"🔴 止损 {pair} {pnl_pct:.1f}%")
                    has_activity = True
                elif pnl_pct_leveraged >= TAKE_PROFIT_PCT*100:
                    close_position(ex, pair, side, cnt, posSide=side)
                    send_discord(f"🟢 止盈 {pair} {pnl_pct:.1f}%")
                    has_activity = True
                continue
            
            # 检查仓位限制 (按实际占用保证金计算)
            current_value = 0
            for p in pos.keys():
                try:
                    tk = ex.fetch_ticker(p)
                    pos_price = tk['last']
                    current_value += pos[p]['contracts'] * pos_price / LEVERAGE
                except:
                    current_value += pos[p]['contracts'] * pos[p]['entry'] / LEVERAGE
            
            # 如果超过仓位限制，不开新仓
            if current_value >= usdt * MAX_EXPOSURE:
                print(f"已达最大仓位 {MAX_EXPOSURE*100}% (当前: {current_value/usdt*100:.1f}%)")
                continue
            
            # 检查价格变动幅度
            tracked = tracking.get(pair, {})
            last_price = tracked.get('last_price')
            if last_price and MIN_PRICE_CHANGE > 0:
                price_change = abs(price - last_price) / last_price
                if price_change < MIN_PRICE_CHANGE:
                    print(f"价格变动{price_change*100:.2f}% < {MIN_PRICE_CHANGE*100}%，跳过")
                    continue
                print(f"价格变动: {price_change*100:.2f}%")
            
            # 趋势确认
            if TREND_CONFIRMATION:
                macd = df['MACD'].iloc[-1]
                macd_s = df['MACD_signal'].iloc[-1]
                if final_signal == 1 and macd < macd_s:
                    print("趋势向下，跳过多单")
                    continue
                if final_signal == -1 and macd > macd_s:
                    print("趋势向上，跳过空单")
                    continue
                print("趋势确认通过")
            
            # 开仓
            if final_signal == 1:
                # 波动率调整仓位
                adjusted_position_size = POSITION_SIZE
                if VOLATILITY_ADJUST:
                    adjusted_position_size = calculate_volatility_position(price)
                    print(f"调整后仓位: {adjusted_position_size*100:.0f}%")
                
                contracts = usdt * adjusted_position_size * LEVERAGE / price
                contracts = max(0.01, round(contracts, 2))
                
                if open_position(ex, pair, 'long', contracts, posSide='long'):
                    log_trade('OPEN_LONG', pair, price, contracts, note='开多')
                    tracking[pair] = {'highest': price, 'lowest': price, 'entry': price, 'side': 'long', 'contracts': contracts, 'partial_tp_done': False, 'last_price': price}
                    save_positions_tracking(tracking)
                    send_discord(f"""🟢 开多 {pair}

💰 价格: {format_price(price)}
📊 数量: {contracts:.2f}张
🤖 ML概率: {str(round(ml_prob*100, 1)) + '%' if ml_result else 'N/A'}
⏰ {datetime.now().strftime('%H:%M')}""")
                    has_activity = True
                    
            elif final_signal == -1:
                # 波动率调整仓位
                adjusted_position_size = POSITION_SIZE
                if VOLATILITY_ADJUST:
                    adjusted_position_size = calculate_volatility_position(price)
                    print(f"调整后仓位: {adjusted_position_size*100:.0f}%")
                
                contracts = usdt * adjusted_position_size * LEVERAGE / price
                contracts = max(0.01, round(contracts, 2))
                
                if open_position(ex, pair, 'short', contracts, posSide='short'):
                    log_trade('OPEN_SHORT', pair, price, contracts, note='开空')
                    tracking[pair] = {'highest': price, 'lowest': price, 'entry': price, 'side': 'short', 'contracts': contracts, 'partial_tp_done': False, 'last_price': price}
                    save_positions_tracking(tracking)
                    send_discord(f"""🔴 开空 {pair}

💰 价格: {format_price(price)}
📊 数量: {contracts:.2f}张
🤖 ML概率: {str(round(ml_prob*100, 1)) + '%' if ml_result else 'N/A'}
⏰ {datetime.now().strftime('%H:%M')}""")
                    has_activity = True
            
        except Exception as e:
            print(f"错误: {e}")
    
    if not has_activity:
        print("\n无交易")


if __name__ == '__main__':
    main()
