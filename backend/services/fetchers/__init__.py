# -*- coding: utf-8 -*-
"""
fetchers/ — 多源数据获取层
==========================

按优先级排序的数据源：
  0. TencentFetcher   — 腾讯财经（最优先）
  1. SinaFetcher     — 新浪财经
  2. AkshareFetcher  — AkShare（封装/备用）

每个 Fetcher 都是 BaseFetcher 的子类，
实现统一的 get_daily_data() 接口，
由 DataFetcherManager 按优先级自动故障切换。

Usage:
  from fetchers import get_fetcher_manager

  fm = get_fetcher_manager()
  df = fm.get_daily_data("600900", days=30)
"""

from .tencent_fetcher import TencentFetcher
from .sina_fetcher import SinaFetcher
from .akshare_fetcher import AkshareFetcher

__all__ = [
    'TencentFetcher',
    'SinaFetcher',
    'AkshareFetcher',
]
