"""
cooldown.py — 信号推送冷却追踪器。
"""

import time


COOLDOWN = 900  # 同一标的信号推送冷却时间(15分钟)


class CooldownTracker:
    """防止同一标的信号在 COOLDOWN 秒内重复推送。"""

    def __init__(self, cooldown: int = COOLDOWN):
        self._cooldown = cooldown
        self._last: dict[str, float] = {}

    def can_fire(self, symbol: str) -> bool:
        now = time.time()
        last = self._last.get(symbol, 0)
        if now - last < self._cooldown:
            return False
        self._last[symbol] = now
        return True

    def purge_old(self):
        now = time.time()
        self._last = {k: v for k, v in self._last.items() if now - v < self._cooldown}
