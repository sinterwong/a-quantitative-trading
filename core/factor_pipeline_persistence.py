"""
core/factor_pipeline_persistence.py — DynamicWeightPipeline IC 状态持久化

之前 _ic_history / _dynamic_weights / _decay_disabled 全部只在内存,进程一
重启 63 天滚动 IC 重新从 0 开始攒,衰减保护计数被重置。本模块把状态写到
state.db 的 factor_pipeline_state 表,init 时恢复。

表结构:
    factor_pipeline_state(
        pipeline_id      TEXT,
        factor_name      TEXT,
        ic_history       TEXT,   -- JSON list[float]
        decay_disabled   INTEGER, -- 0/1
        current_weight   REAL,
        bars_since_update INTEGER,
        updated_at       TEXT,
        PRIMARY KEY (pipeline_id, factor_name)
    )

约定:
  - pipeline_id 默认 "default",未来支持多 pipeline 并存
  - 只读 / 只写 IC + 衰减 + 权重 + bars_since_update 这一最小必要状态;
    _weight_history / _factor_status_log 仍保留在内存(诊断用,丢了也行)
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import closing
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger('core.factor_pipeline_persistence')

_DEFAULT_PIPELINE_ID = 'default'
_WRITE_LOCK = threading.Lock()


def _get_conn() -> Optional[sqlite3.Connection]:
    try:
        from core.state_db import state_db_path
        conn = sqlite3.connect(state_db_path(), timeout=10, check_same_thread=False)
        conn.execute('PRAGMA journal_mode=WAL')
        return conn
    except Exception as exc:
        logger.warning('factor_pipeline_persistence connect failed: %s', exc)
        return None


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS factor_pipeline_state (
            pipeline_id        TEXT NOT NULL,
            factor_name        TEXT NOT NULL,
            ic_history         TEXT NOT NULL DEFAULT '[]',
            decay_disabled     INTEGER NOT NULL DEFAULT 0,
            current_weight     REAL NOT NULL DEFAULT 0,
            bars_since_update  INTEGER NOT NULL DEFAULT 0,
            updated_at         TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (pipeline_id, factor_name)
        )
        """
    )
    conn.commit()


def load_pipeline_state(
    pipeline_id: str = _DEFAULT_PIPELINE_ID,
) -> Tuple[Dict[str, List[float]], Dict[str, float], Dict[str, bool], int]:
    """
    返回 (ic_history, dynamic_weights, decay_disabled, bars_since_update)。
    任何错误都返回空状态(等价于全新启动),不会抛异常。
    """
    empty: Tuple[Dict[str, List[float]], Dict[str, float], Dict[str, bool], int] = ({}, {}, {}, 0)
    conn = _get_conn()
    if conn is None:
        return empty
    try:
        with closing(conn):
            _ensure_schema(conn)
            rows = conn.execute(
                'SELECT factor_name, ic_history, decay_disabled, current_weight,'
                ' bars_since_update FROM factor_pipeline_state'
                ' WHERE pipeline_id = ?',
                (pipeline_id,),
            ).fetchall()

            ic_history: Dict[str, List[float]] = {}
            weights: Dict[str, float] = {}
            disabled: Dict[str, bool] = {}
            bars: int = 0
            for name, ic_json, dis, w, b in rows:
                try:
                    ic_history[name] = json.loads(ic_json or '[]')
                except Exception:
                    ic_history[name] = []
                weights[name] = float(w or 0.0)
                disabled[name] = bool(dis)
                bars = max(bars, int(b or 0))
            logger.info(
                'factor_pipeline_state loaded: pipeline=%s n_factors=%d bars=%d',
                pipeline_id, len(rows), bars,
            )
            return ic_history, weights, disabled, bars
    except Exception as exc:
        logger.warning('load_pipeline_state read failed: %s', exc)
        return empty


def save_pipeline_state(
    ic_history: Dict[str, List[float]],
    dynamic_weights: Dict[str, float],
    decay_disabled: Dict[str, bool],
    bars_since_update: int,
    pipeline_id: str = _DEFAULT_PIPELINE_ID,
) -> bool:
    """
    UPSERT 当前 IC 状态到 state.db。失败不抛异常,返回 False。
    """
    conn = _get_conn()
    if conn is None:
        return False
    factors = set(ic_history) | set(dynamic_weights) | set(decay_disabled)
    if not factors:
        conn.close()
        return False
    try:
        with _WRITE_LOCK, closing(conn):
            _ensure_schema(conn)
            rows = [
                (
                    pipeline_id, name,
                    json.dumps(ic_history.get(name, [])),
                    1 if decay_disabled.get(name, False) else 0,
                    float(dynamic_weights.get(name, 0.0)),
                    int(bars_since_update),
                )
                for name in factors
            ]
            conn.executemany(
                """
                INSERT INTO factor_pipeline_state
                    (pipeline_id, factor_name, ic_history, decay_disabled,
                     current_weight, bars_since_update, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(pipeline_id, factor_name) DO UPDATE SET
                    ic_history        = excluded.ic_history,
                    decay_disabled    = excluded.decay_disabled,
                    current_weight    = excluded.current_weight,
                    bars_since_update = excluded.bars_since_update,
                    updated_at        = CURRENT_TIMESTAMP
                """,
                rows,
            )
            conn.commit()
        return True
    except Exception as exc:
        logger.warning('save_pipeline_state failed: %s', exc)
        return False
