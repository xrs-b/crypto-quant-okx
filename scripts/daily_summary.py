#!/usr/bin/env python3
"""生成日报摘要：可供 cron / 手动运行"""
import os
import sys
from pathlib import Path
from pprint import pprint

PROJECT_DIR = Path(os.getenv('PROJECT_DIR', Path(__file__).resolve().parent.parent)).resolve()
sys.path.insert(0, str(PROJECT_DIR))

from core.config import Config
from core.database import Database
from analytics.governance import GovernanceEngine


def main():
    cfg = Config()
    db = Database(cfg.db_path)
    gov = GovernanceEngine(cfg, db)
    report = gov.generate_daily_summary()
    contract = report.get('close_outcome_decision_contract') or {}
    operator_policy = contract.get('operator_action_policy') or {}
    follow_up_gate = contract.get('follow_up_policy_gate') or {}
    print('今日日报:')
    pprint(report)
    if contract:
        print('\nclose outcome contract:')
        print({
            'governance_mode': report.get('close_outcome_feedback_loop', {}).get('governance_mode'),
            'operator_action': operator_policy.get('action'),
            'route': operator_policy.get('route'),
            'follow_up': operator_policy.get('follow_up'),
            'follow_up_decision': follow_up_gate.get('decision'),
            'follow_up_owner': follow_up_gate.get('owner'),
        })


if __name__ == '__main__':
    main()
