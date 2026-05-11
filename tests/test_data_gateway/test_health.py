# -*- coding: utf-8 -*-
"""
health.py 单元测试 — 滑窗成功率 + 延迟评分。
"""

import time

from core.data_gateway.capabilities import Capability
from core.data_gateway.health import (
    HealthTracker,
    get_health_tracker,
    reset_health_tracker,
)


# ── 冷启动 ───────────────────────────────────────────────────────────────────


def test_cold_start_uses_priority_hint():
    t = HealthTracker()
    # 无任何记录,使用 priority_hint
    assert t.score("tencent", Capability.QUOTE, priority_hint=0.8) == 0.8
    assert t.score("foo", Capability.QUOTE, priority_hint=0.2) == 0.2


def test_cold_start_clamped():
    t = HealthTracker()
    assert t.score("x", Capability.QUOTE, priority_hint=-1.0) == 0.0
    assert t.score("x", Capability.QUOTE, priority_hint=2.0) == 1.0


# ── 成功率 ───────────────────────────────────────────────────────────────────


def test_full_success_score_high():
    t = HealthTracker(warmup_count=3)
    for _ in range(5):
        t.record("p", Capability.QUOTE, success=True, latency_ms=100)
    # 全成功 + 低延迟 → 接近 1
    assert t.score("p", Capability.QUOTE, priority_hint=0.1) > 0.9


def test_full_failure_score_low():
    t = HealthTracker(warmup_count=3)
    for _ in range(5):
        t.record("p", Capability.QUOTE, success=False, latency_ms=100)
    # 全失败 → 0
    assert t.score("p", Capability.QUOTE, priority_hint=0.8) == 0.0


def test_mixed_success_rate():
    t = HealthTracker(warmup_count=3)
    for _ in range(3):
        t.record("p", Capability.QUOTE, success=True, latency_ms=100)
    for _ in range(2):
        t.record("p", Capability.QUOTE, success=False, latency_ms=100)
    # 成功率 60%,低延迟 → 接近 60%
    s = t.score("p", Capability.QUOTE)
    assert 0.4 < s < 0.7


# ── 延迟权重 ─────────────────────────────────────────────────────────────────


def test_slow_provider_score_decreases():
    t = HealthTracker(warmup_count=3, latency_threshold_ms=1000)
    for _ in range(5):
        t.record("fast", Capability.QUOTE, success=True, latency_ms=50)
        t.record("slow", Capability.QUOTE, success=True, latency_ms=900)
    fast = t.score("fast", Capability.QUOTE)
    slow = t.score("slow", Capability.QUOTE)
    assert fast > slow
    assert fast > 0.9
    assert slow < 0.9


# ── Warmup 混合 ──────────────────────────────────────────────────────────────


def test_warmup_blends_with_hint():
    """warmup 期间分数应介于 priority_hint 和实测分数之间。"""
    t = HealthTracker(warmup_count=10)
    # 仅 2 次成功调用 → 远未 warmup
    for _ in range(2):
        t.record("p", Capability.QUOTE, success=True, latency_ms=50)
    s = t.score("p", Capability.QUOTE, priority_hint=0.0)
    # 实测分接近 1.0,hint 0.0,n=2,warmup=10 → w=0.2 → 0.2*1 + 0.8*0 ≈ 0.2
    assert 0.1 < s < 0.3


# ── 时间窗 ───────────────────────────────────────────────────────────────────


def test_old_events_drop_out_of_window():
    t = HealthTracker(window_seconds=0.1, warmup_count=2)
    for _ in range(3):
        t.record("p", Capability.QUOTE, success=False, latency_ms=100)
    time.sleep(0.15)
    # 窗外失败被丢弃,无数据 → 走 hint
    assert t.score("p", Capability.QUOTE, priority_hint=0.7) == 0.7


# ── 单例 ─────────────────────────────────────────────────────────────────────


def test_global_tracker_singleton():
    reset_health_tracker(None)
    a = get_health_tracker()
    b = get_health_tracker()
    assert a is b
    reset_health_tracker(None)


# ── 隔离 ─────────────────────────────────────────────────────────────────────


def test_provider_capability_pair_isolated():
    t = HealthTracker(warmup_count=1)
    t.record("a", Capability.QUOTE, success=False, latency_ms=100)
    t.record("b", Capability.QUOTE, success=True, latency_ms=100)
    t.record("a", Capability.KLINE_DAILY, success=True, latency_ms=100)
    assert t.score("a", Capability.QUOTE) == 0.0
    assert t.score("b", Capability.QUOTE) > 0.9
    assert t.score("a", Capability.KLINE_DAILY) > 0.9


def test_reset_clears():
    t = HealthTracker(warmup_count=1)
    t.record("p", Capability.QUOTE, success=True, latency_ms=10)
    assert t.score("p", Capability.QUOTE) > 0.5
    t.reset()
    assert t.score("p", Capability.QUOTE, priority_hint=0.3) == 0.3
