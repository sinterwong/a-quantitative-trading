# -*- coding: utf-8 -*-
"""
circuit_breaker.py — 熔断器
============================

状态机：
  CLOSED（正常）--连续失败N次--> OPEN（熔断，冷却中）
  OPEN --冷却时间到--> HALF_OPEN（半开，试探性请求）
  HALF_OPEN --成功--> CLOSED（恢复正常）
  HALF_OPEN --失败--> OPEN（再冷却）

参数：
  failure_threshold: 连续失败次数阈值（默认3次）
  cooldown_seconds:   冷却时间（默认300秒=5分钟）
  half_open_max_calls: 半开状态下最大尝试次数（默认1次）

Usage:
  cb = CircuitBreaker(failure_threshold=3, cooldown_seconds=300)
  
  if cb.is_available("TencentFetcher"):
      try:
          data = fetcher.fetch()
          cb.record_success("TencentFetcher")
      except DataSourceUnavailableError:
          cb.record_failure("TencentFetcher")
"""

import logging
import time
from threading import RLock
from typing import Dict, Any

from .data_fetch_exceptions import DataSourceUnavailableError, RateLimitError

logger = logging.getLogger('circuit_breaker')


class CircuitBreaker:
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 3,
        cooldown_seconds: float = 300.0,
        half_open_max_calls: int = 1,
    ):
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self.half_open_max_calls = half_open_max_calls

        # 各数据源状态: {source_name: {state, failures, last_failure_time, half_open_calls}}
        self._states: Dict[str, Dict[str, Any]] = {}
        self._lock = RLock()

    # ── 内部状态管理 ─────────────────────────────────────────────────

    def _get_state(self, source: str) -> Dict[str, Any]:
        if source not in self._states:
            self._states[source] = {
                'state': self.CLOSED,
                'failures': 0,
                'last_failure_time': 0.0,
                'half_open_calls': 0,
            }
        return self._states[source]

    # ── 公开 API ────────────────────────────────────────────────────

    def is_available(self, source: str) -> bool:
        """
        检查数据源是否可用（可以发起请求）。

        Returns:
            True  — 可以请求（CLOSED 或 HALF_OPEN 且还有尝试次数）
            False — 跳过请求（OPEN 冷却中，或 HALF_OPEN 尝试次数用完）
        """
        with self._lock:
            state = self._get_state(source)
            current_time = time.time()

            if state['state'] == self.CLOSED:
                return True

            if state['state'] == self.OPEN:
                # 检查冷却是否到期
                if current_time - state['last_failure_time'] >= self.cooldown_seconds:
                    # 冷却到期，转为 HALF_OPEN
                    state['state'] = self.HALF_OPEN
                    state['half_open_calls'] = 0
                    logger.info("[CircuitBreaker] %s: OPEN → HALF_OPEN（冷却结束）", source)
                    return True
                return False

            if state['state'] == self.HALF_OPEN:
                # 半开状态，最多允许 half_open_max_calls 次试探
                if state['half_open_calls'] < self.half_open_max_calls:
                    state['half_open_calls'] += 1
                    return True
                return False

            return True  # 兜底，正常请求

    def record_success(self, source: str) -> None:
        """请求成功：重置该数据源的失败计数，回到 CLOSED"""
        with self._lock:
            state = self._get_state(source)
            if state['state'] != self.CLOSED:
                logger.info("[CircuitBreaker] %s: %s → CLOSED（请求成功）",
                            source, state['state'])
            state['state'] = self.CLOSED
            state['failures'] = 0
            state['half_open_calls'] = 0

    def record_failure(self, source: str) -> None:
        """
        请求失败：增加失败计数，达到阈值则进入 OPEN 状态。
        同时记录最后失败时间（用于计算冷却到期）。
        """
        with self._lock:
            state = self._get_state(source)
            state['failures'] += 1
            state['last_failure_time'] = time.time()

            if state['state'] == self.HALF_OPEN:
                # 半开状态下失败，立即回到 OPEN
                state['state'] = self.OPEN
                logger.warning("[CircuitBreaker] %s: HALF_OPEN → OPEN（试探失败，再冷却%.0fs）",
                               source, self.cooldown_seconds)

            elif state['failures'] >= self.failure_threshold:
                state['state'] = self.OPEN
                logger.warning("[CircuitBreaker] %s: CLOSED → OPEN（连续%d次失败，冷却%.0fs）",
                               source, state['failures'], self.cooldown_seconds)

    def record_rate_limit(self, source: str, retry_after: float = None) -> None:
        """
        遇到频率限制：直接触发熔断，跳过冷却等待。
        retry_after 建议等待时间（秒），用于日志记录。
        """
        with self._lock:
            state = self._get_state(source)
            state['state'] = self.OPEN
            state['last_failure_time'] = time.time()
            wait = retry_after if retry_after else self.cooldown_seconds
            logger.warning("[CircuitBreaker] %s: 触发限流 → OPEN（建议等待%.0fs）",
                           source, wait)

    def get_status(self, source: str) -> Dict[str, Any]:
        """返回指定数据源的熔断状态快照（调试用）"""
        with self._lock:
            return dict(self._get_state(source), source=source,
                        available=self.is_available(source))

    def reset(self, source: str = None) -> None:
        """重置熔断器（全重置或指定 source）"""
        with self._lock:
            if source:
                if source in self._states:
                    self._states[source] = {
                        'state': self.CLOSED,
                        'failures': 0,
                        'last_failure_time': 0.0,
                        'half_open_calls': 0,
                    }
            else:
                self._states.clear()
