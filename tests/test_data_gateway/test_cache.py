# -*- coding: utf-8 -*-
"""
cache.py 单元测试 — MemoryCache + ParquetDiskCache + TieredCache。
"""

import os
import time

import pandas as pd
import pytest

from core.data_gateway.cache import MemoryCache, ParquetDiskCache, TieredCache


# ── MemoryCache ──────────────────────────────────────────────────────────────


def test_memory_cache_miss():
    c = MemoryCache()
    assert c.get("k") is None


def test_memory_cache_hit():
    c = MemoryCache()
    c.set("k", {"v": 1}, ttl=10)
    assert c.get("k") == {"v": 1}


def test_memory_cache_expiry():
    c = MemoryCache(default_ttl=0.01)
    c.set("k", "v")
    time.sleep(0.05)
    assert c.get("k") is None


def test_memory_cache_custom_ttl_overrides_default():
    c = MemoryCache(default_ttl=0.01)
    c.set("k", "v", ttl=10)
    time.sleep(0.05)
    assert c.get("k") == "v"


def test_memory_cache_invalidate():
    c = MemoryCache()
    c.set("k", "v", ttl=10)
    c.invalidate("k")
    assert c.get("k") is None


def test_memory_cache_clear():
    c = MemoryCache()
    c.set("a", 1, ttl=10)
    c.set("b", 2, ttl=10)
    c.clear()
    assert len(c) == 0


def test_memory_cache_max_entries_evicts():
    c = MemoryCache(max_entries=2)
    c.set("a", 1, ttl=10)
    time.sleep(0.001)
    c.set("b", 2, ttl=10)
    time.sleep(0.001)
    c.set("c", 3, ttl=10)
    # 最早过期的 "a" 被踢
    assert len(c) == 2
    assert c.get("a") is None


# ── ParquetDiskCache ─────────────────────────────────────────────────────────


@pytest.fixture
def disk_cache(tmp_path):
    return ParquetDiskCache(str(tmp_path / "cache"))


def test_disk_cache_miss(disk_cache):
    assert disk_cache.get("nonexistent") is None


def test_disk_cache_roundtrip(disk_cache):
    df = pd.DataFrame({"date": ["2026-05-11"], "close": [10.5]})
    disk_cache.set("sh600519:daily", df)
    out = disk_cache.get("sh600519:daily")
    assert out is not None
    pd.testing.assert_frame_equal(out, df)


def test_disk_cache_empty_df_not_stored(disk_cache):
    disk_cache.set("k", pd.DataFrame())
    assert disk_cache.get("k") is None


def test_disk_cache_ttl_expiry(disk_cache, monkeypatch):
    df = pd.DataFrame({"x": [1]})
    disk_cache.set("k", df)
    # 模拟 mtime 倒退到很久以前
    path = disk_cache._path_for("k")
    old = time.time() - 100000
    os.utime(path, (old, old))
    assert disk_cache.get("k", ttl=60) is None


def test_disk_cache_invalidate(disk_cache):
    df = pd.DataFrame({"x": [1]})
    disk_cache.set("k", df)
    disk_cache.invalidate("k")
    assert disk_cache.get("k") is None


def test_disk_cache_clear(disk_cache):
    disk_cache.set("a", pd.DataFrame({"x": [1]}))
    disk_cache.set("b", pd.DataFrame({"x": [2]}))
    disk_cache.clear()
    assert disk_cache.get("a") is None
    assert disk_cache.get("b") is None


def test_disk_cache_different_keys_dont_collide(disk_cache):
    disk_cache.set("a", pd.DataFrame({"v": [1]}))
    disk_cache.set("b", pd.DataFrame({"v": [2]}))
    assert disk_cache.get("a")["v"].iloc[0] == 1
    assert disk_cache.get("b")["v"].iloc[0] == 2


def test_disk_cache_preserves_datetime_index(disk_cache):
    """回归：to_parquet(index=False) 会丢 DatetimeIndex 导致 macro/日 K 时间
    轴变成整数。此测试锁定 set→get round-trip 保留 DatetimeIndex。"""
    idx = pd.date_range("2026-01-01", periods=3, freq="D")
    df = pd.DataFrame({"v": [1.0, 2.0, 3.0]}, index=idx)
    df.index.name = "date"
    disk_cache.set("k", df)
    out = disk_cache.get("k")
    assert out is not None
    assert isinstance(out.index, pd.DatetimeIndex)
    pd.testing.assert_index_equal(out.index, idx)


