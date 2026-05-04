"""
core/ipo_store.py — 历史新股数据库（Parquet）

功能：
  - 通过 AKShare 获取 A 股历史新股数据
  - 本地 Parquet 缓存（data/ipo/ipo_history.parquet），TTL=7 天
  - 提供标准化接口查询历史新股信息

数据列说明：
  symbol         : 股票代码（如 '001270.SZ'）
  name           : 股票名称
  ipo_date       : 上市日期
  issue_price    : 发行价（元）
  shares         : 发行数量（万股）
  pe_ratio       : 发行市盈率
  industry       : 所属行业
  market_type    : 交易市场（SH/SZ/BJ）

使用方式：
    store = IPOHistoryStore()
    df = store.get_ipo_history(start='2020-01-01', end='2024-12-31')
    # df 索引为上市日期，列为上述字段
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger('core.ipo_store')

_IPO_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'data', 'ipo'
)
os.makedirs(_IPO_DIR, exist_ok=True)

_PARQUET_PATH = os.path.join(_IPO_DIR, 'ipo_history.parquet')

# 缓存有效期：IPO 数据变更极少，7 天足够
_TTL_SECONDS = 7 * 86400


class IPOHistoryStore:
    """
    历史新股数据管理器。

    缓存策略：
      1. 内存缓存（进程生命周期内）
      2. 本地 Parquet（跨进程持久化，TTL=7 天）
      3. AKShare 网络获取（Parquet 过期或不存在时）
    """

    def __init__(self):
        self._memory_cache: Optional[pd.DataFrame] = None
        self._cache_timestamp: Optional[datetime] = None

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def get_ipo_history(
        self,
        start: Optional[str] = None,
        end: Optional[str] = None,
        symbol: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        获取历史新股列表（支持过滤）。

        Parameters
        ----------
        start  : 开始日期（'YYYY-MM-DD'），默认 2000-01-01
        end    : 结束日期（'YYYY-MM-DD'），默认今日
        symbol : 股票代码（如 '001270.SZ'），可选，精确过滤

        Returns
        -------
        pd.DataFrame
            DatetimeIndex（上市日期），列：
            symbol / name / ipo_date / issue_price / shares /
            pe_ratio / industry / market_type
            若获取失败返回空 DataFrame。
        """
        df = self._get_cached()
        if df is None or df.empty:
            return pd.DataFrame()

        # 日期过滤
        if start is not None:
            df = df[df['ipo_date'] >= start]
        if end is not None:
            df = df[df['ipo_date'] <= end]

        # 精确过滤
        if symbol is not None:
            df = df[df['symbol'] == symbol]

        return df

    def get_ipo_by_date(self, ipo_date: str) -> pd.DataFrame:
        """获取指定上市日期的新股（可能有多个）"""
        return self.get_ipo_history(start=ipo_date, end=ipo_date)

    def invalidate(self) -> None:
        """清除内存缓存（强制下次重新加载）"""
        self._memory_cache = None
        self._cache_timestamp = None

    # ------------------------------------------------------------------
    # 缓存逻辑
    # ------------------------------------------------------------------

    def _get_cached(self) -> Optional[pd.DataFrame]:
        """内存缓存 → Parquet 缓存 → 网络获取"""
        now = datetime.now()

        # 内存缓存检查
        if self._memory_cache is not None and self._cache_timestamp is not None:
            if (now - self._cache_timestamp).total_seconds() < _TTL_SECONDS:
                return self._memory_cache

        # Parquet 缓存检查
        df = self._load_parquet()
        if df is not None:
            self._memory_cache = df
            self._cache_timestamp = now
            return df

        # 网络获取
        df = self._fetch()
        if df is not None and not df.empty:
            self._save_parquet(df)
            self._memory_cache = df
            self._cache_timestamp = now
            return df

        return None

    def _load_parquet(self) -> Optional[pd.DataFrame]:
        """从本地 Parquet 加载数据"""
        if not os.path.exists(_PARQUET_PATH):
            return None
        try:
            df = pd.read_parquet(_PARQUET_PATH)
            logger.info('IPO history loaded from Parquet: %d rows', len(df))
            return df
        except Exception as e:
            logger.warning('Failed to load IPO Parquet: %s', e)
            return None

    def _save_parquet(self, df: pd.DataFrame) -> None:
        """保存数据到本地 Parquet"""
        try:
            df.to_parquet(_PARQUET_PATH, index=True)
            logger.info('IPO history saved to Parquet: %d rows', len(df))
        except Exception as e:
            logger.warning('Failed to save IPO Parquet: %s', e)

    # ------------------------------------------------------------------
    # 数据获取（AKShare）
    # ------------------------------------------------------------------

    def _fetch(self) -> Optional[pd.DataFrame]:
        """
        通过 AKShare 获取历史新股数据。
        失败时返回 None。
        """
        try:
            import akshare as ak

            try:
                # stock_ipo_phd: 历史新股数据
                df = ak.stock_ipo_phd()
            except Exception as e:
                logger.debug('stock_ipo_phd failed: %s', e)
                df = None

            if df is None or df.empty:
                # 降级方案：尝试 stock_ipo_summary_cninfo
                try:
                    df = ak.stock_ipo_summary_cninfo()
                except Exception as e2:
                    logger.debug('stock_ipo_summary_cninfo failed: %s', e2)
                    return None

            return self._normalize(df)

        except ImportError:
            logger.warning('akshare not installed, IPO history unavailable')
            return None
        except Exception as e:
            logger.warning('IPO history fetch failed: %s', e)
            return None

    def _normalize(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        标准化 AKShare 返回的 DataFrame。
        列名映射：不同接口返回列名不一致，统一映射到标准列名。
        """
        if df is None or df.empty:
            return pd.DataFrame()

        # 统一列名（大小写不敏感匹配）
        df.columns = [c.strip().lower() for c in df.columns]

        # 标准列映射表（AKShare 新股接口常见列名）
        col_map = {}
        for old in df.columns:
            if 'code' in old or 'symbol' in old:
                col_map[old] = 'symbol'
            elif 'name' in old and 'stock' not in old:
                col_map[old] = 'name'
            elif 'date' in old or 'listing' in old or 'ipo_date' in old:
                col_map[old] = 'ipo_date'
            elif 'price' in old or 'issue' in old:
                col_map[old] = 'issue_price'
            elif 'shares' in old or 'quantity' in old or 'vol' in old:
                col_map[old] = 'shares'
            elif 'pe' in old or 'ratio' in old:
                col_map[old] = 'pe_ratio'
            elif 'industry' in old:
                col_map[old] = 'industry'
            elif 'market' in old or 'board' in old or 'exchange' in old:
                col_map[old] = 'market_type'

        df = df.rename(columns=col_map)

        # 确保必需列存在
        required = ['symbol', 'name', 'ipo_date']
        for col in required:
            if col not in df.columns:
                logger.warning('IPO data missing required column: %s', col)
                return pd.DataFrame()

        # 数据类型转换
        if 'ipo_date' in df.columns:
            df['ipo_date'] = pd.to_datetime(df['ipo_date'], errors='coerce')
            df = df.dropna(subset=['ipo_date'])

        if 'issue_price' in df.columns:
            df['issue_price'] = pd.to_numeric(df['issue_price'], errors='coerce')

        if 'shares' in df.columns:
            df['shares'] = pd.to_numeric(df['shares'], errors='coerce')

        if 'pe_ratio' in df.columns:
            df['pe_ratio'] = pd.to_numeric(df['pe_ratio'], errors='coerce')

        # 市场类型标准化
        if 'market_type' in df.columns:
            df['market_type'] = df['market_type'].apply(self._normalize_market)

        # 设置索引
        df = df.set_index('ipo_date')
        df = df.sort_index()

        logger.info('IPO history normalized: %d rows', len(df))
        return df

    @staticmethod
    def _normalize_market(val) -> str:
        """标准化市场类型标识"""
        if pd.isna(val):
            return 'UNKNOWN'
        s = str(val).upper()
        if 'SH' in s or '上海' in s or '沪' in s:
            return 'SH'
        elif 'SZ' in s or '深圳' in s or '深' in s:
            return 'SZ'
        elif 'BJ' in s or '北京' in s:
            return 'BJ'
        return 'UNKNOWN'
