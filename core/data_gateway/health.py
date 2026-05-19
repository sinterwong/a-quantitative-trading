# -*- coding: utf-8 -*-
"""
data_gateway.health — provider 健康度跟踪

按 (provider × capability) 维护滑动窗口的成功率和延迟,合成 [0, 1] 评分。
gateway 用此评分对候选 provider 排序,而不是硬编码主备路由。

与熔断器配合:
  - 熔断器(core.circuit_breaker)是硬开关 — open 状态直接禁用 provider
  - 健康度是软排序 — 在所有未熔断的 provider 中按分数排序选源

冷启动:
  - 没有历史数据时,使用 provider declare() 给出的 priority_hint 作为初始分
  - 第 N 次调用后(默认 5 次),完全基于实测数据
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple


@dataclass
class _Event:
    """单次调用记录。"""
    ts: float
    success: bool
    latency_ms: float


class HealthTracker:
    """provider × capability 健康度跟踪器。

    使用方式:
        tracker.record("tencent", Capability.QUOTE, success=True, latency_ms=85)
        score = tracker.score("tencent", Capability.QUOTE, priority_hint=0.8)

    评分公式:
        score = success_rate × (1 - latency_p95_norm)
        - success_rate: 窗口内成功比例
        - latency_p95_norm: p95 延迟归一化到 [0, 1](阈值默认 2000ms)
        - 不足 warmup_count 次时混合 priority_hint(线性过渡)
    """

    def __init__(
        self,
        *,
        window_size: int = 50,
        window_seconds: float = 600.0,
        warmup_count: int = 5,
        latency_threshold_ms: float = 2000.0,
    ):
        self._window_size = window_size
        self._window_seconds = window_seconds
        self._warmup = warmup_count
        self._latency_threshold = latency_threshold_ms
        self._events: Dict[Tuple[str, str], Deque[_Event]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(provider: str, capability) -> Tuple[str, str]:
        cap_name = capability.value if hasattr(capability, "value") else str(capability)
        return (provider, cap_name)

    def record(
        self,
        provider: str,
        capability,
        *,
        success: bool,
        latency_ms: float,
    ) -> None:
        key = self._key(provider, capability)
        ev = _Event(ts=time.time(), success=success, latency_ms=max(0.0, latency_ms))
        with self._lock:
            dq = self._events.get(key)
            if dq is None:
                dq = deque(maxlen=self._window_size)
                self._events[key] = dq
            dq.append(ev)

    def _live_events(self, key: Tuple[str, str]) -> list[_Event]:
        cutoff = time.time() - self._window_seconds
        with self._lock:
            dq = self._events.get(key)
            if dq is None:
                return []
            return [e for e in dq if e.ts >= cutoff]

    def score(
        self,
        provider: str,
        capability,
        *,
        priority_hint: float = 0.5,
    ) -> float:
        """返回 [0, 1] 的健康度评分。"""
        key = self._key(provider, capability)
        events = self._live_events(key)
        n = len(events)

        if n == 0:
            return max(0.0, min(1.0, priority_hint))

        success_count = sum(1 for e in events if e.success)
        success_rate = success_count / n

        # p95 延迟(只看成功调用 — 失败调用延迟无意义)
        ok_latencies = sorted(e.latency_ms for e in events if e.success)
        if ok_latencies:
            idx = max(0, int(len(ok_latencies) * 0.95) - 1)
            p95 = ok_latencies[idx]
            latency_norm = min(1.0, p95 / self._latency_threshold)
        else:
            latency_norm = 1.0  # 全失败 → 视为最慢

        measured = success_rate * (1.0 - latency_norm * 0.5)
        # latency_norm 系数 0.5: 延迟权重不超过 50%,成功率更重要

        if n >= self._warmup:
            return max(0.0, min(1.0, measured))

        # 冷启动:线性混合
        w = n / self._warmup
        return max(0.0, min(1.0, w * measured + (1 - w) * priority_hint))

    def snapshot(self) -> Dict[str, Dict[str, float]]:
        """监控用:返回 {provider: {capability: score}} 当前快照。"""
        out: Dict[str, Dict[str, float]] = {}
        with self._lock:
            keys = list(self._events.keys())
        for prov, cap in keys:
            out.setdefault(prov, {})[cap] = self.score(prov, type("X", (), {"value": cap}))
        return out

    def reset(self) -> None:
        with self._lock:
            self._events.clear()


# ─── 全局单例 ──────────────────────────────────────────────────────────────────


from core.singleton import LockedSingleton

_tracker_singleton: LockedSingleton[HealthTracker] = LockedSingleton(
    HealthTracker, name="health_tracker"
)


def get_health_tracker() -> HealthTracker:
    """获取全局 HealthTracker 单例(线程安全)。"""
    return _tracker_singleton.get()


def reset_health_tracker(tracker: Optional[HealthTracker] = None) -> None:
    """重置/替换全局单例(测试用)。"""
    _tracker_singleton.reset(tracker)


__all__ = ["HealthTracker", "get_health_tracker", "reset_health_tracker"]
