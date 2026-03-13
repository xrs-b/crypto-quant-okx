"""
日志模块
"""
import logging
import os
from datetime import datetime
from pathlib import Path
from logging.handlers import RotatingFileHandler


def setup_logger(name: str = 'okx_trading', level: int = logging.INFO) -> logging.Logger:
    """设置日志器"""
    
    # 创建logger
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # 避免重复添加handler
    if logger.handlers:
        return logger
    
    # 日志格式
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    
    # 控制台handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # 文件handler - 日志目录
    project_root = Path(__file__).parent.parent
    log_dir = project_root / 'logs'
    log_dir.mkdir(exist_ok=True)
    
    # 文件handler - 详细日志
    file_handler = RotatingFileHandler(
        log_dir / 'trading.log',
        maxBytes=10*1024*1024,  # 10MB
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    
    # 错误日志单独文件
    error_handler = RotatingFileHandler(
        log_dir / 'error.log',
        maxBytes=5*1024*1024,  # 5MB
        backupCount=3,
        encoding='utf-8'
    )
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)
    logger.addHandler(error_handler)
    
    return logger


# 全局logger实例
logger = setup_logger()


class TradeLogger:
    """交易专用日志"""
    
    def __init__(self):
        self.logger = logging.getLogger('okx_trading.trade')
    
    def signal(self, symbol: str, signal_type: str, price: float, 
               strength: int, reasons: list, executed: bool = False):
        """记录信号"""
        self.logger.info(
            f"[SIGNAL] {symbol} {signal_type} @ {price:.2f} "
            f"强度:{strength}% 执行:{executed} 原因:{reasons}"
        )
    
    def trade(self, symbol: str, side: str, entry: float, quantity: float,
              trade_id: int = None):
        """记录开仓"""
        self.logger.info(
            f"[TRADE] {'开多' if side == 'long' else '开空'} {symbol} "
            f"@ {entry:.2f} x {quantity:.4f} ID:{trade_id}"
        )
    
    def close(self, symbol: str, exit: float, pnl: float, reason: str):
        """记录平仓"""
        self.logger.info(
            f"[CLOSE] {symbol} @ {exit:.2f} PnL:{pnl:.2f} 原因:{reason}"
        )
    
    def error(self, message: str, exc_info: bool = False):
        """记录错误"""
        self.logger.error(f"[ERROR] {message}", exc_info=exc_info)
    
    def info(self, message: str):
        """记录信息"""
        self.logger.info(f"[INFO] {message}")
    
    def warning(self, message: str):
        """记录警告"""
        self.logger.warning(f"[WARN] {message}")


# 全局交易日志
trade_logger = TradeLogger()
