# -*- coding: utf-8 -*-
"""
data_gateway.cache — 统一缓存层

合并原先散落在 data_layer._TTLCache / ParquetCache 的实现:
  - MemoryCache: 线程安全的 TTL 内存缓存(任意可序列化对象)
  - ParquetDiskCache: K 线类 DataFrame 的本地落盘(增量更新)
  - TieredCache: 组合 L1 内存 + L2 落盘，重启不丢、跨进程复用

provider 可注入 cache 实例使用,或由 gateway 统一注入。
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional

import pandas as pd


# ─── 内存 TTL 缓存 ─────────────────────────────────────────────────────────────


class MemoryCache:
    """TTL 内存缓存(线程安全)。"""

    def __init__(self, default_ttl: float = 30.0, max_entries: int = 4096):
        self._default_ttl = default_ttl
        self._max_entries = max_entries
        self._store: Dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[Any]:
        """获取缓存值。未命中或过期返回 None。"""
        now = time.time()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expire_at, value = entry
            if now >= expire_at:
                self._store.pop(key, None)
                return None
            return value

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """写入缓存,ttl 秒后过期。"""
        expire_at = time.time() + (ttl if ttl is not None else self._default_ttl)
        with self._lock:
            if len(self._store) >= self._max_entries:
                # 简单 LRU 近似:踢掉最早过期的一条
                victim = min(self._store.items(), key=lambda kv: kv[1][0])[0]
                self._store.pop(victim, None)
            self._store[key] = (expire_at, value)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)


# ─── Parquet 落盘缓存(K 线类) ─────────────────────────────────────────────────


class ParquetDiskCache:
    """按 key 存储 DataFrame 的本地 Parquet 缓存。

    用于日 K / 分钟 K 等"历史增量"数据:相同 symbol+interval 重复请求时,
    可先读本地 parquet,再增量补当日,大幅降低对外网压力。

    设计:
      - 每个 key 一个 .parquet 文件,文件名 = sha1(key)
      - 通过 mtime 判定新鲜度,超过 ttl 视为需刷新
      - 不做并发合并(同 key 并发写最后写者赢,可接受)
    """

    def __init__(self, root_dir: str, default_ttl: float = 86400.0):
        self._root = os.path.abspath(root_dir)
        self._default_ttl = default_ttl
        # 目录懒创建：仅在第一次 set 时创建，避免无写入也产生空目录

    def _ensure_root(self) -> None:
        os.makedirs(self._root, exist_ok=True)

    def _path_for(self, key: str) -> str:
        import hashlib
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return os.path.join(self._root, f"{digest}.parquet")

    def get(self, key: str, ttl: Optional[float] = None) -> Optional[pd.DataFrame]:
        """读取缓存。超过 ttl 或不存在返回 None。"""
        if not os.path.isdir(self._root):
            return None
        path = self._path_for(key)
        if not os.path.exists(path):
            return None
        age = time.time() - os.path.getmtime(path)
        if age > (ttl if ttl is not None else self._default_ttl):
            return None
        try:
            return pd.read_parquet(path)
        except Exception:
            return None

    def set(self, key: str, df: pd.DataFrame) -> None:
        """写入缓存。空 DataFrame 不写盘。"""
        if df is None or df.empty:
            return
        self._ensure_root()
        path = self._path_for(key)
        try:
            df.to_parquet(path, index=False)
        except Exception:
            # 缓存失败不影响主流程
            pass

    def invalidate(self, key: str) -> None:
        path = self._path_for(key)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def clear(self) -> None:
        if not os.path.isdir(self._root):
            return
        try:
            for f in os.listdir(self._root):
                if f.endswith(".parquet"):
                    try:
                        os.remove(os.path.join(self._root, f))
                    except OSError:
                        pass
        except OSError:
            pass


# ─── 分层缓存(L1 内存 + L2 落盘) ───────────────────────────────────────────────


class TieredCache:
    """L1 = MemoryCache(毫秒级)，L2 = ParquetDiskCache(重启不丢、跨进程共享)。

    设计：
      - 仅 DataFrame 进 L2(parquet 不支持任意 Python 对象)
      - L1 miss → 查 L2 → 命中后回填 L1
      - L2 仅在 set 时按 capability 白名单写盘(避免 quote 等高频数据污染)
      - 调用方通过 `persistent=True` 明示哪些写入需要落盘

    用法:
        cache = TieredCache(memory=MemoryCache(), disk=ParquetDiskCache("data/cache/gw"))
        cache.set(key, df, ttl=86400, persistent=True)     # 写 L1 + L2
        cache.set(key, quote, ttl=30)                       # 只写 L1
        df = cache.get(key, disk_ttl=86400)                # L1 miss → 查 L2
    """

    def __init__(
        self,
        *,
        memory: "MemoryCache",
        disk: Optional["ParquetDiskCache"] = None,
    ):
        self._memory = memory
        self._disk = disk

    # ── 透传 MemoryCache 接口 ──────────────────────────────────────────────────

    def get(self, key: str, disk_ttl: Optional[float] = None) -> Optional[Any]:
        """L1 优先；L1 miss 时若开了 L2 且传了 disk_ttl，尝试从 L2 取 DataFrame。"""
        val = self._memory.get(key)
        if val is not None:
            return val
        if self._disk is None or disk_ttl is None:
            return None
        df = self._disk.get(key, ttl=disk_ttl)
        if df is None:
            return None
        # 回填 L1(用 disk_ttl 的 1/10，避免内存层过期窗口比 disk 还长)
        self._memory.set(key, df, ttl=max(60.0, disk_ttl / 10.0))
        return df

    def set(
        self,
        key: str,
        value: Any,
        ttl: Optional[float] = None,
        *,
        persistent: bool = False,
    ) -> None:
        """persistent=True 时同时写 L2(仅 DataFrame 落盘)。"""
        self._memory.set(key, value, ttl=ttl)
        if persistent and self._disk is not None and isinstance(value, pd.DataFrame):
            self._disk.set(key, value)

    def invalidate(self, key: str) -> None:
        self._memory.invalidate(key)
        if self._disk is not None:
            self._disk.invalidate(key)

    def clear(self) -> None:
        self._memory.clear()
        if self._disk is not None:
            self._disk.clear()

    # ── 兼容 MemoryCache 内部属性访问(gateway.invalidate_fundamentals_history 用到) ──

    @property
    def _store(self) -> Dict[str, Any]:
        return self._memory._store

    @property
    def _lock(self) -> threading.Lock:
        return self._memory._lock

    def __len__(self) -> int:
        return len(self._memory)


__all__ = ["MemoryCache", "ParquetDiskCache", "TieredCache"]
