# -*- coding: utf-8 -*-
"""
base_fetcher.py — 数据源抽象基类
================================

设计模式：策略模式 (Strategy Pattern)

职责：
  1. 统一定义日线数据获取接口
  2. 提供标准化流程模板（fetch → normalize → clean → 计算技术指标）
  3. 内置随机休眠防封禁

子类实现：
  - _fetch_raw_data()    — 从具体数据源获取原始 DataFrame
  - _normalize_data()    — 将原始列名映射为标准列名
  - _get_source_name()   — 返回数据源名称字符串

标准化列名（所有子类必须输出）：
  date, open, high, low, close, volume, amount, pct_chg

Usage:
  class TencentFetcher(BaseFetcher):
      name = "TencentFetcher"
      priority = 0  # 越小越优先

      def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
          ...  # 实现具体 HTTP 调用
          return raw_df

      def _normalize_data(self, df: pd.DataFrame) -> pd.DataFrame:
          ...  # 列名映射
          return normalized_df
"""

import logging
import random
import time
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Any

import pandas as pd

from .data_fetch_exceptions import DataFetchError, RateLimitError, DataSourceUnavailableError
from .circuit_breaker import CircuitBreaker

logger = logging.getLogger('base_fetcher')

# 标准化列名定义
STANDARD_COLUMNS = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']


def safe_float(val: Any, default: Optional[float] = None) -> Optional[float]:
    """
    安全转换为浮点数。

    处理场景：
    - None / 空字符串 / "-" / "--" → default
    - pandas NaN / numpy NaN → default
    - 数值字符串 → float
    - 已是数值 → float
    """
    if val is None:
        return default
    try:
        if isinstance(val, str):
            val = val.strip()
            if val == "" or val in ("-", "--"):
                return default
        # 处理 pandas/numpy NaN
        try:
            import math
            if isinstance(val, (int, float)) and (val != val):  # NaN check
                return default
        except (ValueError, TypeError):
            pass
        result = float(val)
        # NaN check
        if result != result:  # faster isnan
            return default
        return result
    except (ValueError, TypeError):
        return default


def safe_int(val: Any, default: Optional[int] = None) -> Optional[int]:
    """安全转换为整数（先转 float 再取整）。"""
    f_val = safe_float(val, default=None)
    if f_val is not None:
        return int(f_val)
    return default


def normalize_stock_code(code: str) -> str:
    """
    标准化股票代码（去交易所前后缀）。

    Examples:
        '600519'    -> '600519'   (already clean)
        'SH600519'  -> '600519'   (strip SH prefix)
        'SZ000001'  -> '000001'   (strip SZ prefix)
        '600519.SH' -> '600519'   (strip .SH suffix)
        '000001.SZ' -> '000001'   (strip .SZ suffix)
    """
    code = code.strip().upper()
    # Strip prefix (SH/SZ/BJ)
    if code.startswith(('SH', 'SZ', 'BJ')) and len(code) > 6:
        code = code[2:]
    # Strip suffix (.SH/.SZ/.BJ/.HK)
    if '.' in code:
        parts = code.rsplit('.', 1)
        if parts[0].isdigit():
            code = parts[0]
    return code


