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
    tmp_name: Optional[str] = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # 原子写:tmp + replace
        with tempfile.NamedTemporaryFile(
            mode='w', encoding='utf-8',
            dir=str(target.parent), prefix='.risk_state-', suffix='.json',
            delete=False,
        ) as tf:
            tmp_name = tf.name
            json.dump(payload, tf, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp_name, target)
        tmp_name = None  # replace 成功后所有权转移,无需清理
        logger.info(
            'risk_state written: halt_new_buys=%s breach=%s',
            payload['halt_new_buys'], payload['breach'],
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning('risk_state write failed: %s', exc)
        return False
    finally:
        # json.dump / os.replace 中途抛异常时,残留的 tmp 文件会污染 data/
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


# 过期事件(daily_risk_report 没刷新导致 stale)的告警节流,避免每 tick 都 warn
_STALE_WARN_INTERVAL_SEC = 3600.0  # 每小时最多 warn 一次
_last_stale_warn_at: Optional[datetime] = None


def read_risk_state() -> Optional[Dict[str, Any]]:
    """读取 risk_state.json。文件不存在或损坏返回 None。"""
    target = _path()
    if not target.exists():
        return None
    try:
        with open(target, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning('risk_state read failed: %s', exc)
        return None


def _warn_stale(updated: datetime, ttl_hours: float, now: datetime) -> None:
    """daily_risk_report 没刷新导致 ttl 过期 — 节流后打 warning。

    意图:如果 daily_risk_report 任务本身挂了(Scheduler 故障/数据源挂),24h 后
    闸门会自动解除,这是潜在的"静默失效"——必须显式告警让 oncall 知道。
    """
    global _last_stale_warn_at
    if _last_stale_warn_at is not None and (
        (now - _last_stale_warn_at).total_seconds() < _STALE_WARN_INTERVAL_SEC
    ):
        return
    _last_stale_warn_at = now
    age_hours = (now - updated).total_seconds() / 3600.0
    logger.warning(
        'risk_state 已过期(%.1fh > ttl=%.1fh),闸门自动解除——'
        '请确认 daily_risk_report 任务是否仍在按时运行',
        age_hours, ttl_hours,
    )


def is_new_buys_halted(
    now: Optional[datetime] = None,
) -> Tuple[bool, str]:
    """便捷接口:返回 (是否拦截新仓, 原因)。

    判定:
      - 文件不存在 / 解析失败       → (False, '')
      - halt_new_buys=False         → (False, '')
      - 文件比 ttl_hours 旧         → (False, '')(过期视为已恢复, 但会 warn)
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
        _warn_stale(updated, ttl, now)
        return False, ''
    return True, str(state.get('reason') or '组合风险超限')
