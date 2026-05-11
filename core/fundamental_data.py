"""
core/fundamental_data.py — 基本面历史时序数据管理器

功能：
  - 通过 DataGateway.fundamentals_history() 获取 A 股财务历史数据（季报/年报）
  - DataGateway 层统一管理缓存（24h TTL）、熔断和健康度路由
  - 将季频数据对齐至日频（前向填充，防止前视偏差）
  - 提供标准化接口供基本面因子调用

数据列说明（由 DataGateway 标准化）：
  roe_ttm     : ROE（TTM，加权，%）
  eps_ttm     : EPS（TTM，元/股）
  revenue_yoy : 营收同比增速（%，自算）
  profit_yoy  : 净利润同比增速（%，AkShare 直接提供）
  ocf_to_profit: 经营现金流/净利润（此数据源不可得，因子将降级为零）

注意：
  pe_ttm / pb / ocf_to_profit / holder_num 在 stock_financial_analysis_indicator_em
  中不可得，对应因子（PEPercentile / CashFlowQuality / ShareholderConcentration）
  将降级为零，这是已知数据层限制。

使用方式：
    from core.fundamental_data import FundamentalDataManager

    mgr = FundamentalDataManager()
    df = mgr.get_fundamentals('000001.SZ', start='2022-01-01')
    # df 索引为交易日日期（DatetimeIndex），列为上述财务指标
"""

from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

from core.data_gateway import get_gateway

logger = logging.getLogger('core.fundamental_data')


class FundamentalDataManager:
    """
    基本面历史数据管理器。

    所有网络请求委托给 DataGateway，享受统一保护：
      - 熔断（ProviderError 触发 HealthTracker 降权）
      - 健康度路由（优先用稳定的 provider）
      - 缓存（TTL=24h，减少重复请求）

    内部不再持有任何 HTTP/AkShare 代码。
    """

    def get_fundamentals(
        self,
        symbol: str,
        start: Optional[str] = None,
        end: Optional[str] = None,
    ) -> pd.DataFrame:
        """
        获取标的的基本面历史数据（日频，前向填充季报）。

        Parameters
        ----------
        symbol : str
            标的代码，如 '000001.SZ'，'600519.SH'
        start : str, optional
            开始日期（'YYYY-MM-DD'），默认 3 年前
        end : str, optional
            结束日期，默认今日

        Returns
        -------
        pd.DataFrame
            DatetimeIndex（交易日），列：roe_ttm / eps_ttm /
            revenue_yoy / profit_yoy / ocf_to_profit
            若获取失败返回空 DataFrame。
        """
        try:
            gw = get_gateway()
            df = gw.fundamentals_history(symbol, start=start, end=end)
            if df is None or df.empty:
                return pd.DataFrame()
            # 确保索引类型一致
            if not pd.api.types.is_datetime64_any_dtype(df.index):
                df.index = pd.to_datetime(df.index, errors='coerce')
                df = df[~df.index.isna()]
            return df.sort_index()
        except Exception as exc:
            logger.warning(
                'FundamentalDataManager.get_fundamentals(%s) 失败: %s',
                symbol, exc,
            )
            return pd.DataFrame()

    def invalidate(self, symbol: str) -> None:
        """清除指定标的的缓存（委托 DataGateway 清除）"""
        try:
            get_gateway().invalidate_cache()
        except Exception:
            pass
