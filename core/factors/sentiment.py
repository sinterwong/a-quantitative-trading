"""
core/factors/sentiment.py — 情绪因子库

3 个市场情绪因子，基于 A 股特色数据源：

  1. MarginTradingFactor  : 融资余额变化率（融资盘增量 = 看多情绪升温）
  2. NorthboundFlowFactor : 北向资金净流入强度（外资持续买入 = 看多信号）
  3. ShortInterestFactor  : 融券余额变化率（融券增加 = 看空压力上升）

数据来源：
  - 融资融券：MarginDataStore（自动拉取 + Parquet 日更新时序）
  - 北向资金：复用 core/external_signal.py 的 NorthboundStatsAnalyzer 数据接口，
    或直接接受注入的 DataFrame

设计原则：
  - 接受 sentiment_data: pd.DataFrame（外部注入，优先于自动拉取）
  - sentiment_data=None 时，MarginDataStore 自动从 AKShare 拉取并缓存 Parquet
  - 无数据时返回全零（降级不崩溃）
  - 数据频率为日频（每日收盘后更新）

用法：
    # 方式一：自动拉取（需 AKShare 可用）
    f = MarginTradingFactor(symbol='000001.SZ')
    z = f.evaluate(price_df)

    # 方式二：注入已获取的数据（测试 / 离线场景）
    margin_df = pd.DataFrame({'margin_balance': [...]}, index=dates)
    f = MarginTradingFactor(sentiment_data=margin_df)
    z = f.evaluate(price_df)
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import List, Optional

import numpy as np
import pandas as pd

from core.factors.base import Factor, FactorCategory, Signal

logger = logging.getLogger('core.factors.sentiment')

# ---------------------------------------------------------------------------
# MarginDataStore — 融资融券数据持久化层
# ---------------------------------------------------------------------------

_SENTIMENT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    'data', 'sentiment',
)
os.makedirs(_SENTIMENT_DIR, exist_ok=True)

_MARGIN_TTL_HOURS = 24   # Parquet 缓存有效期


class MarginDataStore:
    """
    融资融券数据管理器。

    功能：
      - 从 AKShare stock_margin_detail() 拉取个股融资融券日序列
      - 本地 Parquet 持久化（data/sentiment/margin_{symbol}.parquet）
      - TTL=24h：交易日收盘后触发一次更新，日内重复调用走缓存
      - 返回包含 margin_balance（融资余额）和 short_balance（融券余额）的日频 DataFrame

    使用：
        store = MarginDataStore()
        df = store.get('000001.SZ')
        # df.columns: ['margin_balance', 'short_balance']
        # df.index  : DatetimeIndex（交易日）
    """

    def __init__(self):
        self._memory: dict = {}   # symbol → (fetched_at: datetime, df: pd.DataFrame)

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def get(self, symbol: str, start: Optional[str] = None) -> pd.DataFrame:
        """
        获取标的融资融券日频序列。

        Parameters
        ----------
        symbol : 标的代码，如 '000001.SZ'
        start  : 截取起始日期（'YYYY-MM-DD'），默认返回全部历史

        Returns
        -------
        pd.DataFrame
            DatetimeIndex，列：margin_balance（元）, short_balance（元）
            失败时返回空 DataFrame。
        """
        # 内存缓存
        if symbol in self._memory:
            fetched_at, df = self._memory[symbol]
            age_h = (datetime.now() - fetched_at).total_seconds() / 3600
            if age_h < _MARGIN_TTL_HOURS:
                return self._slice(df, start)

        # Parquet 缓存
        df = self._load_parquet(symbol)
        if df is not None:
            self._memory[symbol] = (datetime.now(), df)
            return self._slice(df, start)

        # 网络拉取
        df = self._fetch(symbol)
        if df is not None and not df.empty:
            self._save_parquet(symbol, df)
            self._memory[symbol] = (datetime.now(), df)
            return self._slice(df, start)

        return pd.DataFrame()

    def invalidate(self, symbol: str) -> None:
        """清除指定标的的内存缓存（下次调用强制重新拉取）。"""
        self._memory.pop(symbol, None)

    # ------------------------------------------------------------------
    # 数据获取
    # ------------------------------------------------------------------

    def _fetch(self, symbol: str) -> Optional[pd.DataFrame]:
        """调用 AKShare stock_margin_detail() 构建日频时序。"""
        try:
            import akshare as ak
            code = symbol.replace('.SH', '').replace('.SZ', '')
            raw = ak.stock_margin_detail(symbol=code)
            if raw is None or raw.empty:
                return None
            return self._normalize(raw)
        except Exception as e:
            logger.warning('MarginDataStore fetch failed for %s: %s', symbol, e)
            return None

    @staticmethod
    def _normalize(raw: pd.DataFrame) -> pd.DataFrame:
        """
        将 AKShare stock_margin_detail 原始列名归一化为标准列：
          margin_balance（融资余额）、short_balance（融券余额）
        AKShare 返回列名可能为：信用交易日期/rz_ye/rz_mre/rq_ye/rq_sl 等。
        """
        col_map_margin = {
            'rz_ye': 'margin_balance',
            'rzye': 'margin_balance',
            '融资余额': 'margin_balance',
            'margin_balance': 'margin_balance',
        }
        col_map_short = {
            'rq_ye': 'short_balance',
            'rqye': 'short_balance',
            '融券余额': 'short_balance',
            'short_balance': 'short_balance',
        }
        col_map_date = {
            '信用交易日期': 'date',
            'trade_date': 'date',
            'date': 'date',
        }

        df = raw.copy()
        # 统一列名（小写）
        df.columns = [c.strip().lower() for c in df.columns]

        rename = {}
        for src, dst in {**col_map_margin, **col_map_short, **col_map_date}.items():
            if src.lower() in df.columns:
                rename[src.lower()] = dst
        df = df.rename(columns=rename)

        # 确保日期列存在
        date_candidates = ['date', '信用交易日期', 'trade_date']
        date_col = next((c for c in date_candidates if c in df.columns), None)
        if date_col is None:
            # 尝试找第一列作为日期
            date_col = df.columns[0]

        df['date'] = pd.to_datetime(df[date_col], errors='coerce')
        df = df.dropna(subset=['date']).set_index('date').sort_index()

        result = pd.DataFrame(index=df.index)
        if 'margin_balance' in df.columns:
            result['margin_balance'] = pd.to_numeric(df['margin_balance'], errors='coerce')
        if 'short_balance' in df.columns:
            result['short_balance'] = pd.to_numeric(df['short_balance'], errors='coerce')

        return result.dropna(how='all')

    # ------------------------------------------------------------------
    # Parquet 持久化
    # ------------------------------------------------------------------

    def _parquet_path(self, symbol: str) -> str:
        safe = symbol.replace('.', '_')
        return os.path.join(_SENTIMENT_DIR, f'margin_{safe}.parquet')

    def _load_parquet(self, symbol: str) -> Optional[pd.DataFrame]:
        path = self._parquet_path(symbol)
        if not os.path.exists(path):
            return None
        mtime = os.path.getmtime(path)
        age_h = (datetime.now().timestamp() - mtime) / 3600
        if age_h > _MARGIN_TTL_HOURS:
            return None
        try:
            return pd.read_parquet(path)
        except Exception as e:
            logger.warning('MarginDataStore Parquet load failed %s: %s', symbol, e)
            return None

    def _save_parquet(self, symbol: str, df: pd.DataFrame) -> None:
        path = self._parquet_path(symbol)
        try:
            df.to_parquet(path, engine='pyarrow', compression='snappy')
        except Exception as e:
            logger.warning('MarginDataStore Parquet save failed %s: %s', symbol, e)

    @staticmethod
    def _slice(df: pd.DataFrame, start: Optional[str]) -> pd.DataFrame:
        if start and not df.empty:
            return df[df.index >= pd.Timestamp(start)]
        return df


def _align_sentiment(
    sentiment_data: Optional[pd.DataFrame],
    price_index: pd.Index,
    column: str,
    fill_method: str = 'ffill',
) -> pd.Series:
    """
    将情绪数据对齐到价格数据索引（前向填充）。
    缺失或不存在时返回全 NaN Series。
    """
    if sentiment_data is None or sentiment_data.empty:
        return pd.Series(np.nan, index=price_index)
    if column not in sentiment_data.columns:
        return pd.Series(np.nan, index=price_index)
    series = sentiment_data[column].reindex(price_index, method=fill_method)
    return series


# ---------------------------------------------------------------------------
# 1. 融资余额变化率因子
# ---------------------------------------------------------------------------

class MarginTradingFactor(Factor):
    """
    融资余额变化率因子（A 股融资盘动量）。

    因子值 = 融资余额短期变化率 vs 长期变化率（加速度）
    = (rolling_mean(Δmargin/margin, short)) / (rolling_mean(Δmargin/margin, long))

    解读：
      - z > 0：融资买入加速（看多情绪升温）→ 中期跟随 BUY
      - z < 0：融资买入减速或净偿还（去杠杆）→ 短期谨慎

    注意：融资过热（连续快速增长）本身也是尾部风险信号，
    因此本因子权重建议 ≤ 0.15。

    Parameters
    ----------
    sentiment_data : pd.DataFrame, optional
        需包含列 'margin_balance'（融资余额，元）。
        为 None 且 symbol 不为空时，自动通过 MarginDataStore 拉取。
    symbol       : 标的代码（auto_fetch 模式必填）
    short_window : 短期滚动窗口（默认 5 天）
    long_window  : 长期滚动窗口（默认 20 天）
    """

    name = 'MarginTrading'
    category = FactorCategory.SENTIMENT

    def __init__(
        self,
        sentiment_data: Optional[pd.DataFrame] = None,
        short_window: int = 5,
        long_window: int = 20,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.sentiment_data = sentiment_data
        self.short_window = short_window
        self.long_window = long_window
        self.threshold = threshold
        self.symbol = symbol

    def _get_sentiment(self, price_index: pd.Index) -> Optional[pd.DataFrame]:
        """sentiment_data 优先；为 None 且有 symbol 时走 MarginDataStore 自动拉取。"""
        if self.sentiment_data is not None:
            return self.sentiment_data
        if self.symbol:
            start = str(price_index.min().date()) if len(price_index) else None
            try:
                df = MarginDataStore().get(self.symbol, start=start)
                if not df.empty:
                    return df
            except Exception as e:
                logger.warning('MarginTradingFactor auto-fetch failed for %s: %s', self.symbol, e)
        return None

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        sentiment = self._get_sentiment(data.index)
        margin = _align_sentiment(sentiment, data.index, 'margin_balance')

        if margin.isna().all():
            return pd.Series(0.0, index=data.index)

        # 日变化率
        margin_chg = margin.pct_change().fillna(0.0)

        # 短期 vs 长期加速度
        short_ma = margin_chg.rolling(self.short_window, min_periods=1).mean()
        long_ma = margin_chg.rolling(self.long_window, min_periods=1).mean()
        accel = short_ma - long_ma

        return self.normalize(accel)


# ---------------------------------------------------------------------------
# 2. 北向资金净流入因子
# ---------------------------------------------------------------------------

class NorthboundFlowFactor(Factor):
    """
    北向资金净流入强度因子。

    因子值 = 滚动均值(北向净流入, window) / 历史标准差
    = 净流入的 z-score（相对历史波动性）

    解读：
      - z > threshold：北向持续大额净买入 → 外资看多 → BUY
      - z < -threshold：北向持续大额净卖出 → 外资撤离 → SELL

    Parameters
    ----------
    sentiment_data : pd.DataFrame
        需包含列 'north_flow'（北向净流入，亿元/天）。
        可来自 AKShare stock_connect_north_net_flow_in() 或
        DataLayer.get_north_flow()。
    window : 平滑窗口（默认 5 天）
    """

    name = 'NorthboundFlow'
    category = FactorCategory.SENTIMENT

    def __init__(
        self,
        sentiment_data: Optional[pd.DataFrame] = None,
        window: int = 5,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.sentiment_data = sentiment_data
        self.window = window
        self.threshold = threshold
        self.symbol = symbol

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        flow = _align_sentiment(self.sentiment_data, data.index, 'north_flow')

        if flow.isna().all():
            return pd.Series(0.0, index=data.index)

        # 滚动均值平滑（减少单日噪声）
        smoothed = flow.fillna(0.0).rolling(self.window, min_periods=1).mean()

        return self.normalize(smoothed)

    def signals(
        self,
        factor_values: pd.Series,
        price: float,
        threshold: float = 1.0,
    ) -> List[Signal]:
        """北向资金信号：持续净买入 → BUY，持续净卖出 → SELL"""
        latest = factor_values.iloc[-1]
        from datetime import datetime

        if latest > threshold:
            strength = min((latest - threshold) / threshold, 1.0)
            return [Signal(
                timestamp=datetime.now(),
                symbol=self.symbol,
                direction='BUY',
                strength=strength,
                factor_name=self.name,
                price=price,
                metadata={'north_flow_zscore': round(float(latest), 3)},
            )]
        if latest < -threshold:
            strength = min((abs(latest) - threshold) / threshold, 1.0)
            return [Signal(
                timestamp=datetime.now(),
                symbol=self.symbol,
                direction='SELL',
                strength=strength,
                factor_name=self.name,
                price=price,
                metadata={'north_flow_zscore': round(float(latest), 3)},
            )]
        return []


# ---------------------------------------------------------------------------
# 3. 融券余额变化率因子（做空压力）
# ---------------------------------------------------------------------------

class ShortInterestFactor(Factor):
    """
    融券余额变化率因子（做空压力指标）。

    因子值 = -rolling_mean(Δshort_balance/short_balance, window)
    （取负值：融券增加 → 做空压力上升 → 因子值为负 → SELL 方向）

    解读：
      - z < -threshold：融券余额快速增加（做空压力大）→ SELL
      - z > threshold：融券余额快速减少（空头回补 / 看空情绪退潮）→ BUY

    注意：A 股融券规模远小于融资，此因子信号强度通常偏弱，
    建议结合其他因子使用。

    Parameters
    ----------
    sentiment_data : pd.DataFrame, optional
        需包含列 'short_balance'（融券余额，元）。
        为 None 且 symbol 不为空时，自动通过 MarginDataStore 拉取。
    symbol : 标的代码（auto_fetch 模式必填）
    window : 变化率平滑窗口（默认 10 天）
    """

    name = 'ShortInterest'
    category = FactorCategory.SENTIMENT

    def __init__(
        self,
        sentiment_data: Optional[pd.DataFrame] = None,
        window: int = 10,
        threshold: float = 1.0,
        symbol: str = '',
    ):
        self.sentiment_data = sentiment_data
        self.window = window
        self.threshold = threshold
        self.symbol = symbol

    def _get_sentiment(self, price_index: pd.Index) -> Optional[pd.DataFrame]:
        """sentiment_data 优先；为 None 且有 symbol 时走 MarginDataStore 自动拉取。"""
        if self.sentiment_data is not None:
            return self.sentiment_data
        if self.symbol:
            start = str(price_index.min().date()) if len(price_index) else None
            try:
                df = MarginDataStore().get(self.symbol, start=start)
                if not df.empty:
                    return df
            except Exception as e:
                logger.warning('ShortInterestFactor auto-fetch failed for %s: %s', self.symbol, e)
        return None

    def evaluate(self, data: pd.DataFrame) -> pd.Series:
        sentiment = self._get_sentiment(data.index)
        short_bal = _align_sentiment(sentiment, data.index, 'short_balance')

        if short_bal.isna().all():
            return pd.Series(0.0, index=data.index)

        # 日变化率（融券增加为正值）
        short_chg = short_bal.pct_change().fillna(0.0)
        smoothed = short_chg.rolling(self.window, min_periods=1).mean()

        # 取负：融券增加 → 因子值为负（SELL 方向）
        return self.normalize(-smoothed)
