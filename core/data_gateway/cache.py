# -*- coding: utf-8 -*-
"""
data_gateway.cache — 统一缓存层

合并原先散落在 data_layer._TTLCache / ParquetCache 的实现:
  - MemoryCache: 线程安全的 TTL 内存缓存(任意可序列化对象)
  - ParquetDiskCache: K 线类 DataFrame 的本地落盘(增量更新)

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
        os.makedirs(self._root, exist_ok=True)

    def _path_for(self, key: str) -> str:
        import hashlib
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return os.path.join(self._root, f"{digest}.parquet")

    def get(self, key: str, ttl: Optional[float] = None) -> Optional[pd.DataFrame]:
        """读取缓存。超过 ttl 或不存在返回 None。"""
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
        try:
            for f in os.listdir(self._root):
                if f.endswith(".parquet"):
                    try:
                        os.remove(os.path.join(self._root, f))
                    except OSError:
                        pass
        except OSError:
            pass


__all__ = ["MemoryCache", "ParquetDiskCache"]
