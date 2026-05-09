"""
core/circuit_breaker.py — 通用故障熔断器（数据源/外部 API）

P2-16: 用于 data_layer / fundamental_data / 其它易抖动的外部调用。
连续 N 次失败触发熔断 → 在 cooldown_seconds 内直接返回 'open' 状态，
让调用方走降级路径（备份源 / 缓存）；冷却结束后进入 half-open，
首次调用成功立即恢复，失败则重置为 open。

使用：
    from core.circuit_breaker import get_breaker

    cb = get_breaker('akshare', failure_threshold=3, cooldown_seconds=300)
    if cb.allow():
        try:
            df = ak.stock_zh_a_minute(...)
            cb.on_success()
        except Exception:
            cb.on_failure()
            return None
    else:
        # 熔断中 → 走备份源
        return _fetch_from_backup(...)
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class _BreakerState:
    failures: int = 0
    opened_at: float = 0.0   # epoch seconds; 0 = closed


class CircuitBreaker:
    """单一外部依赖的熔断器。线程安全。"""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        cooldown_seconds: float = 300.0,
        on_open=None,    # callable(name) → 可选，触发熔断时回调（告警）
    ) -> None:
        self.name = name
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.001, float(cooldown_seconds))
        self._state = _BreakerState()
        self._lock = threading.Lock()
        self._on_open = on_open

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------

    def state(self) -> str:
        """返回 'closed' | 'open' | 'half_open'。"""
        with self._lock:
            return self._state_unlocked()

    def _state_unlocked(self) -> str:
        st = self._state
        if st.opened_at == 0.0:
            return 'closed'
        elapsed = time.time() - st.opened_at
        if elapsed >= self.cooldown_seconds:
            return 'half_open'
        return 'open'

    def allow(self) -> bool:
        """是否允许此次调用。open 状态返回 False。"""
        with self._lock:
            return self._state_unlocked() != 'open'

    # ------------------------------------------------------------------
    # 状态转移
    # ------------------------------------------------------------------

    def on_success(self) -> None:
        with self._lock:
            self._state.failures = 0
            self._state.opened_at = 0.0

    def on_failure(self) -> None:
        with self._lock:
            st = self._state
            cur_state = self._state_unlocked()
            # half_open 状态下失败 → 立即重新熔断
            if cur_state == 'half_open':
                st.opened_at = time.time()
                if self._on_open:
                    try:
                        self._on_open(self.name)
                    except Exception:
                        pass
                return
            st.failures += 1
            if st.failures >= self.failure_threshold and st.opened_at == 0.0:
                st.opened_at = time.time()
                if self._on_open:
                    try:
                        self._on_open(self.name)
                    except Exception:
                        pass

    def reset(self) -> None:
        """手动重置（测试用）。"""
        with self._lock:
            self._state.failures = 0
            self._state.opened_at = 0.0


# ---------------------------------------------------------------------------
# 全局注册表
# ---------------------------------------------------------------------------

_REGISTRY: Dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def get_breaker(
    name: str,
    failure_threshold: int = 3,
    cooldown_seconds: float = 300.0,
    on_open=None,
) -> CircuitBreaker:
    """获取或创建命名熔断器。同名实例只创建一次（首次调用配置生效）。"""
    with _REGISTRY_LOCK:
        cb = _REGISTRY.get(name)
        if cb is None:
            cb = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
                on_open=on_open,
            )
            _REGISTRY[name] = cb
        return cb


def reset_all() -> None:
    """重置全部熔断器（测试用）。"""
    with _REGISTRY_LOCK:
        for cb in _REGISTRY.values():
            cb.reset()


def all_states() -> Dict[str, str]:
    """所有熔断器当前状态（监控用）。"""
    with _REGISTRY_LOCK:
        return {name: cb.state() for name, cb in _REGISTRY.items()}


__all__ = [
    'CircuitBreaker',
    'get_breaker',
    'reset_all',
    'all_states',
]
