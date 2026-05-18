"""
core/risk_state.py — 跨进程的"风险闸门"状态文件

设计:
  - daily_risk_report(15:30) 一旦发现 CVaR/回撤/MC 任一 breach,把状态写入
    data/risk_state.json
  - IntradayMonitor 每轮 _check_new_positions 入口读取,halt_new_buys=True
    且未过期则直接拒绝所有新仓建仓,只保留持仓监控 + 平仓动作
  - 过期窗口默认 24h(下次 daily_risk_report 会刷新或清空)
  - 写文件用 tmp + os.replace 原子化,避免读到半截 JSON

文件格式:
  {
    "updated_at": "2026-05-18T15:32:00",
    "date": "2026-05-18",
    "breach": ["CVaR_5%", "drawdown_15%"],
    "halt_new_buys": true,
    "reason": "组合风险超限",
    "ttl_hours": 24,
    "summary": {...}     # 完整 daily_risk_report summary,诊断用
  }
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger('core.risk_state')

_PROJ_DIR = Path(__file__).parent.parent
_DEFAULT_PATH = _PROJ_DIR / 'data' / 'risk_state.json'
_DEFAULT_TTL_HOURS = 24


def _path() -> Path:
    env = os.environ.get('QUANT_RISK_STATE_PATH', '').strip()
    if env:
        return Path(env)
    return _DEFAULT_PATH


def write_risk_state(
    breach: List[str],
    summary: Optional[Dict[str, Any]] = None,
    ttl_hours: int = _DEFAULT_TTL_HOURS,
) -> bool:
    """把当前风险状态写到 risk_state.json。

    无论 breach 是否为空都写,这样 daily_risk_report 在恢复正常的当天
    会主动"解除"闸门。返回是否写入成功。
    """
    payload = {
        'updated_at': datetime.now().isoformat(timespec='seconds'),
        'date': datetime.now().date().isoformat(),
        'breach': list(breach or []),
        'halt_new_buys': bool(breach),
        'reason': (
            '组合风险超限: ' + ', '.join(breach)
            if breach else '组合风险检查通过'
        ),
        'ttl_hours': int(ttl_hours),
        'summary': summary or {},
    }
    target = _path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 原子写:tmp + replace
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8',
            dir=str(target.parent), prefix='.risk_state-', suffix='.json',
            delete=False,
        ) as tf:
            json.dump(payload, tf, ensure_ascii=False, indent=2, default=str)
            tmp_name = tf.name
        os.replace(tmp_name, target)
        logger.info(
            'risk_state written: halt_new_buys=%s breach=%s',
            payload['halt_new_buys'], payload['breach'],
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning('risk_state write failed: %s', exc)
        return False


def read_risk_state() -> Optional[Dict[str, Any]]:
    """读取 risk_state.json。文件不存在或损坏返回 None。"""
    target = _path()
    if not target.exists():
        return None
    try:
        with open(target, encoding='utf-8') as f:
            return json.load(f)
    except Exception as exc:  # noqa: BLE001
        logger.warning('risk_state read failed: %s', exc)
        return None


def is_new_buys_halted(
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """便捷接口:返回 (是否拦截新仓, 原因)。

    判定:
      - 文件不存在 / 解析失败       → (False, '')
      - halt_new_buys=False         → (False, '')
      - 文件比 ttl_hours 旧         → (False, '')(过期视为已恢复)
      - 否则                        → (True, summary['reason'])
    """
    state = read_risk_state()
    if not state or not state.get('halt_new_buys'):
        return False, ''
    try:
        updated = datetime.fromisoformat(state.get('updated_at', ''))
    except Exception:
        return False, ''
    ttl = float(state.get('ttl_hours', _DEFAULT_TTL_HOURS))
    now = now or datetime.now()
    if now - updated > timedelta(hours=ttl):
        return False, ''
    return True, str(state.get('reason') or '组合风险超限')
