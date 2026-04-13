"""
cache.py — LLM 响应缓存管理器
==============================
支持内存 LRU 和磁盘持久化，防止重复 API 调用。
"""

import os
import json
import hashlib
import time
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CacheEntry:
    key: str
    value: str
    ttl: int          # Unix timestamp when this entry expires
    created_at: float  # Unix timestamp when created

    def is_expired(self) -> bool:
        return time.time() > self.ttl


class CacheManager:
    """
    两级缓存：内存 LRU + 磁盘持久化。

    策略：
    - 内存缓存：保留最新 N 条，避免磁盘 I/O
    - 磁盘缓存：持久化，重启后保留（TTL 到期后失效）
    - TTL = 0 表示永不过期
    """

    def __init__(
        self,
        cache_dir: str = ".llm_cache",
        memory_capacity: int = 200,
        default_ttl: int = 300,
    ):
        self.cache_dir = cache_dir
        self.memory_capacity = memory_capacity
        self.default_ttl = default_ttl

        # 内存 LRU
        self._memory: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = threading.RLock()

        # 确保缓存目录存在
        os.makedirs(self.cache_dir, exist_ok=True)

    # ─── 公共接口 ────────────────────────────────

    def get(self, content: str, task: str = "default", ttl: Optional[int] = None) -> Optional[str]:
        """
        根据内容 hash 查找缓存。

        Args:
            content: 被缓存的原始内容（如新闻文本）
            task: 任务类型（不同任务隔离缓存）
            ttl: 覆盖默认 TTL（秒）

        Returns:
            缓存的响应内容，或 None（未命中/已过期）
        """
        key = self._make_key(content, task)
        now = time.time()

        # 1. 先查内存
        with self._lock:
            mem_entry = self._memory.get(key)
            if mem_entry and not mem_entry.is_expired():
                # 移到末尾（最新）
                self._memory.move_to_end(key)
                logger.debug("CACHE HIT (memory): %s", key[:16])
                return mem_entry.value
            elif mem_entry:
                # 已过期，从内存删除
                self._memory.pop(key, None)

        # 2. 查磁盘
        disk_entry = self._load_from_disk(key)
        if disk_entry:
            if not disk_entry.is_expired():
                # 回填内存
                with self._lock:
                    self._memory[key] = disk_entry
                    self._memory.move_to_end(key)
                    self._trim_memory()
                logger.debug("CACHE HIT (disk): %s", key[:16])
                return disk_entry.value
            else:
                # 已过期，删除磁盘文件
                self._remove_from_disk(key)

        logger.debug("CACHE MISS: %s", key[:16])
        return None

    def set(
        self,
        content: str,
        value: str,
        task: str = "default",
        ttl: Optional[int] = None,
    ):
        """
        写入缓存。

        Args:
            content: 原始内容（用于生成 key）
            value: 要缓存的响应内容
            task: 任务类型
            ttl: TTL 秒，None 则用 default_ttl
        """
        key = self._make_key(content, task)
        actual_ttl = ttl if ttl is not None else self.default_ttl
        now = time.time()

        entry = CacheEntry(
            key=key,
            value=value,
            ttl=now + actual_ttl,
            created_at=now,
        )

        # 写内存
        with self._lock:
            self._memory[key] = entry
            self._memory.move_to_end(key)
            self._trim_memory()

        # 写磁盘
        self._save_to_disk(entry)

    def invalidate(self, content: str, task: str = "default"):
        """手动清除指定缓存"""
        key = self._make_key(content, task)
        with self._lock:
            self._memory.pop(key, None)
        self._remove_from_disk(key)
        logger.info("CACHE INVALIDATE: %s", key[:16])

    def clear(self, task: Optional[str] = None):
        """
        清除缓存。

        Args:
            task: None 表示清除所有任务；str 表示只清除该任务类型
        """
        # 内存
        with self._lock:
            if task is None:
                self._memory.clear()
            else:
                to_remove = [k for k in self._memory if k.endswith(f':{task}')]
                for k in to_remove:
                    self._memory.pop(k, None)

        # 磁盘
        if self.cache_dir and os.path.isdir(self.cache_dir):
            for fname in os.listdir(self.cache_dir):
                if task is None or fname.endswith(f':{task}.json'):
                    try:
                        os.remove(os.path.join(self.cache_dir, fname))
                    except OSError:
                        pass

        logger.info("CACHE CLEAR: task=%s", task or 'all')

    # ─── 内部方法 ────────────────────────────────

    def _make_key(self, content: str, task: str) -> str:
        h = hashlib.sha256(content.encode('utf-8')).hexdigest()[:32]
        return f"{task}:{h}"

    def _disk_path(self, key: str) -> str:
        # 文件名格式：{task_hash}.json
        safe = key.replace(':', '_', 1)
        return os.path.join(self.cache_dir, f"{safe}.json")

    def _save_to_disk(self, entry: CacheEntry):
        try:
            path = self._disk_path(entry.key)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(asdict(entry), f)
        except Exception as e:
            logger.warning("Cache disk write failed: %s", e)

    def _load_from_disk(self, key: str) -> Optional[CacheEntry]:
        try:
            path = self._disk_path(key)
            if not os.path.exists(path):
                return None
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return CacheEntry(**data)
        except Exception as e:
            logger.warning("Cache disk read failed: %s", e)
            return None

    def _remove_from_disk(self, key: str):
        try:
            path = self._disk_path(key)
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def _trim_memory(self):
        """超过容量时淘汰最老的条目"""
        while len(self._memory) > self.memory_capacity:
            self._memory.popitem(last=False)
