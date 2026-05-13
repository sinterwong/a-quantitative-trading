# -*- coding: utf-8 -*-
"""
data_gateway.providers.yfinance — yfinance 兜底数据源(全球指数/期货/VIX)

能力矩阵:
  - MARKET_INDEX: VIX / ES=F / NQ=F / ^HSI 等无法走腾讯的全球行情
  - KLINE_DAILY: 同上,作为外盘 K 线兜底

仅在腾讯/新浪不覆盖时使用,priority_hint 偏低。
yfinance 是库不是 HTTP API,所以本 provider 不使用 HttpClient
(yfinance 内部自己管 HTTP),但仍统一暴露为 Provider 接口。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

from ..capabilities import Capability, Market, ProviderCapability
from ..schemas import MarketIndexSnapshot
from .base import Provider, ProviderError

logger = logging.getLogger("data_gateway.yfinance")


class YfinanceProvider(Provider):
    """yfinance 库兜底(VIX / ES=F / NQ=F / ^HSI 等)。"""

    name = "yfinance"

    def declare(self) -> ProviderCapability:
        return ProviderCapability(
            capabilities=frozenset({
                Capability.MARKET_INDEX,
                Capability.KLINE_DAILY,
            }),
            markets=frozenset({Market.US, Market.GLOBAL}),
            priority_hint=0.50,  # 延迟大,作为兜底
        )

    def fetch_market_index(self, code: str) -> Optional[MarketIndexSnapshot]:
        try:
            import yfinance as yf
        except ImportError:
            logger.debug("yfinance 未安装,跳过")
            return None

        try:
            ticker = yf.Ticker(code)
            hist = ticker.history(period="2d", auto_adjust=True)
        except Exception as exc:
            raise ProviderError(f"yfinance.fetch_market_index({code}): {exc}") from exc

        if hist is None or hist.empty:
            return None
        latest = hist.iloc[-1]
        prev = hist.iloc[-2] if len(hist) > 1 else latest
        close = float(latest["Close"])
        prev_close = float(prev["Close"])
        pct = ((close - prev_close) / prev_close * 100) if prev_close else 0
        return MarketIndexSnapshot(
            code=code,
            name=code,
            price=close,
            prev_close=prev_close,
            change_pct=round(pct, 3),
            timestamp=datetime.now(),
        )

    def fetch_kline_daily(
        self,
        symbol: str,
        days: int = 120,
        adjust: str = "qfq",
        limit: int = 100,
    ) -> pd.DataFrame:
        """日 K 线（仅 daily，yfinance 不支持分钟 K）。"""
        try:
            import yfinance as yf
        except ImportError:
            return pd.DataFrame()
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period=f"{days + 5}d", auto_adjust=True)
        except Exception as exc:
            raise ProviderError(f"yfinance.fetch_kline_daily({symbol}): {exc}") from exc

        if hist is None or hist.empty:
            return pd.DataFrame()

        df = hist.tail(days).reset_index()
        df = df.rename(columns={
            "Date": "date", "Open": "open", "High": "high",
            "Low": "low", "Close": "close", "Volume": "volume",
        })
        keep = [c for c in ["date", "open", "high", "low", "close", "volume"] if c in df.columns]
        return df[keep]

    def fetch_kline_minute(
        self,
        symbol: str,
        interval: str = "5m",
        limit: int = 100,
    ) -> pd.DataFrame:
        """yfinance 不支持分钟 K。"""
        return pd.DataFrame()


__all__ = ["YfinanceProvider"]