class BaseFetcher(ABC):
    """
    数据源抽象基类。

    子类必须：
      1. 设置 name 和 priority 类属性
      2. 实现 _fetch_raw_data()
      3. 实现 _normalize_data()
    """

    name: str = "BaseFetcher"
    priority: int = 99  # 越小越优先

    # 熔断器（类级别共享，所有实例共用同一个熔断状态）
    _circuit_breaker: CircuitBreaker = CircuitBreaker(
        failure_threshold=3,
        cooldown_seconds=300.0,
    )

    def __init__(self):
        self._last_request_time: Optional[float] = None

    # ── 子类必须实现 ─────────────────────────────────────────────────

    @abstractmethod
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从具体数据源获取原始数据（子类必须实现）。

        Args:
            stock_code: 标准化后的股票代码，如 '600519'
            start_date: 开始日期，格式 'YYYY-MM-DD'
            end_date: 结束日期，格式 'YYYY-MM-DD'

        Returns:
            原始 DataFrame（列名因数据源而异）

        Raises:
            RateLimitError: 触发频率限制
            DataSourceUnavailableError: 数据源不可用（403/封禁/连接断开）
            DataFetchError: 其他获取异常
        """
        pass

    @abstractmethod
    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        标准化数据列名（子类必须实现）。

        必须将原始列名映射为标准列名：
        ['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']

        Args:
            df: 原始 DataFrame
            stock_code: 股票代码（用于日志和特殊处理）

        Returns:
            标准化后的 DataFrame
        """
        pass

    # ── 统一数据获取入口（模板方法）────────────────────────────────

    def get_daily_data(
        self,
        stock_code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        days: int = 30,
    ) -> pd.DataFrame:
        """
        获取日线数据（统一入口模板方法）。

        流程：
          1. 计算日期范围
          2. 休眠防封禁（子类控制间隔）
          3. 调用 _fetch_raw_data()
          4. 调用 _normalize_data()
          5. 数据清洗
          6. 计算技术指标（MA / volume_ratio）

        Args:
            stock_code: 股票代码
            start_date: 开始日期（可选）
            end_date: 结束日期（可选，默认今天）
            days: 获取天数（当 start_date 未指定时使用）

        Returns:
            标准化的 DataFrame（包含技术指标）

        Raises:
            DataFetchError: 所有获取/解析异常
        """
        # 计算日期范围
        if end_date is None:
            end_date = datetime.now().strftime('%Y-%m-%d')

        if start_date is None:
            from datetime import timedelta
            start_dt = datetime.strptime(end_date, '%Y-%m-%d') - timedelta(days=days * 2)
            start_date = start_dt.strftime('%Y-%m-%d')

        request_start = time.time()
        logger.info("[%s] 获取 %s 日线数据: %s ~ %s", self.name, stock_code, start_date, end_date)

        try:
            # Step 1: 获取原始数据
            raw_df = self._fetch_raw_data(stock_code, start_date, end_date)

            if raw_df is None or raw_df.empty:
                raise DataFetchError(
                    f"[{self.name}] 未获取到 {stock_code} 的数据",
                    source=self.name, stock_code=stock_code
                )

            # Step 2: 标准化列名
            df = self._normalize_data(raw_df, stock_code)

            # Step 3: 数据清洗
            df = self._clean_data(df)

            # Step 4: 计算技术指标
            df = self._calculate_indicators(df)

            elapsed = time.time() - request_start
            logger.info("[%s] %s 获取成功: %d 行, elapsed=%.2fs",
                        self.name, stock_code, len(df), elapsed)
            return df

        except (RateLimitError, DataSourceUnavailableError):
            # 这两类异常让 FetcherManager 决定是否切换
            raise
        except DataFetchError:
            raise
        except Exception as e:
            elapsed = time.time() - request_start
            logger.error("[%s] %s 获取异常: elapsed=%.2fs, %s",
                         self.name, stock_code, elapsed, e)
            raise DataFetchError(
                f"[{self.name}] {stock_code}: {e}",
                source=self.name, stock_code=stock_code
            ) from e

    # ── 数据清洗 ────────────────────────────────────────────────────

    def _clean_data(self, df: pd.DataFrame) -> pd.DataFrame:
        """数据清洗：日期格式 / 数值类型 / 去空值 / 排序"""
        df = df.copy()

        # 日期列
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], errors='coerce')

        # 数值列类型转换
        numeric_cols = ['open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']
        for col in numeric_cols:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 去除关键列为空的行
        df = df.dropna(subset=['close', 'volume'])

        # 按日期升序
        df = df.sort_values('date', ascending=True).reset_index(drop=True)

        return df

    # ── 技术指标 ────────────────────────────────────────────────────

    def _calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        计算技术指标。

        计算：
          MA5, MA10, MA20 — 移动平均线
          volume_ratio    — 量比（当日量 / 前5日均量）
        """
        df = df.copy()

        df['ma5'] = df['close'].rolling(window=5, min_periods=1).mean()
        df['ma10'] = df['close'].rolling(window=10, min_periods=1).mean()
        df['ma20'] = df['close'].rolling(window=20, min_periods=1).mean()

        # 量比 = 当日成交量 / 前5日均量（shift 1 避免未来数据泄露）
        avg_vol_5 = df['volume'].rolling(window=5, min_periods=1).mean()
        df['volume_ratio'] = (df['volume'] / avg_vol_5.shift(1)).fillna(1.0)

        # 保留2位小数
        for col in ['ma5', 'ma10', 'ma20', 'volume_ratio']:
            if col in df.columns:
                df[col] = df[col].round(2)

        return df

    # ── 防封禁工具 ──────────────────────────────────────────────────

    @staticmethod
    def random_sleep(min_seconds: float = 1.0, max_seconds: float = 4.0) -> None:
        """
        智能随机休眠（Jitter）。

        防封禁：模拟人类行为的随机延迟，降低被识别为爬虫的概率。
        在请求之间加入不规则的等待时间。
        """
        sleep_time = random.uniform(min_seconds, max_seconds)
        logger.debug("随机休眠 %.2f 秒...", sleep_time)
        time.sleep(sleep_time)

    def controlled_sleep(self) -> None:
        """
        子类可控的请求间隔。

        默认：随机 2~5 秒
        子类可重写以调整间隔（如 Tushare 限流更严格，可以设 3~8 秒）
        """
        self.random_sleep(2.0, 5.0)
