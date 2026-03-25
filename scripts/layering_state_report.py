#!/usr/bin/env python3
"""输出当前 layering 执行态摘要，便于仿真 / 验收时快速比对。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.database import Database  # noqa: E402


def main() -> int:
    db_path = sys.argv[1] if len(sys.argv) > 1 else str(ROOT / 'data' / 'trading.db')
    db = Database(db_path)
    snapshot = db.get_execution_state_snapshot()
    payload = {
        'summary': snapshot.get('summary') or {},
        'exposure': snapshot.get('exposure') or {},
        'active_intents': snapshot.get('active_intents') or [],
        'direction_locks': snapshot.get('direction_locks') or [],
        'layer_plans': snapshot.get('layer_plans') or [],
        'signal_decisions': (snapshot.get('signal_decisions') or [])[:20],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
