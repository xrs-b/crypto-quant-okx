"""
数据库模块
"""
import sqlite3
import json
from datetime import datetime
from typing import Dict, List, Optional, Any
from pathlib import Path
import os


class Database:
    """SQLite数据库管理类"""
    
    def __init__(self, db_path: str = None):
        if db_path is None:
            project_root = Path(__file__).parent.parent
            db_path = project_root / "data" / "trading.db"
        
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._init_db()
    
    def _get_connection(self):
        """获取数据库连接"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_db(self):
        """初始化数据库表"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 交易记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                quantity REAL NOT NULL,
                leverage INTEGER NOT NULL,
                pnl REAL,
                pnl_percent REAL,
                status TEXT NOT NULL,
                open_time DATETIME NOT NULL,
                close_time DATETIME,
                reason TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 信号记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                price REAL NOT NULL,
                strength INTEGER NOT NULL,
                reasons TEXT NOT NULL,
                strategies_triggered TEXT,
                filtered BOOLEAN DEFAULT FALSE,
                filter_reason TEXT,
                executed BOOLEAN DEFAULT FALSE,
                executed_at DATETIME,
                trade_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (trade_id) REFERENCES trades(id)
            )
        ''')
        
        # 策略分析记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS strategy_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                strategy_name TEXT NOT NULL,
                strategy_type TEXT NOT NULL,
                triggered BOOLEAN NOT NULL,
                details TEXT,
                confidence REAL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            )
        ''')
        
        # 持仓表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                quantity REAL NOT NULL,
                leverage INTEGER NOT NULL,
                current_price REAL,
                unrealized_pnl REAL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 系统配置表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 创建索引
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_trades_open_time ON trades(open_time)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_signals_executed ON signals(executed)')
        
        conn.commit()
        conn.close()
    
    # ==================== 交易记录 ====================
    
    def add_trade(self, trade: Dict) -> int:
        """添加交易记录"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO trades (symbol, side, entry_price, quantity, leverage, status, open_time)
            VALUES (?, ?, ?, ?, ?, 'open', ?)
        ''', (
            trade['symbol'], trade['side'], trade['entry_price'],
            trade['quantity'], trade['leverage'], datetime.now().isoformat()
        ))
        
        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return trade_id
    
    def close_trade(self, trade_id: int, exit_price: float, reason: str = None):
        """平仓"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 获取开仓信息
        cursor.execute('SELECT * FROM trades WHERE id = ?', (trade_id,))
        trade = cursor.fetchone()
        
        if trade:
            # 计算盈亏
            if trade['side'] == 'long':
                pnl = (exit_price - trade['entry_price']) * trade['quantity']
            else:
                pnl = (trade['entry_price'] - exit_price) * trade['quantity']
            
            pnl_percent = (exit_price - trade['entry_price']) / trade['entry_price'] * 100
            if trade['side'] == 'short':
                pnl_percent = -pnl_percent
            
            cursor.execute('''
                UPDATE trades 
                SET exit_price = ?, pnl = ?, pnl_percent = ?, status = 'closed',
                    close_time = ?, reason = ?
                WHERE id = ?
            ''', (exit_price, pnl, pnl_percent, datetime.now().isoformat(), reason, trade_id))
        
        conn.commit()
        conn.close()
    
    def get_trades(self, limit: int = 100, offset: int = 0, 
                   symbol: str = None, status: str = None) -> List[Dict]:
        """获取交易记录"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        query = 'SELECT * FROM trades WHERE 1=1'
        params = []
        
        if symbol:
            query += ' AND symbol = ?'
            params.append(symbol)
        if status:
            query += ' AND status = ?'
            params.append(status)
        
        query += ' ORDER BY open_time DESC LIMIT ? OFFSET ?'
        params.extend([limit, offset])
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    
    # ==================== 信号记录 ====================
    
    def add_signal(self, signal: Dict) -> int:
        """添加信号记录"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO signals (symbol, signal_type, price, strength, reasons, 
                              strategies_triggered, filtered, filter_reason, executed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            signal['symbol'], signal['signal_type'], signal['price'],
            signal['strength'], json.dumps(signal['reasons']),
            json.dumps(signal.get('strategies_triggered', [])),
            signal.get('filtered', False),
            signal.get('filter_reason'),
            signal.get('executed', False)
        ))
        
        signal_id = cursor.lastrowid
        
        # 添加策略分析记录
        if 'strategy_details' in signal:
            for detail in signal['strategy_details']:
                cursor.execute('''
                    INSERT INTO strategy_analysis 
                    (signal_id, strategy_name, strategy_type, triggered, details, confidence)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (
                    signal_id, detail['name'], detail['type'],
                    detail['triggered'], json.dumps(detail), detail.get('confidence', 0)
                ))
        
        conn.commit()
        conn.close()
        return signal_id
    
    def update_signal_executed(self, signal_id: int, trade_id: int = None):
        """更新信号执行状态"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE signals 
            SET executed = TRUE, executed_at = ?, trade_id = ?
            WHERE id = ?
        ''', (datetime.now().isoformat(), trade_id, signal_id))
        
        conn.commit()
        conn.close()
    
    def get_signals(self, limit: int = 100, offset: int = 0,
                   symbol: str = None, executed: bool = None) -> List[Dict]:
        """获取信号记录"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        query = 'SELECT * FROM signals WHERE 1=1'
        params = []
        
        if symbol:
            query += ' AND symbol = ?'
            params.append(symbol)
        if executed is not None:
            query += ' AND executed = ?'
            params.append(executed)
        
        query += ' ORDER BY created_at DESC LIMIT ? OFFSET ?'
        params.extend([limit, offset])
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        
        results = []
        for row in rows:
            d = dict(row)
            d['reasons'] = json.loads(d['reasons'])
            d['strategies_triggered'] = json.loads(d['strategies_triggered']) if d['strategies_triggered'] else []
            results.append(d)
        
        return results
    
    def get_signal_stats(self) -> Dict:
        """获取信号统计"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 总信号数
        cursor.execute('SELECT COUNT(*) as total FROM signals')
        total = cursor.fetchone()['total']
        
        # 执行的信号数
        cursor.execute('SELECT COUNT(*) as executed FROM signals WHERE executed = TRUE')
        executed = cursor.fetchone()['executed']
        
        # 按币种统计
        cursor.execute('''
            SELECT symbol, COUNT(*) as count, 
                   SUM(CASE WHEN executed = TRUE THEN 1 ELSE 0 END) as executed
            FROM signals 
            GROUP BY symbol
        ''')
        by_symbol = [dict(row) for row in cursor.fetchall()]
        
        # 按信号类型统计
        cursor.execute('''
            SELECT signal_type, COUNT(*) as count
            FROM signals 
            GROUP BY signal_type
        ''')
        by_type = [dict(row) for row in cursor.fetchall()]
        
        conn.close()
        
        return {
            'total': total,
            'executed': executed,
            'pass_rate': round(executed / total * 100, 2) if total > 0 else 0,
            'by_symbol': by_symbol,
            'by_type': by_type
        }
    
    # ==================== 持仓管理 ====================
    
    def update_position(self, symbol: str, side: str, entry_price: float, 
                      quantity: float, leverage: int, current_price: float = None):
        """更新持仓"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        unrealized_pnl = 0
        if current_price and entry_price:
            if side == 'long':
                unrealized_pnl = (current_price - entry_price) * quantity
            else:
                unrealized_pnl = (entry_price - current_price) * quantity
        
        cursor.execute('''
            INSERT OR REPLACE INTO positions 
            (symbol, side, entry_price, quantity, leverage, current_price, unrealized_pnl, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (symbol, side, entry_price, quantity, leverage, current_price, unrealized_pnl, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
    
    def close_position(self, symbol: str):
        """平仓"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM positions WHERE symbol = ?', (symbol,))
        conn.commit()
        conn.close()
    
    def get_positions(self) -> List[Dict]:
        """获取所有持仓"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM positions')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    # ==================== 统计 ====================
    
    def get_dashboard_stats(self) -> Dict:
        """获取仪表盘统计数据"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 交易统计
        cursor.execute('''
            SELECT 
                COUNT(*) as total_trades,
                SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) as closed_trades,
                SUM(CASE WHEN status = 'open' THEN 1 ELSE 0 END) as open_trades,
                SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as total_profit,
                SUM(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END) as total_loss,
                SUM(pnl) as net_pnl
            FROM trades
        ''')
        trade_stats = dict(cursor.fetchone())
        
        # 信号统计
        cursor.execute('''
            SELECT 
                COUNT(*) as total_signals,
                SUM(CASE WHEN executed = TRUE THEN 1 ELSE 0 END) as executed_signals
            FROM signals
        ''')
        signal_stats = dict(cursor.fetchone())
        
        # 今日统计
        today = datetime.now().date().isoformat()
        cursor.execute('''
            SELECT COUNT(*) as today_signals FROM signals 
            WHERE created_at LIKE ?
        ''', (f'{today}%',))
        today_signals = cursor.fetchone()['today_signals']
        
        conn.close()
        
        return {
            **trade_stats,
            **signal_stats,
            'today_signals': today_signals,
            'pass_rate': round(signal_stats['executed_signals'] / signal_stats['total_signals'] * 100, 2) 
                        if signal_stats['total_signals'] > 0 else 0
        }


# 全局数据库实例
db = Database()
