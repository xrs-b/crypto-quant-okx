"""
数据库模块 - SQLite实现
"""
import sqlite3
import json
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pathlib import Path
import pandas as pd


class Database:
    """数据库管理类"""
    
    def __init__(self, db_path: str = "data/trading.db"):
        self.db_path = db_path
        self._ensure_dir()
        self._init_db()
    
    def _ensure_dir(self):
        """确保目录存在"""
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
    
    def _get_connection(self):
        """获取数据库连接"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def _init_db(self):
        """初始化数据库表"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 信号记录表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                price REAL NOT NULL,
                strength INTEGER DEFAULT 0,
                reasons TEXT,
                strategies_triggered TEXT,
                filtered INTEGER DEFAULT 0,
                filter_reason TEXT,
                executed INTEGER DEFAULT 0,
                trade_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 交易记录表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL,
                quantity REAL NOT NULL,
                leverage INTEGER DEFAULT 1,
                pnl REAL,
                pnl_percent REAL,
                status TEXT DEFAULT 'open',
                open_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                close_time TIMESTAMP,
                notes TEXT,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            )
        """)
        
        # 持仓表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL UNIQUE,
                side TEXT NOT NULL,
                entry_price REAL NOT NULL,
                current_price REAL,
                quantity REAL NOT NULL,
                leverage INTEGER DEFAULT 1,
                unrealized_pnl REAL DEFAULT 0,
                opened_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 每日总结表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE UNIQUE NOT NULL,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0,
                total_volume REAL DEFAULT 0,
                signals_generated INTEGER DEFAULT 0,
                signals_filtered INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 策略分析表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS strategy_analysis (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER,
                strategy_name TEXT NOT NULL,
                triggered INTEGER DEFAULT 0,
                strength INTEGER DEFAULT 0,
                confidence REAL,
                action TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (signal_id) REFERENCES signals(id)
            )
        """)
        
        # 系统日志表
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 候选晋升/降级历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS candidate_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                decision TEXT NOT NULL,
                best_variant TEXT,
                score REAL,
                reason TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # preset 应用历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS preset_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                preset_name TEXT NOT NULL,
                watch_list TEXT,
                backup_path TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 治理决策历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS governance_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_type TEXT NOT NULL,
                level TEXT,
                approval_required INTEGER DEFAULT 0,
                recommended_preset TEXT,
                message TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 日报
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS daily_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                report_date DATE NOT NULL,
                summary TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol)")
        
        conn.commit()
        conn.close()
    
    # =========================================================================
    # 信号操作
    # =========================================================================
    
    def record_signal(self, symbol: str, signal_type: str, price: float,
                      strength: int, reasons: List[Dict], 
                      strategies_triggered: List[str]) -> int:
        """记录信号"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO signals (symbol, signal_type, price, strength, reasons, strategies_triggered)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (symbol, signal_type, price, strength, 
              json.dumps(reasons), json.dumps(strategies_triggered)))
        
        signal_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return signal_id
    
    def update_signal(self, signal_id: int, **kwargs):
        """更新信号"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        fields = []
        values = []
        for k, v in kwargs.items():
            fields.append(f"{k} = ?")
            values.append(v)
        
        values.append(signal_id)
        cursor.execute(f"UPDATE signals SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        conn.close()
    
    def get_signals(self, symbol: str = None, limit: int = 100, 
                    executed_only: bool = False) -> List[Dict]:
        """获取信号列表"""
        conn = self._get_connection()
        
        query = "SELECT * FROM signals"
        conditions = []
        params = []
        
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if executed_only:
            conditions.append("executed = 1")
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        
        # 转换JSON字段
        if not df.empty:
            df['reasons'] = df['reasons'].apply(lambda x: json.loads(x) if x else [])
            df['strategies_triggered'] = df['strategies_triggered'].apply(lambda x: json.loads(x) if x else [])
            df['filtered'] = df['filtered'].astype(bool)
            df['executed'] = df['executed'].astype(bool)
        
        return df.to_dict('records')
    
    # =========================================================================
    # 交易操作
    # =========================================================================
    
    def record_trade(self, symbol: str, side: str, entry_price: float,
                     quantity: float, leverage: int = 1, 
                     signal_id: int = None, notes: str = None) -> int:
        """记录交易"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO trades (symbol, side, entry_price, quantity, leverage, signal_id, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (symbol, side, entry_price, quantity, leverage, signal_id, notes))
        
        trade_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return trade_id
    
    def update_trade(self, trade_id: int, **kwargs):
        """更新交易"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        fields = []
        values = []
        for k, v in kwargs.items():
            fields.append(f"{k} = ?")
            values.append(v)
        
        values.append(trade_id)
        cursor.execute(f"UPDATE trades SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()
        conn.close()
    
    def close_trade(self, trade_id: int, exit_price: float, pnl: float, 
                    pnl_percent: float, notes: str = None):
        """平仓"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            UPDATE trades 
            SET exit_price = ?, pnl = ?, pnl_percent = ?, 
                status = 'closed', close_time = CURRENT_TIMESTAMP, notes = ?
            WHERE id = ?
        """, (exit_price, pnl, pnl_percent, notes, trade_id))
        
        conn.commit()
        conn.close()
    
    def get_trades(self, symbol: str = None, status: str = None,
                   limit: int = 100) -> List[Dict]:
        """获取交易列表"""
        conn = self._get_connection()
        
        query = "SELECT * FROM trades"
        conditions = []
        params = []
        
        if symbol:
            conditions.append("symbol = ?")
            params.append(symbol)
        if status:
            conditions.append("status = ?")
            params.append(status)
        
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        
        query += " ORDER BY open_time DESC LIMIT ?"
        params.append(limit)
        
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        return df.to_dict('records')
    
    def get_trade_stats(self, days: int = 30) -> Dict:
        """获取交易统计"""
        conn = self._get_connection()
        
        query = """
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losses,
                SUM(pnl) as total_pnl,
                AVG(pnl_percent) as avg_pnl_percent
            FROM trades 
            WHERE status = 'closed' 
            AND open_time >= datetime('now', '-' || ? || ' days')
        """
        
        df = pd.read_sql_query(query, conn, params=(days,))
        conn.close()
        
        if not df.empty:
            row = df.iloc[0]
            total = row['total'] or 0
            wins = row['wins'] or 0
            return {
                'total_trades': int(total),
                'winning_trades': int(wins),
                'losing_trades': int(row['losses'] or 0),
                'win_rate': round(wins / total * 100, 2) if total > 0 else 0,
                'total_pnl': round(row['total_pnl'] or 0, 2),
                'avg_pnl_percent': round(row['avg_pnl_percent'] or 0, 2)
            }
        return {'total_trades': 0, 'winning_trades': 0, 'losing_trades': 0, 
                'win_rate': 0, 'total_pnl': 0, 'avg_pnl_percent': 0}
    
    # =========================================================================
    # 持仓操作
    # =========================================================================
    
    def update_position(self, symbol: str, side: str, entry_price: float,
                       quantity: float, leverage: int, current_price: float):
        """更新持仓"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        # 计算未实现盈亏
        if side == 'long':
            unrealized_pnl = (current_price - entry_price) * quantity
        else:
            unrealized_pnl = (entry_price - current_price) * quantity
        
        cursor.execute("""
            INSERT OR REPLACE INTO positions 
            (symbol, side, entry_price, current_price, quantity, leverage, 
             unrealized_pnl, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        """, (symbol, side, entry_price, current_price, quantity, leverage, unrealized_pnl))
        
        conn.commit()
        conn.close()
    
    def get_positions(self) -> List[Dict]:
        """获取当前持仓"""
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM positions", conn)
        conn.close()
        return df.to_dict('records')
    
    def close_position(self, symbol: str):
        """平仓(删除持仓记录)"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        conn.commit()
        conn.close()
    
    # =========================================================================
    # 策略分析操作
    # =========================================================================
    
    def record_strategy_analysis(self, signal_id: int, strategy_name: str,
                                 triggered: bool, strength: int = 0,
                                 confidence: float = 0, action: str = None,
                                 details: str = None):
        """记录策略分析"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO strategy_analysis 
            (signal_id, strategy_name, triggered, strength, confidence, action, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (signal_id, strategy_name, triggered, strength, confidence, action, details))
        
        conn.commit()
        conn.close()
    
    def get_strategy_stats(self, days: int = 30) -> Dict:
        """获取策略统计"""
        conn = self._get_connection()
        
        query = """
            SELECT 
                strategy_name,
                COUNT(*) as total_signals,
                SUM(CASE WHEN triggered = 1 THEN 1 ELSE 0 END) as triggered_count,
                AVG(confidence) as avg_confidence
            FROM strategy_analysis
            WHERE created_at >= datetime('now', '-' || ? || ' days')
            GROUP BY strategy_name
        """
        
        df = pd.read_sql_query(query, conn, params=(days,))
        conn.close()
        return df.to_dict('records')
    
    # =========================================================================
    # 日志操作
    # =========================================================================
    
    def log(self, level: str, message: str, details: Dict = None):
        """记录日志"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("""
            INSERT INTO system_logs (level, message, details)
            VALUES (?, ?, ?)
        """, (level, message, json.dumps(details) if details else None))
        
        conn.commit()
        conn.close()
    
    # =========================================================================
    # 候选审查 / preset 历史
    # =========================================================================

    def record_candidate_review(self, symbol: str, decision: str, best_variant: str = None,
                                score: float = None, reason: str = None, details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO candidate_reviews (symbol, decision, best_variant, score, reason, details)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (symbol, decision, best_variant, score, reason, json.dumps(details) if details else None))
        conn.commit()
        conn.close()

    def get_candidate_reviews(self, symbol: str = None, limit: int = 100) -> List[Dict]:
        conn = self._get_connection()
        query = "SELECT * FROM candidate_reviews"
        params = []
        if symbol:
            query += " WHERE symbol = ?"
            params.append(symbol)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        if not df.empty and 'details' in df.columns:
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def record_preset_history(self, preset_name: str, watch_list: List[str], backup_path: str = None, details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO preset_history (preset_name, watch_list, backup_path, details)
            VALUES (?, ?, ?, ?)
        """, (preset_name, json.dumps(watch_list or []), backup_path, json.dumps(details) if details else None))
        conn.commit()
        conn.close()

    def get_preset_history(self, limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM preset_history ORDER BY created_at DESC LIMIT ?", conn, params=(limit,))
        conn.close()
        if not df.empty:
            df['watch_list'] = df['watch_list'].apply(lambda x: json.loads(x) if x else [])
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def record_governance_decision(self, decision_type: str, level: str = None, approval_required: int = 0,
                                   recommended_preset: str = None, message: str = None, details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO governance_decisions (decision_type, level, approval_required, recommended_preset, message, details)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (decision_type, level, approval_required, recommended_preset, message, json.dumps(details) if details else None))
        conn.commit()
        conn.close()

    def get_governance_decisions(self, limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM governance_decisions ORDER BY created_at DESC LIMIT ?", conn, params=(limit,))
        conn.close()
        if not df.empty:
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def record_daily_report(self, report_date: str, summary: Dict):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO daily_reports (report_date, summary) VALUES (?, ?)", (report_date, json.dumps(summary)))
        conn.commit()
        conn.close()

    def get_daily_reports(self, limit: int = 30) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM daily_reports ORDER BY created_at DESC LIMIT ?", conn, params=(limit,))
        conn.close()
        if not df.empty:
            df['summary'] = df['summary'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    # =========================================================================
    # 清理操作
    # =========================================================================
    
    def cleanup_old_data(self, signals_days: int = 90, 
                         trades_days: int = 365, logs_days: int = 30):
        """清理旧数据"""
        conn = self._get_connection()
        cursor = conn.cursor()
        
        cursor.execute("DELETE FROM signals WHERE created_at < datetime('now', '-' || ? || ' days')", (signals_days,))
        cursor.execute("DELETE FROM trades WHERE open_time < datetime('now', '-' || ? || ' days')", (trades_days,))
        cursor.execute("DELETE FROM system_logs WHERE created_at < datetime('now', '-' || ? || ' days')", (logs_days,))
        cursor.execute("DELETE FROM candidate_reviews WHERE created_at < datetime('now', '-90 days')")
        cursor.execute("DELETE FROM preset_history WHERE created_at < datetime('now', '-180 days')")
        cursor.execute("DELETE FROM governance_decisions WHERE created_at < datetime('now', '-180 days')")
        cursor.execute("DELETE FROM daily_reports WHERE created_at < datetime('now', '-365 days')")
        
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted


# 全局数据库实例
db = Database()
