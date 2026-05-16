"""
cooldown.py — 信号推送冷却追踪器。
"""

import threading
import time


COOLDOWN = 900  # 同一标的信号推送冷却时间(15分钟)


class CooldownTracker:
    """防止同一标的信号在 COOLDOWN 秒内重复推送。

    可被 monitor 后台线程与 API 线程同时访问(``len(tracker._last)`` 走 get_status),
    所以所有读写都加锁。
    """

    def __init__(self, cooldown: int = COOLDOWN):
        self._cooldown = cooldown
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def can_fire(self, symbol: str) -> bool:
        now = time.time()
        with self._lock:
            last = self._last.get(symbol, 0)
            if now - last < self._cooldown:
                return False
            self._last[symbol] = now
            return True

    def purge_old(self):
        now = time.time()
        with self._lock:
            self._last = {k: v for k, v in self._last.items() if now - v < self._cooldown}

    def size(self) -> int:
        """活跃冷却记录数(供 get_status 用,不持有外部锁)。"""
        with self._lock:
            return len(self._last)
