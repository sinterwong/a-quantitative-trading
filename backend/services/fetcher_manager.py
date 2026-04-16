# -*- coding: utf-8 -*-
"""
fetcher_manager.py — 数据源策略管理器
=====================================

设计模式：策略模式 + 迭代式 Failover

职责：
  1. 按 priority 排序管理多个 BaseFetcher 实例
  2. 遍历 fetchers，任一成功即返回
  3. 失败时通过 CircuitBreaker 判断是否继续切换
  4. 所有数据源均失败时抛出 DataFetchError（含各源错误详情）

熔断策略：
  - 连续 3 次失败 → 熔断 5 分钟（跳过该源）
  - 触发 429 限流 → 直接熔断
  - 其他源成功 → 重置所有源的熔断状态

Usage:
  from fetcher_manager import DataFetcherManager

  fm = DataFetcherManager()
  df = fm.get_daily_data("600900", days=30)
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .base_fetcher import BaseFetcher
from .circuit_breaker import CircuitBreaker
from .data_fetch_exceptions import DataFetchError, RateLimitError, DataSourceUnavailableError

logger = logging.getLogger('fetcher_manager')

# ─── 全局 FetcherManager 单例 ──────────────────────────────────────────────

_global_manager: Optional['DataFetcherManager'] = None


def get_fetcher_manager() -> 'DataFetcherManager':
    """获取全局 DataFetcherManager 单例（延迟创建）"""
    global _global_manager
    if _global_manager is None:
        _global_manager = DataFetcherManager()
    return _global_manager


def reset_fetcher_manager() -> None:
    """重置全局单例（用于测试或配置变更后重载）"""
    global _global_manager
    if _global_manager is not None:
        _global_manager._circuit_breaker.reset()
    _global_manager = None


class DataFetcherManager:
    """
    多数据源策略管理器。

    核心逻辑：
      for fetcher in self._fetchers (按 priority 升序):
          if not circuit_breaker.is_available(fetcher.name):
              continue  # 熔断中，跳过
          try:
              return fetcher.get_daily_data(...)
          except RateLimitError:
              circuit_breaker.record_rate_limit()  # 立即熔断
              continue    # 尝试下一个
          except DataSourceUnavailableError:
              circuit_breaker.record_failure()
              if circuit_breaker.is_available(fetcher.name):
                  continue  # 未达熔断阈值，尝试重试
              break         # 已熔断，不再尝试
          except DataFetchError:
              circuit_breaker.record_failure()
              continue    # 切换下一数据源
      raise DataFetchError("所有数据源均失败")
    """

    def __init__(self, fetchers: Optional[List[BaseFetcher]] = None):
        """
        初始化管理器。

        Args:
            fetchers: BaseFetcher 实例列表（可选，默认自动发现）
                     若提供，按 priority 排序；空列表则不注册任何 fetcher。
        """
        self._fetchers: List[BaseFetcher] = []
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=3,
            cooldown_seconds=300.0,
        )
        if fetchers is not None:
            self._fetchers = sorted(fetchers, key=lambda f: f.priority)
        else:
            self._auto_register()

        logger.info("[FetcherManager] 初始化，注册的 fetchers: %s",
                    [(f.name, f.priority) for f in self._fetchers])

    def _auto_register(self) -> None:
        """自动发现并注册所有可用的 fetcher"""
        from .fetchers import TencentFetcher, SinaFetcher, AkshareFetcher

        # 按 priority 顺序注册
        # 始终按固定顺序，便于排查问题
        registered: Dict[str, bool] = {}

        for fetcher_cls in [TencentFetcher, SinaFetcher, AkshareFetcher]:
            name = fetcher_cls.name
            if name in registered:
                continue
            try:
                instance = fetcher_cls()
                self._fetchers.append(instance)
                registered[name] = True
                logger.debug("[FetcherManager] 注册 %s (priority=%d)", name, instance.priority)
            except ImportError as e:
                logger.warning("[FetcherManager] 跳过 %s（未安装）: %s", name, e)
            except Exception as e:
                logger.warning("[FetcherManager] 跳过 %s（初始化失败）: %s", name, e)

        self._fetchers.sort(key=lambda f: f.priority)

    # ── 公开 API ────────────────────────────────────────────────────────

    def get_daily_data(
        self,
        stock_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: int = 30,
    ) -> pd.DataFrame:
        """
        获取日线数据（Failover 统一入口）。

        按 priority 顺序尝试各 fetcher，任一成功即返回。
        全部失败时抛出 DataFetchError（含各源错误原因）。

        Args:
            stock_code: 股票代码（支持 SH/SZ 前缀和 .SH/.SZ 后缀）
            start_date: 开始日期（可选，默认最近 days 个交易日）
            end_date: 结束日期（可选，默认今天）
            days: 获取天数（start_date 未指定时生效）

        Returns:
            标准化的 DataFrame（包含 date/open/high/low/close/volume/amount/pct_chg/ma5/ma10/ma20/volume_ratio）

        Raises:
            DataFetchError: 所有数据源均失败
        """
        errors: List[Tuple[str, str]] = []  # [(source, reason)]

        # 默认日期处理
        if end_date is None:
            end_date = datetime.now().strftime('%Y%m%d')
        if start_date is None:
            start_date = (datetime.now() - timedelta(days=days)).strftime('%Y%m%d')

        for fetcher in self._fetchers:
            if not self._circuit_breaker.is_available(fetcher.name):
                logger.info("[FetcherManager] %s 熔断中，跳过", fetcher.name)
                errors.append((fetcher.name, "熔断中"))
                continue

            try:
                df = fetcher.get_daily_data(
                    stock_code=stock_code,
                    start_date=start_date,
                    end_date=end_date,
                    days=days,
                )
                # 成功：重置所有 fetcher 的熔断状态
                self._circuit_breaker.record_success(fetcher.name)
                # 重置其他 fetcher 的熔断（一个成功说明网络正常）
                for _f in self._fetchers:
                    if _f.name != fetcher.name:
                        self._circuit_breaker.record_success(_f.name)
                logger.info("[FetcherManager] %s 成功获取 %s: %d 行",
                           fetcher.name, stock_code, len(df))
                return df

            except RateLimitError as e:
                # 限流：立即熔断，切换下一数据源
                self._circuit_breaker.record_rate_limit(fetcher.name)
                errors.append((fetcher.name, f"限流: {e}"))
                logger.warning("[FetcherManager] %s 触发限流 → 熔断，切换下一数据源", fetcher.name)
                continue

            except DataSourceUnavailableError as e:
                # 不可用：记录失败，可能触发熔断
                self._circuit_breaker.record_failure(fetcher.name)
                errors.append((fetcher.name, str(e)))
                if self._circuit_breaker.is_available(fetcher.name):
                    # 未达熔断阈值，再尝试一次该 fetcher
                    logger.warning("[FetcherManager] %s 失败（还可重试）: %s", fetcher.name, e)
                    try:
                        df = fetcher.get_daily_data(
                            stock_code=stock_code,
                            start_date=start_date,
                            end_date=end_date,
                            days=days,
                        )
                        self._circuit_breaker.record_success(fetcher.name)
                        return df
                    except Exception:
                        self._circuit_breaker.record_failure(fetcher.name)
                        errors[-1] = (fetcher.name, str(e) + " (重试失败)")
                else:
                    logger.warning("[FetcherManager] %s 已熔断: %s", fetcher.name, e)
                continue

            except DataFetchError as e:
                # 数据获取异常（如空数据）：记录失败，切换下一数据源
                self._circuit_breaker.record_failure(fetcher.name)
                errors.append((fetcher.name, str(e)))
                logger.warning("[FetcherManager] %s 获取异常: %s", fetcher.name, e)
                continue

        # 所有 fetcher 均失败
        error_summary = "; ".join([f"{s}: {r}" for s, r in errors])
        logger.error("[FetcherManager] 所有数据源均失败 %s: %s", stock_code, error_summary)
        raise DataFetchError(
            f"所有数据源均失败: {error_summary}",
            stock_code=stock_code
        )

    def get_fetcher_status(self) -> List[Dict]:
        """返回所有 fetcher 的熔断状态快照（调试用）"""
        return [
            self._circuit_breaker.get_status(f.name)
            for f in self._fetchers
        ]

    @property
    def fetchers(self) -> List[BaseFetcher]:
        """返回当前注册的所有 fetcher（按 priority 排序）"""
        return list(self._fetchers)
