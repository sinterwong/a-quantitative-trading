# -*- coding: utf-8 -*-
"""
tencent_hk_fetcher.py — 腾讯港股日线数据源
==========================================

优先级: 0（港股最优先）
数据源: 腾讯财经 web.ifzq.gtimg.cn 前复权日K线接口

特点:
  - 免费，无需 Token
  - 支持港股正股、指数、ETF
  - 前复权数据

代码格式:
  - hk00700（腾讯控股）
  - hkHSI（恒生指数）
  - HK:00700（自动转换）
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

from ..base_fetcher import BaseFetcher, normalize_stock_code
from ..data_fetch_exceptions import DataSourceUnavailableError, RateLimitError, DataFetchError

logger = logging.getLogger('tencent_hk_fetcher')


class TencentHKFetcher(BaseFetcher):
    """腾讯港股日线数据 fetcher（优先级 0）"""

    name = "TencentHKFetcher"
    priority = 0

    _SSL_CTX = ssl.create_default_context()
    _SSL_CTX.check_hostname = False
    _SSL_CTX.verify_mode = ssl.CERT_NONE

    _USER_AGENTS = [
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    ]

    _last_call_time: float = 0.0

    def _to_tencent_hk_code(self, stock_code: str) -> str:
        """
        将各种港股代码格式转换为腾讯格式。

        '00700' → 'hk00700'
        'HK:00700' → 'hk00700'
        'hk00700' → 'hk00700'
        'HSI' → 'hkHSI'
        """
        code = stock_code.strip()

        # HK:xxx 格式
        if code.upper().startswith("HK:"):
            inner = code[3:].strip()
            if inner.isdigit():
                return f"hk{inner.zfill(5)}"
            return f"hk{inner}"

        # hkxxx 格式
        if code.lower().startswith("hk"):
            return code.lower()

        # 纯数字
        if code.isdigit():
            return f"hk{code.zfill(5)}"

        # 纯字母（如 HSI, HSTECH）
        if code.isalpha():
            return f"hk{code.upper()}"

        return f"hk{code}"

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        通过腾讯财经接口获取港股日线历史数据。

        接口: https://web.ifzq.gtimg.cn/appstock/app/fqkline/get
        参数: param=hk00700,day,start_date,end_date,count,qfq
        """
        self._rate_limit_sleep()

        tc_code = self._to_tencent_hk_code(stock_code)

        # 腾讯 API 要求 YYYY-MM-DD 格式，统一转换
        def _fmt_date(d: str) -> str:
            d = d.strip()
            if len(d) == 8 and d.isdigit():
                return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
            return d

        sd = _fmt_date(start_date)
        ed = _fmt_date(end_date)

        # 腾讯港股接口：最多返回约 120 根日 K
        url = (
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?_var=kline_dayqfq&param={tc_code},day,{sd},{ed},2000,qfq"
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

        try:
            # 去掉 "var xxx = " 前缀
            raw = raw.split('=', 1)[-1].strip().rstrip(';')
            obj = json.loads(raw)
            tc_data = obj.get('data', {}).get(tc_code, {})

            # 查找 K 线数据 key（qfqday / day）
            bars = tc_data.get('qfqday') or tc_data.get('day') or []
            if not bars:
                raise DataFetchError(
                    f"[TencentHKFetcher] 空数据: {stock_code}",
                    source=self.name, stock_code=stock_code
                )

            # 转换为 DataFrame（港股 bar 可能有 7 个字段，第 7 个是分红信息 dict）
            rows = []
            for bar in bars:
                if len(bar) >= 6:
                    rows.append(bar[:6])
            df = pd.DataFrame(
                rows,
                columns=['date', 'open', 'close', 'high', 'low', 'volume']
            )
            # 过滤日期范围（API 返回 YYYY-MM-DD 格式，filter 也用同格式）
            df = df[df['date'] >= sd]
            df = df[df['date'] <= ed]

            return df

        except json.JSONDecodeError as e:
            raise DataSourceUnavailableError(
                f"[TencentHKFetcher] JSON解析失败: {stock_code}: {e}",
                source=self.name, stock_code=stock_code
            )
        except DataFetchError:
            raise
        except Exception as e:
            raise DataSourceUnavailableError(
                f"[TencentHKFetcher] {stock_code}: {e}",
                source=self.name, stock_code=stock_code
            )

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """腾讯原始列名 → 标准列名，并补充 amount/pct_chg"""
        df = df.copy()

        # 数值列
        for col in ['open', 'close', 'high', 'low', 'volume']:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # 成交额：腾讯港股日 K 没有单独的成交额字段，用成交量*均价估算
        if 'amount' not in df.columns:
            df['amount'] = (df['close'] * df['volume']).round(2)

        # 涨跌幅：通过收盘价计算
        df = df.sort_values('date', ascending=True).reset_index(drop=True)
        df['pct_chg'] = df['close'].pct_change().fillna(0).round(4) * 100

        # 保留标准列
        cols = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount', 'pct_chg']
        return df[[c for c in cols if c in df.columns]]

    def _rate_limit_sleep(self) -> None:
        """同域名 200ms 最低间隔"""
        elapsed = time.time() - self._last_call_time
        if elapsed < 0.2:
            time.sleep(0.2 - elapsed)
        self.random_sleep(1.0, 3.0)
        self._last_call_time = time.time()

    @staticmethod
    def _classify_and_raise(e: Exception, stock_code: str) -> None:
        """根据异常类型分类并抛出对应异常"""
        msg = str(e).lower()
        code = getattr(e, 'code', 0)

        if code == 429 or '429' in msg or 'too many' in msg:
            raise RateLimitError(
                f"[TencentHKFetcher] 频率限制: {stock_code}",
                source="TencentHKFetcher", stock_code=stock_code
            )
        if code == 403 or '403' in msg or 'forbidden' in msg or 'ban' in msg:
            raise DataSourceUnavailableError(
                f"[TencentHKFetcher] 被封禁: {stock_code}",
                source="TencentHKFetcher", stock_code=stock_code
            )
        if 'timeout' in msg or 'timed out' in msg:
            raise DataSourceUnavailableError(
                f"[TencentHKFetcher] 超时: {stock_code}",
                source="TencentHKFetcher", stock_code=stock_code
            )
        raise DataSourceUnavailableError(
            f"[TencentHKFetcher] 连接失败: {stock_code}: {e}",
            source="TencentHKFetcher", stock_code=stock_code
        )
