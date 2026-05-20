"""
core/fundamental_data.py — 基本面历史时序数据管理器

功能：
  - 通过 DataGateway.fundamentals_history() 获取 A 股财务历史数据（季报/年报）
  - DataGateway 层统一管理缓存（24h TTL）、熔断和健康度路由
  - 将季频数据对齐至日频（前向填充，防止前视偏差）
  - 提供标准化接口供基本面因子调用

数据列说明（由 DataGateway 多 provider 列级合并）：
  利润表(Akshare):
    roe_ttm        ROE（TTM，加权，%）
    eps_ttm        EPS（TTM，元/股）
    revenue_yoy    营收同比增速（%，自算）
    profit_yoy     净利润同比增速（%）
  成长(Akshare, W1-1):
    eps_yoy        EPS 同比增速（%）
    asset_yoy      总资产同比增速（%）
  估值(Akshare):
    dividend_yield 股息率（%，若数据源提供）
  资产负债(Baostock, W1-2):
    debt_to_equity 资产负债率（%）
    current_ratio  流动比率
    quick_ratio    速动比率

注意：
  pe_ttm / pb / ocf_to_profit / holder_num 在 stock_financial_analysis_indicator_em
  中不可得；ocf_to_profit 可由 Baostock 现金流接口在快照层补充，
  历史时序中仍可能缺失，对应因子(CashFlowQuality) 在缺失时降级为零。

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
from core.errors import DataSourceError

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
        # R0-5: 区分"该标的本就无基本面数据"（返回空 DF）与"获取过程出错"
        # （抛 DataSourceError），避免上层把网络故障当作"数据缺失"静默降级为
        # 0 分继续跑策略，市场恢复后却仍在用错值。
        try:
            gw = get_gateway()
            df = gw.fundamentals_history(symbol, start=start, end=end)
        except Exception as exc:
            logger.warning(
                'FundamentalDataManager.get_fundamentals(%s) 数据源失败: %s',
                symbol, exc,
            )
            raise DataSourceError(
                f'fetch fundamentals_history failed for {symbol}: {exc}'
            ) from exc

        if df is None or df.empty:
            # 合法的"无数据"——例如 ETF / 港股没有 A 股财务字段。
            return pd.DataFrame()
        # 确保索引类型一致
        if not pd.api.types.is_datetime64_any_dtype(df.index):
            df.index = pd.to_datetime(df.index, errors='coerce')
            df = df[~df.index.isna()]
        return df.sort_index()

    def invalidate(self, symbol: str) -> None:
        """清除指定标的的缓存（委托 DataGateway 精确清除该标的基本面历史缓存）。"""
        try:
            get_gateway().invalidate_fundamentals_history(symbol)
        except Exception:
            pass
