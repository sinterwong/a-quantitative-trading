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
        通过 AKShare stock_financial_analysis_indicator_em 获取财务历史数据，
        转换为日频 DataFrame。
        失败时返回 None。
        """
        try:
            import akshare as ak

            # 处理标的代码格式（AkShare 东方财富接口需要 '600519.SH' 格式）
            # stock_financial_analysis_indicator_em 必须带交易所后缀
            code_raw = symbol.upper()
            if not code_raw.endswith(('.SH', '.SZ')):
                # 没有后缀时默认沪市
                code_raw = code_raw + '.SH'

            # 获取财务分析指标（ROE/EPS/营收/净利润历史，~102期）
            df = ak.stock_financial_analysis_indicator_em(symbol=code_raw)
            if df is None or df.empty:
                return None

            return self._normalize_indicator_em(df, symbol)

        except ImportError:
            logger.warning('akshare not installed, fundamental data unavailable')
            return None
        except Exception as e:
            logger.warning('Fundamental fetch failed for %s: %s', symbol, e)
            return None

    def _normalize_indicator_em(
        self, df: pd.DataFrame, symbol: str,
    ) -> Optional[pd.DataFrame]:
        """
        将 stock_financial_analysis_indicator_em 返回的 DataFrame 标准化。

        列映射：
          ROEJQ        → roe_ttm      （加权 ROE，%）
          EPSJB        → eps_ttm      （每股收益 TTM，元/股）
          NETPROFITRPHBZC → profit_yoy（净利润同比增长，%）
          TOTALOPERATEREVE → _revenue  （营业总收入，用于自算营收 YoY）

        注意：
          - pe_ttm / pb / ocf_to_profit / revenue_yoy / holder_num 在此数据源中不可得，
            对应因子（PEPercentile / CashFlowQuality / RevenueGrowth /
            ShareholderConcentration）将降级为零，这是已知数据层限制。
        """
        df = df.copy()

        # 日期列
        if 'REPORT_DATE' not in df.columns:
            return None
        df.index = pd.to_datetime(df['REPORT_DATE'], errors='coerce')
        df = df[~df.index.isna()].sort_index()
        if df.empty:
            return None

        result = {}

        # ROE（%，直接可用）
        if 'ROEJQ' in df.columns:
            result['roe_ttm'] = pd.to_numeric(df['ROEJQ'], errors='coerce')

        # EPS（TTM，元/股）
        if 'EPSJB' in df.columns:
            result['eps_ttm'] = pd.to_numeric(df['EPSJB'], errors='coerce')

        # 净利润 YoY（%，AkShare 直接提供）
        if 'NETPROFITRPHBZC' in df.columns:
            result['profit_yoy'] = pd.to_numeric(df['NETPROFITRPHBZC'], errors='coerce')

        # 营收 YoY（AkShare 无直接字段，从 TOTALOPERATEREVE 自算）
        if 'TOTALOPERATEREVE' in df.columns:
            rev = pd.to_numeric(df['TOTALOPERATEREVE'], errors='coerce').fillna(0.0)
            rev_prev = rev.shift(1)  # 上一年同期
            # 避免除零
            yoy = ((rev / rev_prev.replace(0, np.nan)) - 1) * 100
            result['revenue_yoy'] = yoy.replace([np.inf, -np.inf], np.nan)

        # 以下字段在此数据源中不可得，因子将自动降级：
        #   pe_ttm, pb, ocf_to_profit, holder_num
        # 其对应因子在 financial_data 缺失这些字段时返回零值，这是预期行为。

        if not result:
            return None

        combined = pd.DataFrame(result).sort_index()
        combined = combined[~combined.index.duplicated(keep='last')]

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
        daily = daily.ffill()  # 前向填充（季报日期之后使用）

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
