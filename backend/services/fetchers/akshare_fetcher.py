# -*- coding: utf-8 -*-
"""
akshare_fetcher.py — AkShare 数据源
====================================

优先级: 2
数据源: AkShare 封装接口

ETF日线: ak.fund_etf_hist_em(symbol, period='daily', start_date, end_date)
A股日线: ak.stock_zh_a_daily(symbol, start_date, end_date, adjust='qfq')

特点:
  - AkShare 封装了多个来源（东方财富/新浪/腾讯/同花顺）
  - 自动处理 ETF vs 股票类型判断
  - 自动处理上交所 vs 深交所代码
  - 有防封禁随机休眠（2~5秒）
"""

import logging
import os
import time
from typing import Optional

import pandas as pd

# 禁用代理（AkShare 某些接口对代理敏感）
for _key in list(os.environ.keys()):
    if 'proxy' in _key.lower():
        del os.environ[_key]

try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except ImportError:
    AKSHARE_AVAILABLE = False

from ..base_fetcher import BaseFetcher, normalize_stock_code
from ..data_fetch_exceptions import DataSourceUnavailableError, RateLimitError, DataFetchError

logger = logging.getLogger('akshare_fetcher')


# ─── ETF 代码判断 ────────────────────────────────────────────────────────

ETF_PREFIXES = ('510', '512', '513', '515', '516', '518', '560', '561', '563', '564', '588')


def _is_etf(stock_code: str) -> bool:
    code = normalize_stock_code(stock_code)
    return code.isdigit() and len(code) == 6 and code.startswith(ETF_PREFIXES)


class AkshareFetcher(BaseFetcher):
    """AkShare 日线数据 fetcher（优先级 2）"""

    name = "AkshareFetcher"
    priority = 2

    _last_call_time: float = 0.0

    def __init__(self):
        super().__init__()
        if not AKSHARE_AVAILABLE:
            raise ImportError(
                "[AkshareFetcher] AkShare 未安装，请运行: pip install akshare"
            )

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        通过 AkShare 获取日线历史数据。

        自动判断 ETF 还是 A股，自动选择接口：
          - ETF → fund_etf_hist_em
          - A股 → stock_zh_a_daily
        """
        self._rate_limit_sleep()

        pure = normalize_stock_code(stock_code)
        # AkShare 日期格式: YYYYMMDD
        sd = start_date.replace('-', '')
        ed = end_date.replace('-', '')

        df: Optional[pd.DataFrame] = None

        if _is_etf(pure):
            df = self._fetch_etf(pure, sd, ed)
        else:
            # 优先用新浪历史（最稳定），失败后用 AkShare 直连
            symbol_with_exchange = self._to_akshare_symbol(pure)
            if symbol_with_exchange:
                df = self._fetch_stock(symbol_with_exchange, sd, ed)

        if df is None or df.empty:
            raise DataFetchError(
                f"[AkshareFetcher] 未获取到数据: {stock_code}",
                source=self.name, stock_code=stock_code
            )

        return df

    def _fetch_etf(self, pure: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """使用 fund_etf_hist_em 获取 ETF 日线"""
        try:
            df = ak.fund_etf_hist_em(
                symbol=pure,
                period='daily',
                start_date=start_date,
                end_date=end_date,
            )
            if df is not None and not df.empty:
                logger.debug("[AkshareFetcher] ETF %s: %d 行 via fund_etf_hist_em", pure, len(df))
            return df
        except Exception as e:
            logger.warning("[AkshareFetcher] fund_etf_hist_em 失败 %s: %s", pure, e)
            return None

    def _fetch_stock(self, symbol: str, start_date: str, end_date: str) -> Optional[pd.DataFrame]:
        """使用 stock_zh_a_daily 获取 A股日线"""
        try:
            df = ak.stock_zh_a_daily(
                symbol=symbol,
                start_date=start_date,
                end_date=end_date,
                adjust='qfq',
            )
            if df is not None and not df.empty:
                logger.debug("[AkshareFetcher] 股票 %s: %d 行 via stock_zh_a_daily", symbol, len(df))
            return df
        except Exception as e:
            logger.warning("[AkshareFetcher] stock_zh_a_daily 失败 %s: %s", symbol, e)
            return None

    def _to_akshare_symbol(self, pure: str) -> Optional[str]:
        """
        将纯代码转换为 AkShare 格式。

        Returns:
            'sh600519' / 'sz000001' 等，或 None（不支持的板块）
        """
        if not pure.isdigit() or len(pure) != 6:
            return None
        if pure.startswith(('60', '68', '5')):
            return f"sh{pure}"
        if pure.startswith(('00', '30', '08')):
            return f"sz{pure}"
        return None

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        AkShare 列名映射 → 标准列名。

        可能的列名组合:
          ETF:  日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 涨跌幅, 涨跌额, 换手率
          A股:  date, open, close, high, low, volume, amount, pct_change, change, turnover
        """
        df = df.copy()

        # 列名大小写兼容映射
        rename_map = {}
        for col in df.columns:
            c = col.lower()
            if c == '日期' or c == 'date':
                rename_map[col] = 'date'
            elif c in ('开盘', 'open'):
                rename_map[col] = 'open'
            elif c in ('收盘', 'close'):
                rename_map[col] = 'close'
            elif c in ('最高', 'high'):
                rename_map[col] = 'high'
            elif c in ('最低', 'low'):
                rename_map[col] = 'low'
            elif c in ('成交量', 'volume'):
                rename_map[col] = 'volume'
            elif c in ('成交额', 'amount'):
                rename_map[col] = 'amount'
            elif c in ('涨跌幅', 'pct_change', 'pct_chg'):
                rename_map[col] = 'pct_chg'
            elif c in ('涨跌额', 'change'):
                rename_map[col] = 'pct_chg'  # 复用 pct_chg 槽位，原始数据已%

        df = df.rename(columns=rename_map)

        # 数值转换
        for col in ['open', 'close', 'high', 'low', 'volume', 'amount', 'pct_chg']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # pct_chg 若原始数据是小数形式（如 0.05），转换为百分数（5.0）
        if 'pct_chg' in df.columns:
            vals = df['pct_chg'].dropna()
            if len(vals) > 0 and vals.abs().max() < 1:  # 判断是小数形式
                df['pct_chg'] = df['pct_chg'] * 100

        # amount 可能缺失（ETF 接口不含此字段）
        if 'amount' not in df.columns or df['amount'].isna().all():
            df['amount'] = (df['close'] * df['volume']).round(2)

        # 排序
        df = df.sort_values('date', ascending=True).reset_index(drop=True)

        cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']
        return df[[c for c in cols if c in df.columns]]

    def _rate_limit_sleep(self) -> None:
        """同域名 300ms 最低间隔 + 随机休眠（AkShare 东方财富源较敏感）"""
        elapsed = time.time() - self._last_call_time
        if elapsed < 0.3:
            time.sleep(0.3 - elapsed)
        self.random_sleep(2.0, 5.0)
        self._last_call_time = time.time()
