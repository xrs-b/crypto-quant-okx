"""
交易执行模块 - 增强版
"""
import time
from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
from core.config import Config
from core.exchange import Exchange
from core.database import Database
from core.logger import trade_logger
from analytics.recommendation import get_recommendation_provider


class TradingExecutor:
    """交易执行器 - 增强版"""
    
    def __init__(self, config: Config, exchange: Exchange, db: Database):
        self.config = config
        self.exchange = exchange
        self.db = db
        self.trading_config = config.get('trading', {})
        self._trade_cache = {}  # 交易缓存
        # MFE/MAE 建议提供者
        self._recommendation_provider = get_recommendation_provider(db, config)

    def _exchange_has_position(self, symbol: str, side: str) -> bool:
        try:
            positions = self.exchange.fetch_positions()
        except Exception:
            return True
        for pos in positions or []:
            pos_symbol = pos.get('symbol') or pos.get('info', {}).get('instId') or ''
            if pos_symbol and ':' in pos_symbol:
                pos_symbol = pos_symbol.split(':')[0]
            pos_side = str(pos.get('side') or pos.get('info', {}).get('posSide') or '').lower()
            if pos_side in {'buy', 'long'}:
                pos_side = 'long'
            elif pos_side in {'sell', 'short'}:
                pos_side = 'short'
            contracts = float(pos.get('contracts', 0) or 0)
            if pos_symbol == symbol and pos_side == side and contracts > 0:
                return True
        return False

    def _close_local_position_as_stale(self, symbol: str, side: str, close_price: float, reason: str) -> bool:
        trade = self.db.get_latest_open_trade(symbol, side)
        trade_id = trade.get('id') if trade else None
        if trade_id:
            self.db.mark_trade_stale_closed(trade_id, reason, close_price=close_price)
        self.db.close_position(symbol)
        trade_logger.warning(f"{symbol}: 检测到交易所已无对应仓位，自动收口本地持仓/交易")
        return True
    
    def open_position(self, symbol: str, side: str, 
                    current_price: float, signal_id: int = None) -> Optional[int]:
        """开仓"""
        
        # 检查交易冷却
        if not self._check_cooldown(symbol):
            trade_logger.warning(f"{symbol}: 交易冷却中")
            return None
        
        # 获取余额
        try:
            balance = self.exchange.fetch_balance()
            available = balance.get('free', {}).get('USDT', 0)
        except Exception as e:
            trade_logger.error(f"获取余额失败: {e}")
            return None
        
        if available < 100:
            trade_logger.warning(f"余额不足: {available}")
            return None
        
        # 计算开仓数量 - 修复：基于实际杠杆计算，确保保证金占比准确
        # 步骤1: 先设置杠杆到交易所（确保一致）
        configured_leverage = self.trading_config.get('leverage', 10)
        try:
            if self.exchange.is_futures_symbol(symbol):
                self.exchange.set_leverage(symbol, configured_leverage)
        except Exception as e:
            trade_logger.warning(f"{symbol}: 设置杠杆失败，将使用配置值 - {e}")
        
        # 步骤2: 获取实际杠杆（交易所可能调整）
        effective_leverage = self.exchange.get_actual_leverage(symbol) if hasattr(self.exchange, 'get_actual_leverage') else configured_leverage
        
        # 步骤3: 按目标保证金计算名义价值
        # 目标: 10% 保证金 = available * position_ratio
        # 名义价值 = 保证金 * 实际杠杆
        position_ratio = self.trading_config.get('position_size', 0.1)
        target_margin = available * position_ratio  # 目标保证金 (e.g., 1000 USDT)
        desired_notional = target_margin * effective_leverage  # 名义价值 (e.g., 1000 * 10 = 10000 USDT)
        
        # 可观察性日志
        trade_logger.info(
            f"{symbol}: 仓位计算 - 配置杠杆:{configured_leverage}x, 实际杠杆:{effective_leverage}x, "
            f"目标保证金:{target_margin:.2f}USDT({position_ratio*100:.0f}%), 目标名义:{desired_notional:.2f}USDT"
        )
        
        try:
            if not self.exchange.is_futures_symbol(symbol):
                trade_logger.warning(f"{symbol}: 非U本位合约，跳过")
                return None
            amount = self.exchange.normalize_contract_amount(symbol, desired_notional, current_price)
            contract_size = self.exchange.get_contract_size(symbol) if hasattr(self.exchange, 'get_contract_size') else 1.0
            coin_quantity = self.exchange.contracts_to_coin_quantity(symbol, amount) if hasattr(self.exchange, 'contracts_to_coin_quantity') else amount * contract_size
            
            # 验证：计算实际保证金占用
            estimated_margin = (coin_quantity * current_price) / effective_leverage if effective_leverage > 0 else desired_notional
            actual_margin_ratio = estimated_margin / available if available > 0 else 0
            trade_logger.info(
                f"{symbol}: 预估保证金 {estimated_margin:.2f}USDT ({actual_margin_ratio*100:.1f}% of balance), "
                f"合约数:{amount}, 币数量:{coin_quantity:.4f}"
            )
        except Exception as e:
            trade_logger.error(f"计算下单数量失败: {e}")
            return None
        
        # 重试机制
        max_retries = 3
        retry_delay = 2
        
        for attempt in range(max_retries):
            try:
                # 开仓
                order = self.exchange.create_order(
                    symbol, 
                    'buy' if side == 'long' else 'sell', 
                    amount,
                    posSide=side
                )
                
                # 记录交易
                trade_id = self.db.record_trade(
                    symbol=symbol,
                    side=side,
                    entry_price=current_price,
                    quantity=amount,
                    contract_size=contract_size,
                    coin_quantity=coin_quantity,
                    leverage=effective_leverage,
                    signal_id=signal_id,
                    notes=f"开仓尝试 #{attempt + 1}"
                )
                
                # 更新持仓
                self.db.update_position(
                    symbol=symbol,
                    side=side,
                    entry_price=current_price,
                    quantity=amount,
                    contract_size=contract_size,
                    coin_quantity=coin_quantity,
                    leverage=effective_leverage,
                    current_price=current_price
                )
                
                # 更新冷却时间
                self._update_cooldown(symbol)
                self._seed_trailing_anchor(symbol, side, current_price)
                
                trade_logger.trade(
                    symbol, side, current_price, amount, trade_id
                )
                
                return trade_id
                
            except Exception as e:
                trade_logger.error(f"开仓失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if '51202' in str(e):
                    amount = round(amount * 0.5, 8)
                    trade_logger.warning(f"{symbol}: 市价单数量超过上限，自动缩量后重试 -> {amount}")
                    if amount <= 0:
                        return None
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    return None
    
    def close_position(self, symbol: str, reason: str = 'manual',
                     close_price: float = None, close_quantity: float = None) -> bool:
        """平仓 - U本位合约
        
        Args:
            symbol: 交易对
            reason: 平仓原因
            close_price: 平仓价格（默认市价）
            close_quantity: 平仓数量（合约张数），默认全部平仓
        """
        
        # 获取持仓
        positions = self.db.get_positions()
        position = None
        for p in positions:
            if p['symbol'] == symbol:
                position = p
                break
        
        if not position:
            trade_logger.warning(f"无持仓: {symbol}")
            return False
        
        side = position['side']  # 'long' or 'short'
        # 支持部分平仓：使用指定数量或全部
        quantity = close_quantity if close_quantity is not None else position['quantity']
        coin_quantity = float(position.get('coin_quantity', 0) or 0)
        contract_size = float(position.get('contract_size', 1) or 1)
        entry_price = position['entry_price']
        
        # 获取当前价格
        if close_price is None:
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                close_price = ticker['last']
            except Exception as e:
                trade_logger.error(f"获取价格失败: {e}")
                return False
        
        # 重试机制
        max_retries = 3
        for attempt in range(max_retries):
            try:
                # U本位平仓 - 通过创建反向订单平仓
                # 多仓平空，空仓平多
                close_side = 'sell' if side == 'long' else 'buy'
                
                self.exchange.close_order(
                    symbol, 
                    close_side,
                    quantity,
                    posSide=side
                )

                trade = self.db.get_latest_open_trade(symbol, side)
                exchange_close = self.exchange.fetch_closed_trade_summary(trade, fallback_price=close_price) if trade else None
                if exchange_close and exchange_close.get('exit_price'):
                    close_price = exchange_close['exit_price']
                
                # 计算部分平仓的币数量
                is_partial = close_quantity is not None and close_quantity < position['quantity']
                if is_partial:
                    # 部分平仓：按比例计算币数量
                    close_ratio = close_quantity / position['quantity']
                    closed_coin_quantity = coin_quantity * close_ratio
                    remaining_coin_quantity = coin_quantity - closed_coin_quantity
                else:
                    closed_coin_quantity = coin_quantity
                    remaining_coin_quantity = 0
                
                # 计算盈亏（基于实际平仓的币数量）
                if side == 'long':
                    pnl = (close_price - entry_price) * closed_coin_quantity
                    pnl_percent = (close_price - entry_price) / entry_price * 100
                else:
                    pnl = (entry_price - close_price) * closed_coin_quantity
                    pnl_percent = (entry_price - close_price) / entry_price * 100
                
                # 杠杆后盈亏
                leverage = position.get('leverage', 1)
                leveraged_pnl_percent = pnl_percent * leverage
                
                # 更新交易记录（positions.id ≠ trades.id，需回查最新 open trade）
                trade_id = trade.get('id') if trade else None
                if trade_id:
                    close_note = f"平仓原因: {reason}"
                    if is_partial:
                        close_note += f" | 部分平仓({close_ratio*100:.0f}%)"
                    if exchange_close:
                        self.db.reconcile_trade_close(trade_id, exchange_close, reason=close_note)
                    else:
                        self.db.close_trade(
                            trade_id=trade_id,
                            exit_price=close_price,
                            pnl=pnl,
                            pnl_percent=leveraged_pnl_percent,
                            notes=close_note,
                            close_source='local_market_close'
                        )
                else:
                    trade_logger.warning(f"{symbol}: 未找到可关闭的 open trade 记录，持仓会先从本地移除")
                
                # 部分平仓：更新持仓；全部平仓：删除持仓
                if is_partial:
                    self.db.update_position(
                        symbol=symbol,
                        side=side,
                        entry_price=entry_price,
                        quantity=position['quantity'] - close_quantity,
                        leverage=leverage,
                        current_price=close_price,
                        peak_price=position.get('peak_price'),
                        trough_price=position.get('trough_price'),
                        contract_size=contract_size,
                        coin_quantity=remaining_coin_quantity
                    )
                    trade_logger.info(f"{symbol}: 部分平仓完成，剩余{(position['quantity'] - close_quantity):.4f}张")
                else:
                    self.db.close_position(symbol)
                    # 只有全部平仓才清除缓存
                    self._update_cooldown(symbol)
                    self._clear_trade_cache(symbol)
                
                trade_logger.close(symbol, close_price, pnl, reason)
                
                return True
                
            except Exception as e:
                message = str(e)
                trade_logger.error(f"平仓失败 (尝试 {attempt + 1}/{max_retries}): {e}")
                if '51169' in message and not self._exchange_has_position(symbol, side):
                    self._clear_trade_cache(symbol)
                    return self._close_local_position_as_stale(symbol, side, close_price, f'{reason} | 交易所已无对应仓位')
                if attempt < max_retries - 1:
                    time.sleep(1)
                else:
                    return False
        
        return False
    
    def check_stop_loss(self, symbol: str, current_price: float) -> bool:
        """检查止损"""
        
        # 参数校验
        if not current_price or current_price <= 0:
            return False
        
        positions = self.db.get_positions()
        position = None
        for p in positions:
            if p.get('symbol') == symbol:
                position = p
                break
        
        if not position:
            return False
        
        # 防御性获取必要字段，避免 KeyError
        side = position.get('side')
        entry_price = position.get('entry_price')
        
        if not side or entry_price is None or entry_price <= 0:
            trade_logger.warning(f"{symbol}: 持仓数据不完整 (side={side}, entry_price={entry_price})")
            return False
        
        leverage = position.get('leverage', 1) or 1
        # 优先使用 MFE/MAE 建议，回退到配置默认值
        stop_loss = self._recommendation_provider.get_stop_loss(symbol)
        
        # 计算盈亏比例
        try:
            if side == 'long':
                pnl_percent = (current_price - entry_price) / entry_price
            else:
                pnl_percent = (entry_price - current_price) / entry_price
        except (TypeError, ZeroDivisionError):
            trade_logger.error(f"{symbol}: 止损计算失败 (entry_price={entry_price})")
            return False
        
        # 杠杆后盈亏
        leveraged_pnl = pnl_percent * leverage
        
        if leveraged_pnl <= -stop_loss:
            trade_logger.info(f"触发止损: {symbol} 亏损{leveraged_pnl*100:.2f}%")
            return True
        
        return False
    
    def check_take_profit(self, symbol: str, current_price: float,
                         highest_price: float = None) -> bool:
        """检查止盈/追踪止损"""
        
        # 参数校验
        if not current_price or current_price <= 0:
            return False
        
        positions = self.db.get_positions()
        position = None
        for p in positions:
            if p.get('symbol') == symbol:
                position = p
                break
        
        if not position:
            self._clear_trade_cache(symbol)
            return False
        
        # 防御性获取必要字段，避免 KeyError
        side = position.get('side')
        entry_price = position.get('entry_price')
        
        if not side or entry_price is None or entry_price <= 0:
            trade_logger.warning(f"{symbol}: 持仓数据不完整 (side={side}, entry_price={entry_price})")
            self._clear_trade_cache(symbol)
            return False
        
        leverage = position.get('leverage', 1) or 1
        
        # 追踪止损 - 优先使用配置值（测试友好），其次 MFE/MAE 建议
        ts_params = self._recommendation_provider.get_trailing_stop(symbol)
        
        # 追踪距离：优先 config，其次 recommendation
        config_ts = self.trading_config.get('trailing_stop')
        rec_ts = ts_params.get('distance')
        trailing_stop = config_ts if config_ts is not None else rec_ts
        
        # 盈利触发型追踪止损：
        # - 优先 config（可设为 None 表示旧行为：始终激活）
        # - 其次 recommendation
        config_ta = self.trading_config.get('trailing_activation')
        rec_ta = ts_params.get('activation')
        
        if config_ta is not None:
            trailing_activation = config_ta
        elif rec_ta is not None:
            trailing_activation = rec_ta
        else:
            trailing_activation = 0.01  # 默认 1% 盈利激活
        
        # 计算当前盈利比例（杠杆前）
        try:
            if side == 'long':
                pnl_percent = (current_price - entry_price) / entry_price
            else:
                pnl_percent = (entry_price - current_price) / entry_price
        except (TypeError, ZeroDivisionError):
            pnl_percent = 0
        
        # 检查是否已达到激活阈值（None 表示始终激活，作为安全回退）
        # 一旦激活（trailing_armed），就保持激活状态
        cache = self._trade_cache.setdefault(symbol, {})
        already_armed = cache.get('trailing_armed', False)
        trailing_activated = already_armed or (trailing_activation is None or pnl_percent >= trailing_activation)
        
        if side == 'long':
            anchor = highest_price if highest_price is not None else cache.get('highest_price', position.get('peak_price') or entry_price)
            anchor = max(float(anchor or entry_price), float(position.get('peak_price') or entry_price), float(current_price or entry_price))
            cache['highest_price'] = anchor
            self.db.update_position(symbol, side, entry_price, position.get('quantity', 0), leverage, current_price, peak_price=anchor, trough_price=position.get('trough_price'), contract_size=position.get('contract_size', 1) or 1, coin_quantity=position.get('coin_quantity'))
            
            # 盈利触发型追踪：只有激活后且价格回落才触发
            if trailing_activated:
                stop_price = anchor * (1 - trailing_stop)
                if current_price <= stop_price and current_price > entry_price:
                    try:
                        leveraged_pnl = pnl_percent * leverage
                    except (TypeError, ZeroDivisionError):
                        leveraged_pnl = 0
                    trade_logger.info(f"触发追踪止损: {symbol} 盈利{leveraged_pnl*100:.2f}%")
                    return True
            elif cache.get('trailing_armed'):
                # 记录首次激活
                trade_logger.info(f"{symbol}: 追踪止损已激活 (盈利{pnl_percent*100:.2f}%, 阈值{trailing_activation*100:.2f}%)")
            cache['trailing_armed'] = trailing_activated
        else:
            anchor = cache.get('lowest_price', position.get('trough_price') or entry_price)
            anchor = min(float(anchor or entry_price), float(position.get('trough_price') or entry_price), float(current_price or entry_price))
            cache['lowest_price'] = anchor
            self.db.update_position(symbol, side, entry_price, position.get('quantity', 0), leverage, current_price, peak_price=position.get('peak_price'), trough_price=anchor, contract_size=position.get('contract_size', 1) or 1, coin_quantity=position.get('coin_quantity'))
            
            # 盈利触发型追踪：只有激活后且价格回升才触发
            if trailing_activated:
                stop_price = anchor * (1 + trailing_stop)
                if current_price >= stop_price and current_price < entry_price:
                    try:
                        leveraged_pnl = pnl_percent * leverage
                    except (TypeError, ZeroDivisionError):
                        leveraged_pnl = 0
                    trade_logger.info(f"触发追踪止损: {symbol} 盈利{leveraged_pnl*100:.2f}%")
                    return True
            elif cache.get('trailing_armed'):
                trade_logger.info(f"{symbol}: 追踪止损已激活 (盈利{pnl_percent*100:.2f}%, 阈值{trailing_activation*100:.2f}%)")
            cache['trailing_armed'] = trailing_activated
        
        # 普通止盈 - 优先使用配置值，其次 MFE/MAE 建议
        config_tp = self.trading_config.get('take_profit')
        rec_tp = self._recommendation_provider.get_take_profit(symbol)
        take_profit = rec_tp if config_tp is None else config_tp
        
        try:
            if side == 'long':
                pnl_percent = (current_price - entry_price) / entry_price
            else:
                pnl_percent = (entry_price - current_price) / entry_price
        except (TypeError, ZeroDivisionError):
            trade_logger.error(f"{symbol}: 止盈计算失败 (entry_price={entry_price})")
            return False
        
        leveraged_pnl = pnl_percent * leverage
        
        # 检查部分止盈（第一止盈层）
        partial_tp = self._check_partial_take_profit(symbol, leveraged_pnl, position, current_price)
        if partial_tp:
            # 第一止盈层已触发，检查是否需要检查第二止盈层
            partial_tp2 = self._check_partial_take_profit2(symbol, leveraged_pnl, current_price)
            if partial_tp2:
                # 第二止盈层也触发（在同一周期内不可能，因为会先平第一层）
                return True
            # 第一止盈层已执行，但未触发第二止盈层，检查是否继续给 trailing/full TP 机会
            # 返回 False 让调用方继续检查其他退出条件
        
        # 检查部分止盈（第二止盈层）- 只有第一止盈层已执行或未配置时才检查
        partial_tp2 = self._check_partial_take_profit2(symbol, leveraged_pnl, current_price)
        if partial_tp2:
            return True
        
        # 普通止盈 - 优先使用配置值，其次 MFE/MAE 建议
        if leveraged_pnl >= take_profit:
            trade_logger.info(f"触发止盈: {symbol} 盈利{leveraged_pnl*100:.2f}%")
            return True
        
        return False
    
    def _check_partial_take_profit(self, symbol: str, leveraged_pnl: float, 
                                    position: Dict, current_price: float) -> bool:
        """检查部分止盈（第一止盈层）
        
        Returns:
            True if partial TP was executed, False otherwise
        """
        # 读取分批止盈配置（带安全回退）
        partial_tp_enabled = self.trading_config.get('partial_tp_enabled', False)
        if not partial_tp_enabled:
            return False
        
        # 获取阈值和比例（带默认值）
        partial_tp_threshold = self.trading_config.get('partial_tp_threshold', 0.015)  # 默认 1.5%
        partial_tp_ratio = self.trading_config.get('partial_tp_ratio', 0.5)  # 默认平 50%
        
        # 检查是否已达到部分止盈阈值
        if leveraged_pnl >= partial_tp_threshold:
            # 检查是否已经执行过部分止盈（避免重复）
            cache = self._trade_cache.setdefault(symbol, {})
            if cache.get('partial_tp_executed', False):
                return False
            
            # 计算部分平仓数量
            full_quantity = position.get('quantity', 0)
            close_quantity = full_quantity * partial_tp_ratio
            close_quantity = round(close_quantity, 4)
            
            if close_quantity <= 0:
                return False
            
            # 标记为已执行
            cache['partial_tp_executed'] = True
            
            # 执行部分平仓
            trade_logger.info(
                f"{symbol}: 触发部分止盈 (盈利{leveraged_pnl*100:.2f}%, 阈值{partial_tp_threshold*100:.1f}%, "
                f"平{partial_tp_ratio*100:.0f}%仓位={close_quantity}张)"
            )
            
            # 获取 trade_id 用于记录 partial TP 历史
            trade = self.db.get_latest_open_trade(symbol, position.get('side'))
            trade_id = trade.get('id') if trade else None
            
            # 计算部分平仓盈亏
            entry_price = position.get('entry_price', 0)
            side = position.get('side', 'long')
            coin_quantity = position.get('coin_quantity', 0) or 0
            contract_size = position.get('contract_size', 1) or 1
            close_coin_quantity = coin_quantity * partial_tp_ratio
            if side == 'long':
                pnl = (current_price - entry_price) * close_coin_quantity
            else:
                pnl = (entry_price - current_price) * close_coin_quantity
            
            # 执行平仓
            result = self.close_position(
                symbol=symbol,
                reason='partial_tp',
                close_price=current_price,
                close_quantity=close_quantity
            )
            
            # 记录 partial TP 触发历史
            if result:
                self.db.record_partial_tp(
                    trade_id=trade_id,
                    symbol=symbol,
                    side=side,
                    trigger_price=current_price,
                    close_ratio=partial_tp_ratio,
                    close_quantity=close_quantity,
                    pnl=pnl,
                    note=f"触发阈值:{partial_tp_threshold*100:.1f}%"
                )
            
            return result
        
        return False
    
    def _check_partial_take_profit2(self, symbol: str, leveraged_pnl: float,
                                     current_price: float) -> bool:
        """检查部分止盈（第二止盈层 / 多级退出）
        
        在第一止盈层执行后，当盈利继续扩大时触发。
        
        Returns:
            True if partial TP2 was executed, False otherwise
        """
        # 读取第二止盈层配置（带安全回退）
        partial_tp2_enabled = self.trading_config.get('partial_tp2_enabled', False)
        if not partial_tp2_enabled:
            return False
        
        # 获取阈值和比例（带默认值）
        partial_tp2_threshold = self.trading_config.get('partial_tp2_threshold', 0.03)  # 默认 3%
        partial_tp2_ratio = self.trading_config.get('partial_tp2_ratio', 0.3)  # 默认平 30%
        
        # 检查是否已达到第二止盈阈值
        if leveraged_pnl >= partial_tp2_threshold:
            # 检查是否已经执行过第二止盈（避免重复）
            cache = self._trade_cache.setdefault(symbol, {})
            if cache.get('partial_tp2_executed', False):
                return False
            
            # 获取当前持仓（第一止盈层执行后可能有剩余）
            positions = self.db.get_positions()
            position = None
            for p in positions:
                if p.get('symbol') == symbol:
                    position = p
                    break
            
            if not position:
                return False
            
            # 计算部分平仓数量
            full_quantity = position.get('quantity', 0)
            close_quantity = full_quantity * partial_tp2_ratio
            close_quantity = round(close_quantity, 4)
            
            if close_quantity <= 0:
                return False
            
            # 标记为已执行
            cache['partial_tp2_executed'] = True
            
            # 执行部分平仓
            trade_logger.info(
                f"{symbol}: 触发第二止盈层 (盈利{leveraged_pnl*100:.2f}%, 阈值{partial_tp2_threshold*100:.1f}%, "
                f"平{partial_tp2_ratio*100:.0f}%仓位={close_quantity}张)"
            )
            
            # 获取 trade_id 用于记录 partial TP 历史
            trade = self.db.get_latest_open_trade(symbol, position.get('side'))
            trade_id = trade.get('id') if trade else None
            
            # 计算部分平仓盈亏
            entry_price = position.get('entry_price', 0)
            side = position.get('side', 'long')
            coin_quantity = position.get('coin_quantity', 0) or 0
            close_coin_quantity = coin_quantity * partial_tp2_ratio
            if side == 'long':
                pnl = (current_price - entry_price) * close_coin_quantity
            else:
                pnl = (entry_price - current_price) * close_coin_quantity
            
            # 执行平仓
            result = self.close_position(
                symbol=symbol,
                reason='partial_tp2',
                close_price=current_price,
                close_quantity=close_quantity
            )
            
            # 记录 partial TP 触发历史
            if result:
                self.db.record_partial_tp(
                    trade_id=trade_id,
                    symbol=symbol,
                    side=side,
                    trigger_price=current_price,
                    close_ratio=partial_tp2_ratio,
                    close_quantity=close_quantity,
                    pnl=pnl,
                    note=f"第二止盈层 | 阈值:{partial_tp2_threshold*100:.1f}%"
                )
            
            return result
        
        return False
    
    def update_positions(self) -> Dict[str, Any]:
        """更新所有持仓状态"""
        positions = self.db.get_positions()
        updated = {}
        
        for position in positions:
            symbol = position['symbol']
            try:
                ticker = self.exchange.fetch_ticker(symbol)
                current_price = ticker['last']
                
                # 更新持仓
                self.db.update_position(
                    symbol=symbol,
                    side=position['side'],
                    entry_price=position['entry_price'],
                    quantity=position['quantity'],
                    contract_size=position.get('contract_size', 1),
                    coin_quantity=position.get('coin_quantity'),
                    leverage=position['leverage'],
                    current_price=current_price
                )
                
                updated[symbol] = {
                    'current_price': current_price,
                    'updated': True
                }
                
            except Exception as e:
                trade_logger.error(f"更新{symbol}持仓失败: {e}")
                updated[symbol] = {'error': str(e)}
        
        return updated
    
    def get_portfolio_status(self) -> Dict[str, Any]:
        """获取投资组合状态"""
        positions = self.db.get_positions()
        
        total_pnl = 0
        total_value = 0
        
        for p in positions:
            unrealized_pnl = p.get('unrealized_pnl', 0)
            value = p.get('coin_quantity', 0) * p.get('current_price', 0)
            total_pnl += unrealized_pnl
            total_value += value
        
        # 获取交易统计
        trade_stats = self.db.get_trade_stats(days=30)
        
        return {
            'total_positions': len(positions),
            'total_value': total_value,
            'unrealized_pnl': total_pnl,
            'trade_stats': trade_stats,
            'positions': positions
        }
    
    def _check_cooldown(self, symbol: str) -> bool:
        """检查交易冷却（优先数据库，避免守护跨周期失效）"""
        cooldown_minutes = self.trading_config.get('cooldown_minutes', 15)
        last_trade = self.db.get_latest_trade_time(symbol)
        if not last_trade and symbol in self._trade_cache:
            last_trade = self._trade_cache[symbol].get('last_trade')
        if last_trade:
            diff_minutes = (datetime.utcnow() - last_trade).total_seconds() / 60
            if diff_minutes < cooldown_minutes:
                return False
        return True
    
    def _update_cooldown(self, symbol: str):
        """更新冷却时间"""
        if symbol not in self._trade_cache:
            self._trade_cache[symbol] = {}
        self._trade_cache[symbol]['last_trade'] = datetime.now()

    def _seed_trailing_anchor(self, symbol: str, side: str, price: float):
        if symbol not in self._trade_cache:
            self._trade_cache[symbol] = {}
        # 清除部分止盈标记（新仓位重新开始）
        self._trade_cache[symbol].pop('partial_tp_executed', None)
        self._trade_cache[symbol].pop('partial_tp2_executed', None)
        if side == 'long':
            self._trade_cache[symbol]['highest_price'] = price
        else:
            self._trade_cache[symbol]['lowest_price'] = price

    def _clear_trade_cache(self, symbol: str):
        self._trade_cache.pop(symbol, None)


class RiskManager:
    """风险管理器"""

    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self.trading_config = config.get('trading', {})
        self._exchange = None

    def _loss_guard_enabled(self) -> bool:
        return bool(self.trading_config.get('loss_streak_lock_enabled', True))

    def _loss_guard_hours(self) -> int:
        return int(self.trading_config.get('loss_streak_cooldown_hours', 12) or 12)

    def _sync_loss_streak_guard(self) -> Dict[str, Any]:
        state = self.db.get_risk_guard_state('loss_streak')
        now = datetime.now()
        changed = False
        just_triggered = False
        auto_recovered = False

        trades = self.db.get_trades(status='closed', limit=200)
        new_trades = [t for t in reversed(trades) if int(t.get('id', 0) or 0) > int(state.get('last_trade_id', 0) or 0)]
        for trade in new_trades:
            state['last_trade_id'] = int(trade.get('id', 0) or state.get('last_trade_id', 0) or 0)
            pnl = trade.get('pnl')
            if pnl is None:
                changed = True
                continue
            pnl_value = float(pnl or 0)
            if pnl_value < 0:
                state['current_streak'] = int(state.get('current_streak', 0) or 0) + 1
                state.setdefault('details', {})['last_loss_at'] = trade.get('close_time') or trade.get('open_time')
            else:
                state['current_streak'] = 0
                state.setdefault('details', {})['last_win_at'] = trade.get('close_time') or trade.get('open_time')
                if state.get('lock_active'):
                    state['lock_active'] = 0
                    state['lock_until'] = None
                    state['triggered_at'] = None
            changed = True

        lock_until = state.get('lock_until')
        if state.get('lock_active') and lock_until:
            try:
                lock_dt = datetime.fromisoformat(str(lock_until))
            except Exception:
                lock_dt = None
            if lock_dt and now >= lock_dt:
                state['lock_active'] = 0
                state['lock_until'] = None
                state['triggered_at'] = None
                state['current_streak'] = 0
                state['reset_at'] = now.isoformat()
                auto_recovered = True
                changed = True

        max_consecutive_losses = int(self.trading_config.get('max_consecutive_losses', 3))
        if self._loss_guard_enabled() and not state.get('lock_active') and int(state.get('current_streak', 0) or 0) >= max_consecutive_losses:
            state['lock_active'] = 1
            state['triggered_at'] = now.isoformat()
            state['lock_until'] = (now + timedelta(hours=self._loss_guard_hours())).isoformat()
            state.setdefault('details', {})['max_consecutive_losses'] = max_consecutive_losses
            just_triggered = True
            changed = True

        if changed:
            self.db.save_risk_guard_state(state)
        state['just_triggered'] = just_triggered
        state['auto_recovered'] = auto_recovered
        return state

    def manual_reset_loss_streak(self, note: str = None) -> Dict[str, Any]:
        """手动清零连亏熔断状态，支持幂等调用"""
        state = self.db.get_risk_guard_state('loss_streak')
        
        # Idempotency: if already unlocked, return current state without re-recording
        already_unlocked = not bool(state.get('lock_active', 0))
        if already_unlocked:
            return {
                **state,
                'idempotent': True,
                'message': 'already_unlocked',
                'action': 'no_change'
            }
        
        # Perform reset
        state['current_streak'] = 0
        state['lock_active'] = 0
        state['lock_until'] = None
        state['triggered_at'] = None
        state['reset_at'] = datetime.now().isoformat()
        details = state.get('details', {}) or {}
        if note:
            details['manual_reset_note'] = note
        state['details'] = details
        self.db.save_risk_guard_state(state)
        
        return {
            **state,
            'idempotent': False,
            'message': 'reset_completed',
            'action': 'reset'
        }

    def can_open_position(self, symbol: str) -> tuple:
        """检查是否可以开仓"""
        details = {}

        max_trades = int(self.trading_config.get('max_trades_per_day', 10))
        today_trades = self._get_today_trade_count()
        if today_trades >= max_trades:
            details['daily_limit'] = {'passed': False, 'reason': f'已达每日交易上限({today_trades})'}
            return False, f"已达每日交易上限({today_trades}/{max_trades})", details
        details['daily_limit'] = {'passed': True, 'count': today_trades, 'max': max_trades}

        min_interval = int(self.trading_config.get('min_trade_interval', 300))
        last_trade = self._get_last_trade_time()
        if last_trade:
            diff_seconds = (datetime.utcnow() - last_trade).total_seconds()
            if diff_seconds < min_interval:
                details['global_cooldown'] = {'passed': False, 'remaining': int(min_interval - diff_seconds)}
                return False, f"全局冷却中({int(diff_seconds)}s)", details
        details['global_cooldown'] = {'passed': True}

        max_consecutive_losses = int(self.trading_config.get('max_consecutive_losses', 3))
        loss_guard = self._sync_loss_streak_guard()
        consecutive_losses = int(loss_guard.get('current_streak', 0) or 0)
        if loss_guard.get('lock_active'):
            details['loss_streak_limit'] = {
                'passed': False,
                'current': consecutive_losses,
                'max': max_consecutive_losses,
                'locked': True,
                'recover_at': loss_guard.get('lock_until'),
                'triggered_at': loss_guard.get('triggered_at'),
                'cooldown_hours': self._loss_guard_hours(),
                'just_triggered': bool(loss_guard.get('just_triggered')),
                'auto_recovered': bool(loss_guard.get('auto_recovered')),
            }
            return False, f"连续亏损熔断冷却中({consecutive_losses}/{max_consecutive_losses})", details
        details['loss_streak_limit'] = {
            'passed': True,
            'current': consecutive_losses,
            'max': max_consecutive_losses,
            'locked': False,
            'recover_at': loss_guard.get('lock_until'),
            'triggered_at': loss_guard.get('triggered_at'),
            'cooldown_hours': self._loss_guard_hours(),
            'just_triggered': bool(loss_guard.get('just_triggered')),
            'auto_recovered': bool(loss_guard.get('auto_recovered')),
        }

        max_daily_drawdown = float(self.trading_config.get('max_daily_drawdown', 0.03))
        daily_drawdown_ratio = self._get_daily_drawdown_ratio()
        if daily_drawdown_ratio >= max_daily_drawdown:
            details['daily_drawdown_limit'] = {
                'passed': False,
                'current': round(daily_drawdown_ratio, 4),
                'max': max_daily_drawdown
            }
            return False, f"日内回撤熔断({daily_drawdown_ratio*100:.2f}%/{max_daily_drawdown*100:.2f}%)", details
        details['daily_drawdown_limit'] = {
            'passed': True,
            'current': round(daily_drawdown_ratio, 4),
            'max': max_daily_drawdown
        }

        max_exposure = float(self.trading_config.get('max_exposure', 0.3))
        position_ratio = float(self.trading_config.get('position_size', 0.1))
        configured_leverage = int(self.trading_config.get('leverage', 10))
        
        # 获取实际杠杆用于更准确的预估
        effective_leverage = configured_leverage
        if self._exchange:
            try:
                effective_leverage = self._exchange.get_actual_leverage(symbol) if hasattr(self._exchange, 'get_actual_leverage') else configured_leverage
            except Exception:
                pass
        
        current_exposure = self._get_current_exposure()
        
        # 使用实际杠杆预估新仓位的保证金占用
        # 目标保证金 = available * position_ratio
        # 这与 executor.py 中的逻辑保持一致
        projected_margin_ratio = position_ratio  # 实际保证金占比就是 position_ratio
        
        projected_exposure = current_exposure + projected_margin_ratio
        
        # 可观察性
        trade_logger.info(
            f"风控检查 {symbol}: 当前暴露:{current_exposure*100:.1f}%, "
            f"计划仓位:{position_ratio*100:.0f}%, 配置杠杆:{configured_leverage}x, 实际杠杆:{effective_leverage}x, "
            f"预计总暴露:{projected_exposure*100:.1f}%, 上限:{max_exposure*100:.0f}%"
        )
        
        if projected_exposure > max_exposure:
            details['exposure_limit'] = {
                'passed': False,
                'current': round(current_exposure, 4),
                'projected': round(projected_exposure, 4),
                'max': max_exposure,
                'planned_leverage': configured_leverage,
                'effective_leverage': effective_leverage,
                'position_ratio': position_ratio
            }
            return False, f"开仓后将超过最大持仓比例({projected_exposure*100:.0f}%)", details
        details['exposure_limit'] = {
            'passed': True,
            'current': round(current_exposure, 4),
            'projected': round(projected_exposure, 4),
            'max': max_exposure,
            'planned_leverage': configured_leverage,
            'effective_leverage': effective_leverage,
            'position_ratio': position_ratio
        }

        return True, None, details

    def get_risk_status(self) -> Dict[str, Any]:
        """供仪表盘显示的风险状态"""
        balance = self._get_balance_summary()
        current_exposure = self._get_current_exposure()
        daily_drawdown = self._get_daily_drawdown_ratio()
        loss_guard = self._sync_loss_streak_guard()
        consecutive_losses = int(loss_guard.get('current_streak', 0) or 0)
        status = 'locked' if loss_guard.get('lock_active') else ('guarded' if (daily_drawdown > 0 or consecutive_losses > 0) else 'normal')
        return {
            'today_trades': self._get_today_trade_count(),
            'last_trade_time': self._get_last_trade_time().isoformat() if self._get_last_trade_time() else None,
            'current_exposure': round(current_exposure, 4),
            'max_exposure': float(self.trading_config.get('max_exposure', 0.3)),
            'position_size': float(self.trading_config.get('position_size', 0.1)),
            'daily_drawdown': round(daily_drawdown, 4),
            'max_daily_drawdown': float(self.trading_config.get('max_daily_drawdown', 0.03)),
            'consecutive_losses': consecutive_losses,
            'max_consecutive_losses': int(self.trading_config.get('max_consecutive_losses', 3)),
            'loss_streak_lock_enabled': self._loss_guard_enabled(),
            'loss_streak_cooldown_hours': self._loss_guard_hours(),
            'loss_streak_locked': bool(loss_guard.get('lock_active')),
            'loss_streak_recover_at': loss_guard.get('lock_until'),
            'loss_streak_triggered_at': loss_guard.get('triggered_at'),
            'balance': balance,
            'status': status
        }

    def _get_balance_summary(self) -> Dict[str, float]:
        try:
            from core.exchange import Exchange
            if self._exchange is None:
                self._exchange = Exchange(self.config.all)
            balance = self._exchange.fetch_balance()
            total = float(balance.get('total', {}).get('USDT', 0) or 0)
            free = float(balance.get('free', {}).get('USDT', 0) or 0)
            used = max(0.0, total - free)
            return {'total': round(total, 2), 'free': round(free, 2), 'used': round(used, 2)}
        except Exception:
            return {'total': 0.0, 'free': 0.0, 'used': 0.0}

    def _parse_trade_time(self, value: str) -> Optional[datetime]:
        if not value:
            return None
        return datetime.fromisoformat(value)

    def _get_today_trade_count(self) -> int:
        trades = self.db.get_trades(limit=1000)
        today = datetime.utcnow().date()
        count = 0
        for trade in trades:
            opened_at = self._parse_trade_time(trade.get('open_time', ''))
            if opened_at and opened_at.date() == today:
                count += 1
        return count

    def _get_last_trade_time(self) -> Optional[datetime]:
        return self.db.get_latest_trade_time()

    def _get_consecutive_losses(self) -> int:
        trades = self.db.get_trades(status='closed', limit=20)
        count = 0
        for trade in trades:
            pnl = float(trade.get('pnl', 0) or 0)
            if pnl < 0:
                count += 1
            else:
                break
        return count

    def _get_daily_drawdown_ratio(self) -> float:
        trades = self.db.get_trades(status='closed', limit=1000)
        today = datetime.utcnow().date()
        today_pnl = 0.0
        for trade in trades:
            realized_at = self._parse_trade_time(trade.get('close_time') or trade.get('open_time') or '')
            if realized_at and realized_at.date() == today:
                today_pnl += float(trade.get('pnl', 0) or 0)
        if today_pnl >= 0:
            return 0.0
        balance_total = self._get_balance_summary().get('total', 0.0) or 1.0
        return abs(today_pnl) / balance_total

    def _get_current_exposure(self) -> float:
        positions = self.db.get_positions()
        total_balance = self._get_balance_summary().get('total', 0.0) or 1.0
        total_margin_used = 0.0
        for p in positions:
            qty = float(p.get('coin_quantity', 0) or 0)
            px = float(p.get('current_price', 0) or p.get('entry_price', 0) or 0)
            lev = max(1, int(p.get('leverage', 1) or 1))
            total_margin_used += (qty * px) / lev if qty and px else 0.0
        return total_margin_used / total_balance if total_balance > 0 else 0.0
