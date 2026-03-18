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
                filter_code TEXT,
                filter_group TEXT,
                action_hint TEXT,
                filter_details TEXT,
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
                contract_size REAL DEFAULT 1,
                coin_quantity REAL,
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
                contract_size REAL DEFAULT 1,
                coin_quantity REAL,
                leverage INTEGER DEFAULT 1,
                unrealized_pnl REAL DEFAULT 0,
                peak_price REAL,
                trough_price REAL,
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

        # 审批历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS approval_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                approval_type TEXT NOT NULL,
                target TEXT,
                decision TEXT NOT NULL,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 治理参数变更历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS governance_config_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                config_key TEXT NOT NULL,
                before_value TEXT,
                after_value TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # smoke 验收执行历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS smoke_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                exchange_mode TEXT,
                position_mode TEXT,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                amount REAL,
                success INTEGER DEFAULT 0,
                error TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # override 应用/回滚审计历史
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS override_audit_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                target_file TEXT,
                backup_path TEXT,
                symbols TEXT,
                parameter_count INTEGER DEFAULT 0,
                note TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 通知 outbox（给 OpenClaw bridge 消费）
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notification_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel TEXT NOT NULL,
                event_type TEXT NOT NULL,
                title TEXT,
                message TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                details TEXT,
                delivered_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        # 兼容旧库：补充信号诊断字段
        cursor.execute("PRAGMA table_info(signals)")
        signal_columns = {row[1] for row in cursor.fetchall()}
        if 'filter_code' not in signal_columns:
            cursor.execute("ALTER TABLE signals ADD COLUMN filter_code TEXT")
        if 'filter_group' not in signal_columns:
            cursor.execute("ALTER TABLE signals ADD COLUMN filter_group TEXT")
        if 'action_hint' not in signal_columns:
            cursor.execute("ALTER TABLE signals ADD COLUMN action_hint TEXT")
        if 'filter_details' not in signal_columns:
            cursor.execute("ALTER TABLE signals ADD COLUMN filter_details TEXT")

        # 兼容旧库：补充 trades / positions 单位字段与持仓追踪锚点字段
        cursor.execute("PRAGMA table_info(trades)")
        trade_columns = {row[1] for row in cursor.fetchall()}
        if 'contract_size' not in trade_columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN contract_size REAL DEFAULT 1")
        if 'coin_quantity' not in trade_columns:
            cursor.execute("ALTER TABLE trades ADD COLUMN coin_quantity REAL")

        cursor.execute("PRAGMA table_info(positions)")
        position_columns = {row[1] for row in cursor.fetchall()}
        if 'contract_size' not in position_columns:
            cursor.execute("ALTER TABLE positions ADD COLUMN contract_size REAL DEFAULT 1")
        if 'coin_quantity' not in position_columns:
            cursor.execute("ALTER TABLE positions ADD COLUMN coin_quantity REAL")
        if 'peak_price' not in position_columns:
            cursor.execute("ALTER TABLE positions ADD COLUMN peak_price REAL")
        if 'trough_price' not in position_columns:
            cursor.execute("ALTER TABLE positions ADD COLUMN trough_price REAL")

        # 创建索引
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_positions_symbol ON positions(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_smoke_runs_symbol ON smoke_runs(symbol)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_smoke_runs_created ON smoke_runs(created_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notification_outbox_status ON notification_outbox(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notification_outbox_created ON notification_outbox(created_at)")
        
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
            if 'filter_details' in df.columns:
                df['filter_details'] = df['filter_details'].apply(
                    lambda x: json.loads(x) if isinstance(x, str) and x else ({ } if x is None or pd.isna(x) else x)
                )
            df['filtered'] = df['filtered'].astype(bool)
            df['executed'] = df['executed'].astype(bool)
        
        return df.to_dict('records')
    
    # =========================================================================
    # 交易操作
    # =========================================================================
    
    def record_trade(self, symbol: str, side: str, entry_price: float,
                     quantity: float, leverage: int = 1,
                     signal_id: int = None, notes: str = None,
                     contract_size: float = 1.0, coin_quantity: float = None) -> int:
        """记录交易"""
        conn = self._get_connection()
        cursor = conn.cursor()
        if coin_quantity is None:
            coin_quantity = float(quantity or 0) * float(contract_size or 1)
        
        cursor.execute("""
            INSERT INTO trades (symbol, side, entry_price, quantity, contract_size, coin_quantity, leverage, signal_id, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (symbol, side, entry_price, quantity, contract_size, coin_quantity, leverage, signal_id, notes))
        
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

    def get_latest_open_trade(self, symbol: str, side: str = None) -> Optional[Dict]:
        """获取某币种最新未平仓交易"""
        conn = self._get_connection()
        query = "SELECT * FROM trades WHERE symbol = ? AND status = 'open'"
        params = [symbol]
        if side:
            query += " AND side = ?"
            params.append(side)
        query += " ORDER BY open_time DESC, id DESC LIMIT 1"
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        if df.empty:
            return None
        return df.iloc[0].to_dict()

    def get_latest_trade_time(self, symbol: str = None) -> Optional[datetime]:
        conn = self._get_connection()
        cursor = conn.cursor()
        if symbol:
            cursor.execute("SELECT open_time FROM trades WHERE symbol = ? ORDER BY open_time DESC, id DESC LIMIT 1", (symbol,))
        else:
            cursor.execute("SELECT open_time FROM trades ORDER BY open_time DESC, id DESC LIMIT 1")
        row = cursor.fetchone()
        conn.close()
        if not row or not row[0]:
            return None
        return datetime.fromisoformat(row[0])

    def get_open_trades(self, symbol: str = None, side: str = None, limit: int = 200) -> List[Dict]:
        conn = self._get_connection()
        query = "SELECT * FROM trades WHERE status = 'open'"
        params = []
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        if side:
            query += " AND side = ?"
            params.append(side)
        query += " ORDER BY open_time DESC, id DESC LIMIT ?"
        params.append(limit)
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        return df.to_dict('records')

    def mark_trade_stale_closed(self, trade_id: int, reason: str, close_price: float = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        note = f"自动收口: {reason}"
        cursor.execute(
            "UPDATE trades SET status = 'closed', exit_price = COALESCE(?, exit_price), close_time = CURRENT_TIMESTAMP, notes = CASE WHEN notes IS NULL OR notes = '' THEN ? ELSE notes || ' | ' || ? END WHERE id = ? AND status = 'open'",
            (close_price, note, note, trade_id)
        )
        changed = cursor.rowcount
        conn.commit()
        conn.close()
        return changed > 0
    
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
                       quantity: float, leverage: int, current_price: float,
                       peak_price: float = None, trough_price: float = None,
                       contract_size: float = 1.0, coin_quantity: float = None):
        """更新持仓"""
        conn = self._get_connection()
        cursor = conn.cursor()
        if coin_quantity is None:
            coin_quantity = float(quantity or 0) * float(contract_size or 1)
        
        # 计算未实现盈亏（按折算币数量）
        if side == 'long':
            unrealized_pnl = (current_price - entry_price) * coin_quantity
        else:
            unrealized_pnl = (entry_price - current_price) * coin_quantity

        cursor.execute("SELECT opened_at, peak_price, trough_price FROM positions WHERE symbol = ?", (symbol,))
        existing = cursor.fetchone()
        opened_at = existing['opened_at'] if existing else None
        current_peak = existing['peak_price'] if existing else None
        current_trough = existing['trough_price'] if existing else None
        final_peak = peak_price if peak_price is not None else current_peak
        final_trough = trough_price if trough_price is not None else current_trough
        if side == 'long':
            anchor = float(current_price or entry_price)
            final_peak = max(float(final_peak or anchor), anchor)
            if final_trough is None:
                final_trough = entry_price
        else:
            anchor = float(current_price or entry_price)
            final_trough = min(float(final_trough or anchor), anchor)
            if final_peak is None:
                final_peak = entry_price
        
        cursor.execute("""
            INSERT OR REPLACE INTO positions 
            (id, symbol, side, entry_price, current_price, quantity, contract_size, coin_quantity, leverage,
             unrealized_pnl, peak_price, trough_price, opened_at, updated_at)
            VALUES ((SELECT id FROM positions WHERE symbol = ?), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP), CURRENT_TIMESTAMP)
        """, (symbol, symbol, side, entry_price, current_price, quantity, contract_size, coin_quantity, leverage, unrealized_pnl, final_peak, final_trough, opened_at))
        
        conn.commit()
        conn.close()
    
    def get_positions(self) -> List[Dict]:
        """获取当前持仓"""
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM positions", conn)
        conn.close()
        if not df.empty:
            if 'contract_size' not in df.columns:
                df['contract_size'] = 1.0
            if 'coin_quantity' not in df.columns:
                df['coin_quantity'] = df['quantity'] * df['contract_size']
            else:
                df['coin_quantity'] = df.apply(lambda r: r['coin_quantity'] if pd.notna(r['coin_quantity']) else r['quantity'] * (r['contract_size'] if pd.notna(r['contract_size']) else 1.0), axis=1)
        return df.to_dict('records')
    
    def close_position(self, symbol: str):
        """平仓(删除持仓记录)"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM positions WHERE symbol = ?", (symbol,))
        conn.commit()
        conn.close()

    def remove_positions_not_in(self, symbols: List[str]) -> int:
        """删除不在指定 symbol 集合内的本地持仓"""
        conn = self._get_connection()
        cursor = conn.cursor()
        symbols = symbols or []
        if symbols:
            placeholders = ','.join(['?'] * len(symbols))
            cursor.execute(f"DELETE FROM positions WHERE symbol NOT IN ({placeholders})", symbols)
        else:
            cursor.execute("DELETE FROM positions")
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted
    
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
        details_json = json.dumps(details) if details else None

        cursor.execute("""
            SELECT id, level, approval_required, recommended_preset, message, details
            FROM governance_decisions
            WHERE decision_type = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
        """, (decision_type,))
        last = cursor.fetchone()
        if last:
            same_as_latest = (
                (last['level'] or '') == (level or '') and
                int(last['approval_required'] or 0) == int(approval_required or 0) and
                (last['recommended_preset'] or '') == (recommended_preset or '') and
                (last['message'] or '') == (message or '') and
                (last['details'] or '') == (details_json or '')
            )
            if same_as_latest:
                conn.close()
                return False

        cursor.execute("""
            INSERT INTO governance_decisions (decision_type, level, approval_required, recommended_preset, message, details)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (decision_type, level, approval_required, recommended_preset, message, details_json))
        conn.commit()
        conn.close()
        return True

    def get_governance_decisions(self, limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM governance_decisions ORDER BY created_at DESC LIMIT ?", conn, params=(limit,))
        conn.close()
        if not df.empty:
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def record_governance_config_change(self, config_key: str, before_value: Any = None, after_value: Any = None, details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO governance_config_history (config_key, before_value, after_value, details)
            VALUES (?, ?, ?, ?)
            """,
            (
                config_key,
                json.dumps(before_value, ensure_ascii=False) if before_value is not None else None,
                json.dumps(after_value, ensure_ascii=False) if after_value is not None else None,
                json.dumps(details, ensure_ascii=False) if details else None,
            )
        )
        conn.commit()
        conn.close()

    def get_governance_config_history(self, limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query(
            "SELECT * FROM governance_config_history ORDER BY created_at DESC, id DESC LIMIT ?",
            conn,
            params=(limit,)
        )
        conn.close()
        if not df.empty:
            for col in ['before_value', 'after_value', 'details']:
                df[col] = df[col].apply(lambda x: json.loads(x) if x else None)
        return df.to_dict('records')

    def record_daily_report(self, report_date: str, summary: Dict):
        conn = self._get_connection()
        cursor = conn.cursor()
        summary_json = json.dumps(summary)
        cursor.execute(
            "SELECT id, summary FROM daily_reports WHERE report_date = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            (report_date,)
        )
        existing = cursor.fetchone()
        if existing:
            if (existing['summary'] or '') == summary_json:
                conn.close()
                return {'action': 'noop', 'id': existing['id']}
            cursor.execute(
                "UPDATE daily_reports SET summary = ?, created_at = CURRENT_TIMESTAMP WHERE id = ?",
                (summary_json, existing['id'])
            )
            conn.commit()
            conn.close()
            return {'action': 'updated', 'id': existing['id']}

        cursor.execute("INSERT INTO daily_reports (report_date, summary) VALUES (?, ?)", (report_date, summary_json))
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return {'action': 'inserted', 'id': row_id}

    def get_daily_reports(self, limit: int = 30) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM daily_reports ORDER BY created_at DESC LIMIT ?", conn, params=(limit,))
        conn.close()
        if not df.empty:
            df['summary'] = df['summary'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def get_latest_daily_report(self, report_date: str) -> Optional[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query(
            "SELECT * FROM daily_reports WHERE report_date = ? ORDER BY created_at DESC, id DESC LIMIT 1",
            conn,
            params=(report_date,)
        )
        conn.close()
        if df.empty:
            return None
        row = df.iloc[0].to_dict()
        row['summary'] = json.loads(row['summary']) if row.get('summary') else {}
        return row

    def record_approval(self, approval_type: str, target: str, decision: str, details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO approval_history (approval_type, target, decision, details)
            VALUES (?, ?, ?, ?)
        """, (approval_type, target, decision, json.dumps(details) if details else None))
        conn.commit()
        conn.close()

    def record_override_audit(self, action: str, target_file: str = None, backup_path: str = None,
                              symbols: List[str] = None, parameter_count: int = 0, note: str = None,
                              details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO override_audit_history (action, target_file, backup_path, symbols, parameter_count, note, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action,
                target_file,
                backup_path,
                json.dumps(symbols or [], ensure_ascii=False),
                int(parameter_count or 0),
                note,
                json.dumps(details, ensure_ascii=False) if details else None,
            )
        )
        conn.commit()
        conn.close()

    def get_override_audit_history(self, limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query(
            "SELECT * FROM override_audit_history ORDER BY created_at DESC, id DESC LIMIT ?",
            conn,
            params=(limit,)
        )
        conn.close()
        if not df.empty:
            df['symbols'] = df['symbols'].apply(lambda x: json.loads(x) if x else [])
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def get_approval_history(self, limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM approval_history ORDER BY created_at DESC LIMIT ?", conn, params=(limit,))
        conn.close()
        if not df.empty:
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def get_latest_approval(self, approval_type: str, target: str = None) -> Optional[Dict]:
        conn = self._get_connection()
        query = "SELECT * FROM approval_history WHERE approval_type = ?"
        params = [approval_type]
        if target is not None:
            query += " AND target = ?"
            params.append(target)
        query += " ORDER BY created_at DESC, id DESC LIMIT 1"
        df = pd.read_sql_query(query, conn, params=params)
        conn.close()
        if df.empty:
            return None
        row = df.iloc[0].to_dict()
        row['details'] = json.loads(row['details']) if row.get('details') else {}
        return row

    def record_smoke_run(self, exchange_mode: str, position_mode: str, symbol: str, side: str,
                         amount: float = None, success: bool = False, error: str = None, details: Dict = None) -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO smoke_runs (exchange_mode, position_mode, symbol, side, amount, success, error, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exchange_mode,
                position_mode,
                symbol,
                side,
                amount,
                int(bool(success)),
                error,
                json.dumps(details, ensure_ascii=False) if details else None,
            )
        )
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id

    def get_smoke_runs(self, limit: int = 20) -> List[Dict]:
        conn = self._get_connection()
        df = pd.read_sql_query("SELECT * FROM smoke_runs ORDER BY created_at DESC, id DESC LIMIT ?", conn, params=(limit,))
        conn.close()
        if not df.empty:
            df['success'] = df['success'].astype(bool)
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def update_smoke_run_details(self, smoke_run_id: int, details: Dict):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE smoke_runs SET details = ? WHERE id = ?", (json.dumps(details, ensure_ascii=False), smoke_run_id))
        conn.commit()
        conn.close()

    def get_latest_smoke_run(self) -> Optional[Dict]:
        rows = self.get_smoke_runs(limit=1)
        return rows[0] if rows else None

    def enqueue_notification(self, channel: str, event_type: str, title: str, message: str, details: Dict = None) -> int:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO notification_outbox (channel, event_type, title, message, details) VALUES (?, ?, ?, ?, ?)",
            (channel, event_type, title, message, json.dumps(details, ensure_ascii=False) if details else None)
        )
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id

    def update_notification_outbox(self, notification_id: int, status: str, details: Dict = None):
        conn = self._get_connection()
        cursor = conn.cursor()
        fields = ["status = ?"]
        params = [status]
        if details is not None:
            fields.append("details = ?")
            params.append(json.dumps(details, ensure_ascii=False))
        if status == 'delivered':
            fields.append("delivered_at = CURRENT_TIMESTAMP")
        params.append(notification_id)
        cursor.execute(f"UPDATE notification_outbox SET {', '.join(fields)} WHERE id = ?", params)
        conn.commit()
        conn.close()

    def get_notification_outbox(self, status: str = 'pending', limit: int = 50) -> List[Dict]:
        conn = self._get_connection()
        if status == 'all':
            df = pd.read_sql_query("SELECT * FROM notification_outbox ORDER BY created_at ASC, id ASC LIMIT ?", conn, params=(limit,))
        else:
            df = pd.read_sql_query("SELECT * FROM notification_outbox WHERE status = ? ORDER BY created_at ASC, id ASC LIMIT ?", conn, params=(status, limit))
        conn.close()
        if not df.empty:
            df['details'] = df['details'].apply(lambda x: json.loads(x) if x else {})
        return df.to_dict('records')

    def get_notification_outbox_stats(self) -> Dict:
        conn = self._get_connection()
        cursor = conn.cursor()
        stats = {}
        for status in ['delivered', 'pending', 'suppressed', 'disabled']:
            cursor.execute("SELECT COUNT(*) FROM notification_outbox WHERE status = ?", (status,))
            stats[status] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM notification_outbox")
        stats['total'] = cursor.fetchone()[0]
        cursor.execute("SELECT MIN(created_at) FROM notification_outbox WHERE status = 'pending'")
        oldest_pending = cursor.fetchone()[0]
        cursor.execute("SELECT event_type, COUNT(*) AS count FROM notification_outbox WHERE status = 'pending' GROUP BY event_type ORDER BY count DESC LIMIT 3")
        top_pending = [{'event_type': row[0], 'count': row[1]} for row in cursor.fetchall()]
        cursor.execute("SELECT status, COUNT(*) AS count FROM notification_outbox WHERE status IN ('pending','suppressed','disabled') GROUP BY status ORDER BY count DESC")
        failure_breakdown = [{'status': row[0], 'count': row[1]} for row in cursor.fetchall()]
        rows = self.get_notification_outbox(status='all', limit=50)
        recent_failure_rows = [row for row in rows if row.get('status') in {'pending', 'suppressed', 'disabled'}]
        reason_counts = {}
        event_counts = {}
        for row in recent_failure_rows:
            event_type = row.get('event_type') or '--'
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
            details = row.get('details') or {}
            reason = details.get('aggregate_summary') or details.get('reason') or details.get('delivery', {}).get('path') or row.get('status') or '--'
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
        top_failure_events = sorted([{'event_type': k, 'count': v} for k, v in event_counts.items()], key=lambda x: x['count'], reverse=True)[:3]
        top_failure_reasons = sorted([{'reason': k, 'count': v} for k, v in reason_counts.items()], key=lambda x: x['count'], reverse=True)[:3]
        conn.close()
        stats['oldest_pending_at'] = oldest_pending
        stats['top_pending_types'] = top_pending
        stats['failure_breakdown'] = failure_breakdown
        stats['top_failure_events'] = top_failure_events
        stats['top_failure_reasons'] = top_failure_reasons
        return stats

    def mark_notification_delivered(self, notification_id: int, status: str = 'delivered'):
        self.update_notification_outbox(notification_id, status=status)

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

    def cleanup_duplicate_reports(self, dry_run: bool = True) -> Dict:
        """清理重复日报：每个 report_date 仅保留最新一条"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM daily_reports
            WHERE id NOT IN (
                SELECT MAX(id) FROM daily_reports GROUP BY report_date
            )
        """)
        duplicate_count = cursor.fetchone()[0]
        result = {'table': 'daily_reports', 'duplicates': duplicate_count, 'deleted': 0, 'dry_run': dry_run}
        if not dry_run and duplicate_count > 0:
            cursor.execute("""
                DELETE FROM daily_reports
                WHERE id NOT IN (
                    SELECT MAX(id) FROM daily_reports GROUP BY report_date
                )
            """)
            result['deleted'] = cursor.rowcount
            conn.commit()
        conn.close()
        return result

    def cleanup_duplicate_governance_decisions(self, dry_run: bool = True) -> Dict:
        """清理重复治理记录：相同 decision_type + preset + message 仅保留最新一条"""
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) FROM governance_decisions
            WHERE id NOT IN (
                SELECT MAX(id)
                FROM governance_decisions
                GROUP BY decision_type, COALESCE(recommended_preset, ''), COALESCE(message, '')
            )
        """)
        duplicate_count = cursor.fetchone()[0]
        result = {'table': 'governance_decisions', 'duplicates': duplicate_count, 'deleted': 0, 'dry_run': dry_run}
        if not dry_run and duplicate_count > 0:
            cursor.execute("""
                DELETE FROM governance_decisions
                WHERE id NOT IN (
                    SELECT MAX(id)
                    FROM governance_decisions
                    GROUP BY decision_type, COALESCE(recommended_preset, ''), COALESCE(message, '')
                )
            """)
            result['deleted'] = cursor.rowcount
            conn.commit()
        conn.close()
        return result

    def cleanup_duplicate_runtime_records(self, dry_run: bool = True) -> Dict:
        """清理运行期重复记录（日报 + 治理决策）"""
        reports = self.cleanup_duplicate_reports(dry_run=dry_run)
        governance = self.cleanup_duplicate_governance_decisions(dry_run=dry_run)
        return {
            'dry_run': dry_run,
            'daily_reports': reports,
            'governance_decisions': governance,
            'total_duplicates': reports['duplicates'] + governance['duplicates'],
            'total_deleted': reports['deleted'] + governance['deleted'],
        }


# 全局数据库实例
db = Database()