# ── TieredCache ──────────────────────────────────────────────────────────────


@pytest.fixture
def tiered(tmp_path):
    return TieredCache(
        memory=MemoryCache(default_ttl=10.0),
        disk=ParquetDiskCache(str(tmp_path / "tier_cache")),
    )


def test_tiered_l1_hit_does_not_touch_disk(tiered):
    """L1 命中时不应触发 L2 读取(性能优化)。"""
    df = pd.DataFrame({"x": [1, 2]})
    tiered.set("k", df, ttl=10, persistent=True)
    # 偷偷干掉磁盘文件
    tiered._disk.invalidate("k")
    # 还能从 L1 命中
    pd.testing.assert_frame_equal(tiered.get("k", disk_ttl=10), df)


def test_tiered_l1_miss_falls_back_to_l2(tiered):
    """L1 过期或被清除后，L2 仍能提供数据。"""
    df = pd.DataFrame({"x": [3, 4]})
    tiered.set("k", df, ttl=0.01, persistent=True)
    time.sleep(0.05)
    assert tiered._memory.get("k") is None    # L1 真的过期了
    out = tiered.get("k", disk_ttl=86400)
    pd.testing.assert_frame_equal(out, df)


def test_tiered_l1_miss_without_disk_ttl_returns_none(tiered):
    """未传 disk_ttl 时不查 L2(默认行为，调用方需显式启用 L2 fallback)。"""
    df = pd.DataFrame({"x": [1]})
    tiered.set("k", df, ttl=0.01, persistent=True)
    time.sleep(0.05)
    assert tiered.get("k") is None    # 没传 disk_ttl


def test_tiered_l2_refill_warms_l1(tiered):
    """L2 命中后应回填 L1，下次直接走 L1。"""
    df = pd.DataFrame({"x": [5]})
    tiered.set("k", df, ttl=0.01, persistent=True)
    time.sleep(0.05)
    tiered.get("k", disk_ttl=86400)    # L2 → L1 回填
    # 立即再 get，不依赖 disk_ttl 也能命中
    assert tiered._memory.get("k") is not None


def test_tiered_non_persistent_only_writes_l1(tiered):
    """persistent=False 时不写 L2。"""
    df = pd.DataFrame({"x": [1]})
    tiered.set("k", df, ttl=10, persistent=False)
    assert tiered._memory.get("k") is not None
    assert tiered._disk.get("k", ttl=86400) is None


def test_tiered_non_dataframe_never_persists(tiered):
    """非 DataFrame(如 Quote dataclass / list)即使 persistent=True 也不进 L2。"""
    tiered.set("quote", {"price": 100.0}, ttl=30, persistent=True)
    assert tiered._memory.get("quote") == {"price": 100.0}
    assert tiered._disk.get("quote", ttl=86400) is None


def test_tiered_invalidate_clears_both_layers(tiered):
    df = pd.DataFrame({"x": [1]})
    tiered.set("k", df, ttl=10, persistent=True)
    tiered.invalidate("k")
    assert tiered._memory.get("k") is None
    assert tiered._disk.get("k", ttl=86400) is None


def test_tiered_clear_wipes_both(tiered):
    tiered.set("a", pd.DataFrame({"x": [1]}), ttl=10, persistent=True)
    tiered.set("b", pd.DataFrame({"x": [2]}), ttl=10, persistent=True)
    tiered.clear()
    assert tiered.get("a", disk_ttl=86400) is None
    assert tiered.get("b", disk_ttl=86400) is None


def test_tiered_without_disk_degrades_to_memory_only(tmp_path):
    """disk=None 时所有 persistent 写入都只走 L1。"""
    c = TieredCache(memory=MemoryCache(default_ttl=10.0), disk=None)
    df = pd.DataFrame({"x": [1]})
    c.set("k", df, persistent=True)
    assert c._memory.get("k") is not None
    # disk_ttl 传了也无 L2 可查
    c._memory.invalidate("k")
    assert c.get("k", disk_ttl=86400) is None


