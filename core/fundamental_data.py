"""
core/fundamental_data.py — 基本面数据管道

功能：
  - 通过 AKShare 获取 A 股财务报表数据（季报/年报）
  - 本地 Parquet 缓存（data/fundamental/），TTL=24h
  - 将季频数据对齐至日频（前向填充，防止前视偏差）
  - 提供标准化接口供基本面因子调用

数据列说明：
  pe_ttm      : 市盈率（TTM）
  pb          : 市净率
  roe_ttm     : ROE（TTM）
  eps_ttm     : EPS（TTM，元/股）
  revenue_yoy : 营收同比增速（%）
  profit_yoy  : 净利润同比增速（%）
  ocf_to_profit: 经营现金流/净利润（现金流质量）

使用方式：
    manager = FundamentalDataManager()
    df = manager.get_fundamentals('000001.SZ', start='2022-01-01')
    # df 索引为交易日日期，列为上述财务指标
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger('core.fundamental_data')

_FUNDAMENTAL_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'data', 'fundamental'
)
os.makedirs(_FUNDAMENTAL_DIR, exist_ok=True)

# 缓存有效期：基本面数据变化慢，24h 足够
_TTL_SECONDS = 86400


class FundamentalDataManager:
    """
    基本面数据管理器。

    缓存策略：
      1. 内存缓存（进程生命周期内）
      2. 本地 Parquet（跨进程持久化，TTL=24h）
      3. AKShare 网络获取（Parquet 过期或不存在时）
    """

    def __init__(self):
        self._memory_cache: dict = {}   # symbol → (timestamp, DataFrame)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def get_fundamentals(
        self,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        获取标的的基本面数据（日频，前向填充季报）。

        Parameters
        ----------
        symbol : 标的代码，如 '000001.SZ'，'600519.SH'
        start  : 开始日期（'YYYY-MM-DD'），默认 3 年前
        end    : 结束日期，默认今日

        Returns
        -------
        pd.DataFrame
            DatetimeIndex（交易日），列：pe_ttm / pb / roe_ttm / eps_ttm /
            revenue_yoy / profit_yoy / ocf_to_profit
            若获取失败返回空 DataFrame。
        """
        # 内存缓存检查
        if symbol in self._memory_cache:
            ts, df = self._memory_cache[symbol]
            if (datetime.now() - ts).total_seconds() < _TTL_SECONDS:
                return self._slice(df, start, end)

        # Parquet 缓存检查
        df = self._load_parquet(symbol)
        if df is not None:
            self._memory_cache[symbol] = (datetime.now(), df)
            return self._slice(df, start, end)

        # 网络获取
        df = self._fetch(symbol)
        if df is not None and not df.empty:
            self._save_parquet(symbol, df)
            self._memory_cache[symbol] = (datetime.now(), df)
            return self._slice(df, start, end)

        return pd.DataFrame()

    def invalidate(self, symbol: str) -> None:
        """清除指定标的的内存缓存（强制下次重新获取）"""
        self._memory_cache.pop(symbol, None)

    # ------------------------------------------------------------------
    # 数据获取（AKShare）
    # ------------------------------------------------------------------

    def _fetch(self, symbol: str) -> Optional[pd.DataFrame]:
        """
        通过 AKShare 获取财务摘要数据，转换为日频 DataFrame。
        失败时返回 None。
        """
        try:
            import akshare as ak

            # 处理标的代码格式（akshare 需要 6 位纯数字）
            code = symbol.replace('.SH', '').replace('.SZ', '')

            frames = []

            # 方案 1：stock_financial_abstract — 关键财务指标（pe/pb/roe/eps）
            try:
                df_abs = ak.stock_financial_abstract(symbol=code)
                if df_abs is not None and not df_abs.empty:
                    frames.append(('abstract', df_abs))
            except Exception as e:
                logger.debug('stock_financial_abstract failed for %s: %s', symbol, e)

            # 方案 2：stock_a_indicator_lg — 估值指标（pe_ttm/pb）
            try:
                df_ind = ak.stock_a_indicator_lg(symbol=code)
                if df_ind is not None and not df_ind.empty:
                    frames.append(('indicator', df_ind))
            except Exception as e:
                logger.debug('stock_a_indicator_lg failed for %s: %s', symbol, e)

            if not frames:
                return None

            return self._normalize_frames(frames, symbol)

        except ImportError:
            logger.warning('akshare not installed, fundamental data unavailable')
            return None
        except Exception as e:
            logger.warning('Fundamental fetch failed for %s: %s', symbol, e)
            return None

    def _normalize_frames(self, frames: list, symbol: str) -> Optional[pd.DataFrame]:
        """
        将不同来源的 AKShare DataFrame 合并为标准日频格式。
        列名映射：不同接口返回列名不一致，统一映射到标准列名。
        """
        result = {}

        for source, df in frames:
            df = df.copy()

            # 统一索引为日期
            date_col = next(
                (c for c in df.columns
                 if any(k in c.lower() for k in ['date', '日期', '报告期', 'period'])),
                None
            )
            if date_col is None and pd.api.types.is_datetime64_any_dtype(df.index):
                df.index = pd.to_datetime(df.index)
            elif date_col:
                df.index = pd.to_datetime(df[date_col], errors='coerce')
                df = df.drop(columns=[date_col])
            else:
                continue

            df = df[~df.index.isna()].sort_index()

            # 列名映射
            col_map = {
                # pe
                'pe_ttm': 'pe_ttm', 'pettm': 'pe_ttm', '市盈率ttm': 'pe_ttm',
                '市盈率-动态': 'pe_ttm', 'pe': 'pe_ttm',
                # pb
                'pb': 'pb', '市净率': 'pb',
                # roe
                'roe_ttm': 'roe_ttm', 'roettm': 'roe_ttm', 'roe': 'roe_ttm',
                '净资产收益率': 'roe_ttm',
                # eps
                'eps_ttm': 'eps_ttm', 'epsttm': 'eps_ttm', '每股收益ttm': 'eps_ttm',
                '基本每股收益': 'eps_ttm', 'eps': 'eps_ttm',
                # revenue growth
                'revenue_yoy': 'revenue_yoy', '营收同比增长率': 'revenue_yoy',
                '营业总收入_同比增长率': 'revenue_yoy',
                # profit growth
                'profit_yoy': 'profit_yoy', '净利润同比增长率': 'profit_yoy',
                '归母净利润_同比增长率': 'profit_yoy',
                # cash flow quality
                'ocf_to_profit': 'ocf_to_profit',
            }

            for orig, std in col_map.items():
                # 大小写不敏感匹配
                matched = [c for c in df.columns if c.lower().replace(' ', '') == orig]
                if matched and std not in result:
                    series = pd.to_numeric(df[matched[0]], errors='coerce')
                    result[std] = series

        if not result:
            return None

        combined = pd.DataFrame(result).sort_index()
        combined = combined[~combined.index.duplicated(keep='last')]

        # 转换为日频（前向填充，模拟季报公布后数据延续）
        return self._to_daily(combined)

    def _to_daily(self, quarterly: pd.DataFrame) -> pd.DataFrame:
        """
        将季频数据前向填充至日频，防止前视偏差：
        季报公布日后才能使用该数据（假设公布日 = 数据行日期）。
        """
        if quarterly.empty:
            return quarterly

        start = quarterly.index.min()
        end = pd.Timestamp.now()
        daily_idx = pd.date_range(start=start, end=end, freq='B')  # 工作日

        daily = quarterly.reindex(daily_idx, method=None)
        daily = daily.fillna(method='ffill')  # 前向填充（季报日期之后使用）

        return daily

    # ------------------------------------------------------------------
    # Parquet 缓存
    # ------------------------------------------------------------------

    def _parquet_path(self, symbol: str) -> str:
        safe = symbol.replace('.', '_').replace('/', '_')
        return os.path.join(_FUNDAMENTAL_DIR, f'{safe}.parquet')

    def _load_parquet(self, symbol: str) -> Optional[pd.DataFrame]:
        path = self._parquet_path(symbol)
        if not os.path.isfile(path):
            return None

        # 检查文件修改时间（TTL = 24h）
        mtime = os.path.getmtime(path)
        if (datetime.now().timestamp() - mtime) > _TTL_SECONDS:
            return None

        try:
            df = pd.read_parquet(path)
            if not pd.api.types.is_datetime64_any_dtype(df.index):
                df.index = pd.to_datetime(df.index)
            return df.sort_index()
        except Exception as e:
            logger.warning('Fundamental Parquet load failed for %s: %s', symbol, e)
            return None

    def _save_parquet(self, symbol: str, df: pd.DataFrame) -> None:
        path = self._parquet_path(symbol)
        try:
            df.to_parquet(path, engine='pyarrow', compression='snappy')
        except Exception as e:
            logger.warning('Fundamental Parquet save failed for %s: %s', symbol, e)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def _slice(
        self,
        df: pd.DataFrame,
        start: Optional[str],
        end: Optional[str],
    ) -> pd.DataFrame:
        if df.empty:
            return df
        if start:
            df = df[df.index >= pd.Timestamp(start)]
        if end:
            df = df[df.index <= pd.Timestamp(end)]
        return df
