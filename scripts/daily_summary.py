#!/usr/bin/env python3
"""生成日报摘要：可供 cron / 手动运行"""
import sys
from pathlib import Path

PROJECT_DIR = Path('/Volumes/MacHD/Projects/crypto-quant-okx')
sys.path.insert(0, str(PROJECT_DIR))

from core.config import Config
from core.database import Database
from analytics.governance import GovernanceEngine


def main():
    cfg = Config()
    db = Database(cfg.db_path)
    gov = GovernanceEngine(cfg, db)
    report = gov.generate_daily_summary()
    print('今日日报:')
    print(report)


if __name__ == '__main__':
    main()
