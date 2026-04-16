# -*- coding: utf-8 -*-
"""
data_fetch_exceptions.py — 数据获取统一异常类型
================================================

三层异常体系：
  DataFetchError        — 通用数据获取异常（所有子类的基类）
  RateLimitError        — API 频率限制（429 / 触发限流）
  DataSourceUnavailableError  — 数据源不可用（403 / 封禁 / 连接断开）

设计原则：
- 所有数据获取层抛出的异常必须归类到以上三类
- FetcherManager 根据异常类型决定是否切换数据源
- 避免笼统的 Exception，便于精准处理
"""


class DataFetchError(Exception):
    """数据获取通用异常基类"""
    def __init__(self, message: str, source: str = None, stock_code: str = None):
        super().__init__(message)
        self.source = source        # 数据源名称，如 "TencentFetcher"
        self.stock_code = stock_code  # 股票代码


class RateLimitError(DataFetchError):
    """API 频率限制异常（HTTP 429 或触发了源端限流）"""
    def __init__(self, message: str, source: str = None, stock_code: str = None,
                 retry_after: float = None):
        super().__init__(message, source=source, stock_code=stock_code)
        self.retry_after = retry_after  # 建议等待秒数


class DataSourceUnavailableError(DataFetchError):
    """数据源不可用异常（HTTP 403 / 封禁 / 连接断开 / 超时 / 源端明确拒绝）"""
    pass
