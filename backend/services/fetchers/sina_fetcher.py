# -*- coding: utf-8 -*-
"""
sina_fetcher.py — 新浪财经数据源
================================

优先级: 1
数据源: 新浪财经 K线接口

接口: https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData
参数: symbol=<code>&scale=240&ma=no&datalen=6000

特点:
  - 免费，无需 Token
  - 单次请求返回全量历史（最多6000条）
  - scale=240 表示日K（240分钟=1天）
  - 有防封禁随机休眠（2~5秒）
"""

import json
import logging
import random
import ssl
import time
import urllib.request
from typing import Optional

import pandas as pd

from ..base_fetcher import BaseFetcher, normalize_stock_code
from ..data_fetch_exceptions import DataSourceUnavailableError, RateLimitError, DataFetchError

logger = logging.getLogger('sina_fetcher')


class SinaFetcher(BaseFetcher):
    """新浪财经日线数据 fetcher（优先级 1）"""

    name = "SinaFetcher"
    priority = 1

    _SSL_CTX = ssl.create_default_context()
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode = ssl.CERT_NONE

    _USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    ]

    _last_call_time: float = 0.0

    def _to_sina_code(self, stock_code: str) -> str:
        """将标准化代码转换为新浪格式（sh600519 / sz000001）"""
        code = normalize_stock_code(stock_code)
        if code.startswith(('60', '68', '5')):
            return f"sh{code}"
        return f"sz{code}"

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        通过新浪财经接口获取日线历史数据。

        注意：新浪接口不支持服务器端日期过滤，
        返回全量历史（约6000条），日期过滤在客户端完成。
        """
        self._rate_limit_sleep()

        sina_code = self._to_sina_code(stock_code)
        url = (
            f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php"
            f"/CN_MarketData.getKLineData"
            f"?symbol={sina_code}&scale=240&ma=no&datalen=6000"
        )

        headers = {
            'User-Agent': random.choice(self._USER_AGENTS),
            'Referer': 'https://finance.sina.com.cn/',
        }

        raw_text: Optional[str] = None
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, context=self._SSL_CTX, timeout=30) as resp:
                raw_text = resp.read().decode('utf-8', errors='replace')

        except Exception as e:
            self._classify_and_raise(e, stock_code)

        try:
            data_list = json.loads(raw_text)
        except json.JSONDecodeError:
            raise DataSourceUnavailableError(
                f"[SinaFetcher] JSON解析失败: {stock_code}",
                source=self.name, stock_code=stock_code
            )

        if not data_list:
            raise DataFetchError(
                f"[SinaFetcher] 空数据: {stock_code}",
                source=self.name, stock_code=stock_code
            )

        # 转换为 DataFrame
        df = pd.DataFrame(data_list)

        # 客户端日期过滤（新浪不支持服务器端日期范围）
        df = df[df['day'] >= start_date]
        df = df[df['day'] <= end_date]

        if df.empty:
            raise DataFetchError(
                f"[SinaFetcher] 日期范围内无数据: {stock_code} ({start_date}~{end_date})",
                source=self.name, stock_code=stock_code
            )

        return df

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        新浪原始列名: day, open, close, high, low, volume
        →
        标准列名: date, open, high, low, close, volume, amount, pct_chg
        """
        df = df.copy()

        # 重命名
        df = df.rename(columns={
            'day': 'date',
        })

        # 数值列转换
        for col in ['open', 'close', 'high', 'low', 'volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 成交额（新浪日K无此字段，用量*价估算）
        if 'amount' not in df.columns:
            df['amount'] = (df['close'] * df['volume']).round(2)

        # 涨跌幅
        df = df.sort_values('date', ascending=True).reset_index(drop=True)
        df['pct_chg'] = df['close'].pct_change().fillna(0).round(4) * 100

        cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']
        return df[[c for c in cols if c in df.columns]]

    def _rate_limit_sleep(self) -> None:
        """同域名 200ms 最低间隔 + 随机休眠"""
        elapsed = time.time() - self._last_call_time
        if elapsed < 0.2:
            time.sleep(0.2 - elapsed)
        self.random_sleep(2.0, 5.0)
        self._last_call_time = time.time()

    @staticmethod
    def _classify_and_raise(e: Exception, stock_code: str) -> None:
        msg = str(e).lower()
        code = getattr(e, 'code', 0)

        if code == 429 or '429' in msg or 'too many' in msg:
            raise RateLimitError(
                f"[SinaFetcher] 频率限制: {stock_code}",
                source="SinaFetcher", stock_code=stock_code
            )
        if code == 403 or '403' in msg or 'forbidden' in msg or 'ban' in msg:
            raise DataSourceUnavailableError(
                f"[SinaFetcher] 被封禁: {stock_code}",
                source="SinaFetcher", stock_code=stock_code
            )
        if 'timeout' in msg or 'timed out' in msg:
            raise DataSourceUnavailableError(
                f"[SinaFetcher] 超时: {stock_code}",
                source="SinaFetcher", stock_code=stock_code
            )
        raise DataSourceUnavailableError(
            f"[SinaFetcher] 连接失败: {stock_code}: {e}",
            source="SinaFetcher", stock_code=stock_code
        )
