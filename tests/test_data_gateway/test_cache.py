# -*- coding: utf-8 -*-
"""
cache.py 单元测试 — MemoryCache + ParquetDiskCache。
"""

import os
import time

import pandas as pd
import pytest

from core.data_gateway.cache import MemoryCache, ParquetDiskCache


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
