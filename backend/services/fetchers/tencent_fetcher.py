# -*- coding: utf-8 -*-
"""
tencent_fetcher.py — 腾讯财经数据源
===================================

优先级: 0（最高）
数据源: 腾讯财经 web.ifzq.gtimg.cn 历史K线接口

特点:
  - 免费，无需 Token
  - 返回日期/开/收/高/低/量，单次请求全量历史
  - 有防封禁随机休眠（2~5秒）

注意：
  腾讯接口用于日线历史，实时行情用 qt.gtimg.cn（不在本 fetcher 范围）
"""

import json
import logging
import random
import ssl
import time
import urllib.request
from datetime import datetime
from typing import Optional

import pandas as pd

from ..base_fetcher import BaseFetcher, safe_float, normalize_stock_code
from ..data_fetch_exceptions import DataSourceUnavailableError, RateLimitError, DataFetchError

logger = logging.getLogger('tencent_fetcher')


class TencentFetcher(BaseFetcher):
    """腾讯财经日线数据 fetcher（优先级 0）"""

    name = "TencentFetcher"
    priority = 0

    _SSL_CTX = ssl.create_default_context()
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode = ssl.CERT_NONE

    _USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0',
    ]

    # 类级别的最后请求时间（配合 rate limit）
    _last_call_time: float = 0.0

    def _to_tencent_code(self, stock_code: str) -> str:
        """将标准化代码转换为腾讯格式（sh600519 / sz000001）"""
        code = normalize_stock_code(stock_code)
        # 上交所: 60/68/5xx 开头
        if code.startswith(('60', '68', '5')):
            return f"sh{code}"
        # 深交所: 00/30/15/16 开头
        return f"sz{code}"

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        通过腾讯财经接口获取日线历史数据。

        接口: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
        参数: param=<market><code>,day,,,,,<count>,qfq
        """
        self._rate_limit_sleep()

        tc_code = self._to_tencent_code(stock_code)
        # 腾讯接口支持一次性拉取多年数据，count 设为 2000 足够覆盖 A 股历史
        url = (
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?_var=kline_dayqfq&param={tc_code},day,,,,,2000,qfq"
        )

        headers = {
            'User-Agent': random.choice(self._USER_AGENTS),
            'Referer': 'https://finance.qq.com/',
        }

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, context=self._SSL_CTX, timeout=30) as resp:
                raw = resp.read().decode('utf-8', errors='replace')

        except Exception as e:
            self._classify_and_raise(e, stock_code)

        # 腾讯返回格式: var kline_dayqfq = {"data":{ "sh600519": { "day":[["2024-01-02", 开, 收, 高, 低, 量], ...] } }}
        try:
            # 去掉 "var xxx = " 前缀
            raw = raw.split('=', 1)[-1].strip()
            obj = json.loads(raw)
            tc_data = obj.get('data', {}).get(tc_code, {})
            day_list = tc_data.get('day', [])

            if not day_list:
                raise DataFetchError(
                    f"[TencentFetcher] 空数据: {stock_code}",
                    source=self.name, stock_code=stock_code
                )

            # 转换为 DataFrame
            df = pd.DataFrame(
                day_list,
                columns=['date', 'open', 'close', 'high', 'low', 'volume']
            )
            # 过滤日期范围
            df = df[df['date'] >= start_date]
            df = df[df['date'] <= end_date]

            return df

        except json.JSONDecodeError as e:
            raise DataSourceUnavailableError(
                f"[TencentFetcher] JSON解析失败: {stock_code}: {e}",
                source=self.name, stock_code=stock_code
            )
        except DataFetchError:
            raise
        except Exception as e:
            raise DataSourceUnavailableError(
                f"[TencentFetcher] {stock_code}: {e}",
                source=self.name, stock_code=stock_code
            )

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """腾讯原始列名 → 标准列名，并补充 amount/pct_chg"""
        df = df.copy()

        # 数值列
        for col in ['open', 'close', 'high', 'low', 'volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 成交额：腾讯日K没有单独的成交额字段，用成交量*均价估算
        if 'amount' not in df.columns:
            df['amount'] = (df['close'] * df['volume']).round(2)

        # 涨跌幅：通过收盘价计算（与前一交易日比）
        df = df.sort_values('date', ascending=True).reset_index(drop=True)
        df['pct_chg'] = df['close'].pct_change().fillna(0).round(4) * 100

        # 保留标准列
        cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']
        return df[[c for c in cols if c in df.columns]]

    def _rate_limit_sleep(self) -> None:
        """同域名 200ms 最低间隔（类级别共享）"""
        elapsed = time.time() - self._last_call_time
        if elapsed < 0.2:
            time.sleep(0.2 - elapsed)
        self.random_sleep(1.0, 3.0)  # 额外随机休眠
        self._last_call_time = time.time()

    @staticmethod
    def _classify_and_raise(e: Exception, stock_code: str) -> None:
        """根据异常类型分类并抛出对应异常"""
        msg = str(e).lower()
        code = getattr(e, 'code', 0)

        if code == 429 or '429' in msg or 'too many' in msg:
            raise RateLimitError(
                f"[TencentFetcher] 频率限制: {stock_code}",
                source="TencentFetcher", stock_code=stock_code
            )
        if code == 403 or '403' in msg or 'forbidden' in msg or 'ban' in msg:
            raise DataSourceUnavailableError(
                f"[TencentFetcher] 被封禁: {stock_code}",
                source="TencentFetcher", stock_code=stock_code
            )
        if 'timeout' in msg or 'timed out' in msg:
            raise DataSourceUnavailableError(
                f"[TencentFetcher] 超时: {stock_code}",
                source="TencentFetcher", stock_code=stock_code
            )
        raise DataSourceUnavailableError(
            f"[TencentFetcher] 连接失败: {stock_code}: {e}",
            source="TencentFetcher", stock_code=stock_code
        )
