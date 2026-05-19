"""
core/data_layer.py — 统一数据层(薄外观)

为业务侧提供旧接口形态(Quote / NorthFlowSnapshot / DataLayer / BacktestDataLayer),
内部全部转发到 core.data_gateway,不再持有任何网络抓取/HTTP 代码。

两种运行模式:
  DataLayer         — 实盘模式,转发到 gateway
  BacktestDataLayer — 回测模式,从注入 DataFrame 读取,防前视偏差

历史 Parquet 缓存(data/bars/*.parquet)仍由本模块管理,
作为日 K 线的本地长期归档,与 gateway 层的短期 TTL 缓存职责分开。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd

from .data_gateway import get_gateway
from .data_gateway.capabilities import MacroIndicator
from .data_gateway.schemas import NorthFlow as _GwNorthFlow
from .data_gateway.schemas import Quote as _GwQuote

logger = logging.getLogger("core.data_layer")


# ─── 业务数据类(向后兼容) ─────────────────────────────────────────────────────


@dataclass
class Quote:
    """实时行情快照(业务侧形态,Optional 字段表示"数据缺失")。"""

    symbol: str
    price: float
    prev_close: float
    pct_change: float
    high: float
    low: float
    vol_ratio: Optional[float] = None
    pe_ttm: Optional[float] = None
    pb: Optional[float] = None
    turnover_rate: Optional[float] = None
    market_cap: Optional[float] = None
    float_cap: Optional[float] = None
    high_52w: Optional[float] = None
    low_52w: Optional[float] = None
    limit_up: Optional[float] = None
    limit_down: Optional[float] = None
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def day_change(self) -> float:
        return self.price - self.prev_close

    @property
    def is_limit_up(self) -> bool:
        return self.pct_change >= 9.9

    @property
    def is_limit_down(self) -> bool:
        return self.pct_change <= -9.9


@dataclass
class NorthFlowSnapshot:
    """北向资金快照(业务侧形态)。"""

    net_north_yi: float = 0.0
    net_south_yi: float = 0.0
    direction: str = "NEUTRAL"
    source: str = "unknown"
    stale: bool = False
    timestamp: datetime = field(default_factory=datetime.now)

    def is_strong_inflow(self, threshold_yi: float = 50.0) -> bool:
        return self.net_north_yi >= threshold_yi


# ─── gateway.Quote → 业务 Quote 转换 ─────────────────────────────────────────


def _opt_pos(value: float) -> Optional[float]:
    """只有正值才视为"有数据",0 视为缺失(对应业务侧 Optional 语义)。"""
    return value if value and value > 0 else None


def _gw_quote_to_quote(symbol: str, gq: Optional[_GwQuote]) -> Optional[Quote]:
    if gq is None or not gq.is_valid:
        return None
    return Quote(
        symbol=symbol,
        price=gq.price,
        prev_close=gq.prev_close,
        pct_change=gq.pct_change,
        high=gq.high,
        low=gq.low,
        vol_ratio=_opt_pos(gq.volume_ratio),
        pe_ttm=_opt_pos(gq.pe_ttm),
        pb=_opt_pos(gq.pb),
        turnover_rate=_opt_pos(gq.turnover_rate),
        market_cap=_opt_pos(gq.market_cap),
        float_cap=_opt_pos(gq.float_cap),
        high_52w=_opt_pos(gq.high_52w),
        low_52w=_opt_pos(gq.low_52w),
        limit_up=_opt_pos(gq.limit_up),
        limit_down=_opt_pos(gq.limit_down),
        timestamp=gq.timestamp,
    )


def _gw_north_to_snapshot(gn: Optional[_GwNorthFlow]) -> NorthFlowSnapshot:
    if gn is None:
        return NorthFlowSnapshot()
    return NorthFlowSnapshot(
        net_north_yi=gn.net_north_yi,
        net_south_yi=gn.net_south_yi,
        direction=gn.direction,
        source="gateway",
        stale=gn.stale,
        timestamp=gn.timestamp,
    )


# ─── Parquet 历史归档(数据/bars 目录,与 gateway 短期缓存职责分开) ──────────


_PARQUET_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "bars")


class ParquetCache:
    """日 K 历史数据本地长期归档。

    首次 fetch 后写入 data/bars/{symbol}.parquet,后续合并/查询。
    与 gateway 内 30s TTL 短期缓存不同,这里是月级历史归档。
    """

    def __init__(self, cache_dir: str = _PARQUET_DIR):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def _path(self, symbol: str) -> str:
        safe = symbol.replace(".", "_").replace("/", "_")
        return os.path.join(self.cache_dir, f"{safe}.parquet")

    def exists(self, symbol: str) -> bool:
        return os.path.isfile(self._path(symbol))

    def load(self, symbol: str) -> Optional[pd.DataFrame]:
        path = self._path(symbol)
        if not os.path.isfile(path):
            return None
        try:
            df = pd.read_parquet(path)
            if not pd.api.types.is_datetime64_any_dtype(df.index):
                df.index = pd.to_datetime(df.index)
            return df.sort_index()
        except Exception as exc:
            logger.warning("Parquet load failed for %s: %s", symbol, exc)
            return None

    def save(self, symbol: str, df: pd.DataFrame) -> bool:
        if df is None or df.empty:
            return False
        try:
            out = df.copy()
            if not pd.api.types.is_datetime64_any_dtype(out.index):
                out.index = pd.to_datetime(out.index)
            out.to_parquet(self._path(symbol), engine="pyarrow", compression="snappy")
            return True
        except Exception as exc:
            logger.warning("Parquet save failed for %s: %s", symbol, exc)
            return False

    def upsert(self, symbol: str, df_new: pd.DataFrame) -> pd.DataFrame:
        df_old = self.load(symbol)
        if df_old is not None and not df_old.empty:
            combined = pd.concat([df_old, df_new])
            combined = combined[~combined.index.duplicated(keep="last")]
            combined = combined.sort_index()
        else:
            combined = df_new.sort_index()
        self.save(symbol, combined)
        return combined

    def latest_date(self, symbol: str) -> Optional[pd.Timestamp]:
        df = self.load(symbol)
        if df is None or df.empty:
            return None
        return df.index[-1]

    def delete(self, symbol: str) -> bool:
        path = self._path(symbol)
        if os.path.isfile(path):
            os.remove(path)
            return True
        return False


# ─── DataLayer(实盘模式) ────────────────────────────────────────────────────


class DataLayer:
    """实盘统一数据接口 — 转发到 data_gateway。

    Parquet 归档(月级)在本层保留,gateway 短期 TTL 缓存(秒-分级)在网关层。
    """

    BAR_TTL = 3600
    MACRO_TTL = 86400

    def __init__(self, use_parquet_cache: bool = True):
        self._parquet = ParquetCache() if use_parquet_cache else None
        self._gw = get_gateway()

    # ── 日 K 线 ─────────────────────────────────────────────────────────────

    def get_bars(self, symbol: str, days: int = 60) -> pd.DataFrame:
        """获取日 K 线(前复权)。优先用 Parquet 归档,过旧/缺失时走 gateway。"""
        df = None
        if self._parquet is not None:
            cached_df = self._parquet.load(symbol)
            if cached_df is not None and not cached_df.empty:
                latest = self._parquet.latest_date(symbol)
                today = pd.Timestamp.now().normalize()
                if latest is not None and (today - latest).days <= 3:
                    df = cached_df

        if df is None:
            df_net = self._gw.kline(symbol, interval="daily", days=max(days, 365))
            if df_net is not None and not df_net.empty:
                df_net_idx = df_net.copy()
                if "date" in df_net_idx.columns:
                    df_net_idx = df_net_idx.set_index("date")
                if not pd.api.types.is_datetime64_any_dtype(df_net_idx.index):
                    df_net_idx.index = pd.to_datetime(df_net_idx.index)
                if self._parquet is not None:
                    df = self._parquet.upsert(symbol, df_net_idx)
                else:
                    df = df_net_idx

        if df is None or df.empty:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])

        result = df.tail(days).copy()
        # 始终保留 DatetimeIndex，date 列作为兼容字段（部分旧代码依赖它）
        if "date" not in result.columns:
            if isinstance(result.index, pd.DatetimeIndex):
                result.insert(0, "date", result.index)
            else:
                result = result.reset_index().rename(columns={"index": "date"})
        # 旧代码依赖 'index' 列名 → 兼容
        if "index" in result.columns and "date" not in result.columns:
            result = result.rename(columns={"index": "date"})
        return result

    def get_minute_bars(
        self,
        symbol: str,
        period: str = "1",
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """分钟 K 线。period 形如 '1'/'5'/'15'/'30'/'60'(分钟)。"""
        interval = f"{period}m" if not period.endswith("m") else period
        df = self._gw.kline(symbol, interval=interval, limit=200)
        if df is None or df.empty:
            return pd.DataFrame(
                columns=["datetime", "open", "high", "low", "close", "volume"]
            )
        return df

    # ── 实时行情 ─────────────────────────────────────────────────────────────

    def get_realtime(self, symbol: str) -> Optional[Quote]:
        gq = self._gw.quote(symbol)
        return _gw_quote_to_quote(symbol, gq)

    def get_realtime_bulk(self, symbols: List[str]) -> Dict[str, Quote]:
        if not symbols:
            return {}
        batch = self._gw.quotes(symbols)
        out: Dict[str, Quote] = {}
        for sym, gq in batch.items():
            q = _gw_quote_to_quote(sym, gq)
            if q is not None:
                out[sym] = q
        return out

    # ── 北向资金 ─────────────────────────────────────────────────────────────

    def get_north_flow(self) -> NorthFlowSnapshot:
        return _gw_north_to_snapshot(self._gw.north_flow())

    # ── 宏观数据 ─────────────────────────────────────────────────────────────

    def get_macro_data(self, indicator: MacroIndicator) -> pd.DataFrame:
        """indicator: MacroIndicator enum (PMI / M2 / CREDIT / CPI / PPI)。"""
        return self._gw.macro(indicator)

    # ── 缓存管理 ─────────────────────────────────────────────────────────────

    def invalidate(self, symbol: Optional[str] = None) -> None:
        """清除 gateway 层缓存。"""
        # gateway 层短期缓存按 symbol 单独清不方便,直接清全部
        self._gw.invalidate_cache()


# ─── BacktestDataLayer(回测模式) ─────────────────────────────────────────────


class BacktestDataLayer(DataLayer):
    """回测专用:从注入 DataFrame 读取,禁止前视。"""

    def __init__(self, data: Dict[str, pd.DataFrame]):
        super().__init__(use_parquet_cache=False)
        self._data: Dict[str, pd.DataFrame] = {}
        for sym, df in data.items():
            df = df.copy()
            if not pd.api.types.is_datetime64_any_dtype(df["date"]):
                df["date"] = pd.to_datetime(df["date"])
            df = df.sort_values("date").reset_index(drop=True)
            self._data[sym] = df
        self._current_date: Optional[pd.Timestamp] = None

    def set_date(self, dt) -> None:
        self._current_date = pd.Timestamp(dt)

    @property
    def current_date(self) -> Optional[pd.Timestamp]:
        return self._current_date

    def get_bars(self, symbol: str, days: int = 60) -> pd.DataFrame:
        df = self._data.get(symbol)
        if df is None or df.empty:
            return pd.DataFrame(
                columns=["date", "open", "high", "low", "close", "volume"]
            )
        if self._current_date is not None:
            df = df[df["date"] <= self._current_date]
        return df.tail(days).reset_index(drop=True)

    def get_realtime(self, symbol: str) -> Optional[Quote]:
        df = self.get_bars(symbol, days=1)
        if df.empty:
            return None
        row = df.iloc[-1]
        return Quote(
            symbol=symbol,
            price=float(row["close"]),
            prev_close=float(row["close"]),
            pct_change=0.0,
            high=float(row["high"]),
            low=float(row["low"]),
            vol_ratio=None,
            timestamp=(
                row["date"].to_pydatetime()
                if hasattr(row["date"], "to_pydatetime")
                else datetime.now()
            ),
        )

    def get_realtime_bulk(self, symbols: List[str]) -> Dict[str, Quote]:
        out: Dict[str, Quote] = {}
        for sym in symbols:
            q = self.get_realtime(sym)
            if q is not None:
                out[sym] = q
        return out

    def get_north_flow(self) -> NorthFlowSnapshot:
        return NorthFlowSnapshot(direction="NEUTRAL", stale=True)

    def available_dates(self, symbol: str) -> List[pd.Timestamp]:
        df = self._data.get(symbol)
        if df is None:
            return []
        return df["date"].tolist()


# ─── 全局单例 ──────────────────────────────────────────────────────────────────


_global_layer: Optional[DataLayer] = None
_global_lock = threading.Lock()


def get_data_layer() -> DataLayer:
    global _global_layer
    with _global_lock:
        if _global_layer is None:
            _global_layer = DataLayer()
    return _global_layer


def reset_data_layer() -> None:
    global _global_layer
    with _global_lock:
        _global_layer = None
