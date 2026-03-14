#!/usr/bin/env python3
"""候选观察任务：可供 cron / 手动运行"""
import sys
from pathlib import Path

PROJECT_DIR = Path('/Volumes/MacHD/Projects/crypto-quant-okx')
sys.path.insert(0, str(PROJECT_DIR))

from core.config import Config
from core.database import Database
from analytics.optimizer import ParameterOptimizer


def main():
    cfg = Config()
    db = Database(cfg.db_path)
    optimizer = ParameterOptimizer(cfg, db)
    result = optimizer.run(use_cache=False)
    print('候选晋升建议:')
    for row in result.get('candidate_promotions', []):
        print(f"- {row['symbol']}: {row['decision']} | {row['reason']}")


if __name__ == '__main__':
    main()
