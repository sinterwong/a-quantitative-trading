"""
tests/test_data_layer.py — DataLayer 验收测试

覆盖:
  1. Quote / NorthFlowSnapshot 数据类行为
  2. DataLayer 转发到 gateway 的契约(mock gateway)
  3. BacktestDataLayer 前视偏差防护(核心验收)
  4. 全局单例 get_data_layer()

DataLayer 自身的 TTL 缓存已下沉到 data_gateway 层(MemoryCache),
对应的缓存单元测试在 tests/test_data_gateway/test_cache.py。
"""

import sys
import os
import threading
import traceback
from datetime import datetime
from unittest.mock import MagicMock, patch

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.data_layer import (
    Quote,
    NorthFlowSnapshot,
    DataLayer,
    BacktestDataLayer,
    get_data_layer,
    reset_data_layer,
)
from core.data_gateway.symbols import normalize_to_tencent
from core.data_gateway.schemas import Quote as GwQuote
from core.data_gateway.schemas import NorthFlow as GwNorthFlow

# ─── 测试框架（最小实现，无外部依赖）────────────────────────────────────────

_passed = 0
_failed = 0
_errors = []


def _check(cond: bool, msg: str):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS: {msg}")
    else:
        _failed += 1
        print(f"  FAIL: {msg}")
        _errors.append(msg)


def _section(name: str):
    print(f"\n=== {name} ===")


def _run_method(obj, name: str):
    global _passed, _failed
    setup = getattr(obj, "setup_method", None)
    teardown = getattr(obj, "teardown_method", None)
    if setup:
        setup()
    ok = False
    try:
        getattr(obj, name)()
        ok = True
    except AssertionError as e:
        _failed += 1
        print(f"  FAIL: {type(obj).__name__}.{name}: {e}")
        _errors.append(f"{type(obj).__name__}.{name}: {e}")
    except Exception as e:
        _failed += 1
        print(f"  ERROR: {type(obj).__name__}.{name}: {e}")
        _errors.append(f"{type(obj).__name__}.{name}: {traceback.format_exc()}")
    finally:
        if teardown:
            teardown()
    if ok:
        _passed += 1
        print(f"  PASS: {type(obj).__name__}.{name}")


def _run_class(cls):
    _section(cls.__name__)
    obj = cls()
    for name in sorted(dir(obj)):
        if name.startswith("test_"):
            _run_method(obj, name)


# ─── 辅助：构造合成数据 ──────────────────────────────────────────────────────


def _make_bar_df(n: int = 60, start: str = "2024-01-02") -> pd.DataFrame:
    import numpy as np
    dates = pd.date_range(start, periods=n, freq="B")
    np.random.seed(42)
    close = 10.0 + np.cumsum(np.random.randn(n) * 0.1)
    close = close.clip(min=1.0)
    return pd.DataFrame({
        "date":   dates,
        "open":   close * 0.99,
        "high":   close * 1.01,
        "low":    close * 0.98,
        "close":  close,
        "volume": (np.random.rand(n) * 1e6 + 1e5).astype(int),
    })


def _make_gw_quote(symbol: str = "sh510310") -> GwQuote:
    return GwQuote(
        symbol=symbol, name="测试", code=symbol[2:], market="A",
        price=5.0, prev_close=4.9, open=5.0, high=5.1, low=4.8,
        change=0.1, pct_change=2.04,
        volume=100000, amount=500000, turnover_rate=1.0,
        bid1_price=4.99, bid1_vol=100, ask1_price=5.01, ask1_vol=100,
        volume_ratio=1.2, currency="CNY",
    )


def _mock_gateway(*, quote_value=None, quotes_value=None, kline_value=None,
                  north_value=None, macro_value=None):
    mock = MagicMock()
    mock.quote.return_value = quote_value
    mock.quotes.return_value = quotes_value or {}
    mock.kline.return_value = kline_value if kline_value is not None else pd.DataFrame()
    mock.north_flow.return_value = north_value
    mock.macro.return_value = macro_value if macro_value is not None else pd.DataFrame()
    return mock


# ══════════════════════════════════════════════════════════════════════════════


class TestQuote:
    def test_day_change(self):
        q = Quote("600519.SH", price=1810.0, prev_close=1780.0,
                  pct_change=1.69, high=1820.0, low=1760.0)
        assert abs(q.day_change - 30.0) < 0.01

    def test_is_limit_up_false(self):
        q = Quote("510310.SH", price=5.0, prev_close=4.8,
                  pct_change=4.17, high=5.1, low=4.9)
        assert not q.is_limit_up

    def test_is_limit_up_true(self):
        q = Quote("000001.SZ", price=10.99, prev_close=9.99,
                  pct_change=10.01, high=10.99, low=10.00)
        assert q.is_limit_up

    def test_is_limit_down_true(self):
        q = Quote("000001.SZ", price=8.99, prev_close=9.99,
                  pct_change=-10.01, high=9.99, low=8.99)
        assert q.is_limit_down

    def test_vol_ratio_optional(self):
        q = Quote("600900.SH", price=25.0, prev_close=24.5,
                  pct_change=2.04, high=25.5, low=24.3)
        assert q.vol_ratio is None


class TestNorthFlowSnapshot:
    def test_is_strong_inflow_true(self):
        snap = NorthFlowSnapshot(net_north_yi=60.0)
        assert snap.is_strong_inflow(50.0)

    def test_is_strong_inflow_false(self):
        snap = NorthFlowSnapshot(net_north_yi=30.0)
        assert not snap.is_strong_inflow(50.0)

    def test_neutral_direction(self):
        snap = NorthFlowSnapshot(net_north_yi=0.0, direction="NEUTRAL")
        assert snap.direction == "NEUTRAL"


class TestNormalizeToTencent:
    def test_sh(self):
        assert normalize_to_tencent("600519.SH") == "sh600519"

    def test_sz(self):
        assert normalize_to_tencent("000001.SZ") == "sz000001"

    def test_lowercase_suffix(self):
        assert normalize_to_tencent("510310.sh") == "sh510310"

    def test_etf_sh(self):
        assert normalize_to_tencent("510310.SH") == "sh510310"


class TestDataLayerForwarding:
    """验证 DataLayer 正确转发到 gateway,字段映射保持向后兼容。"""

    def test_get_realtime_forwards_to_gateway(self):
        gw = _mock_gateway(quote_value=_make_gw_quote("sh510310"))
        layer = DataLayer(use_parquet_cache=False)
        layer._gw = gw
        q = layer.get_realtime("510310.SH")
        gw.quote.assert_called_once_with("510310.SH")
        assert q is not None
        # 业务侧 Quote.symbol 保留调用方原始格式(不强制归一)
        assert q.symbol == "510310.SH"
        assert q.price == 5.0
        assert q.vol_ratio == 1.2  # gateway.volume_ratio 映射

    def test_get_realtime_bulk_forwards(self):
        gw = _mock_gateway(quotes_value={
            "sh510310": _make_gw_quote("sh510310"),
            "sh600519": _make_gw_quote("sh600519"),
        })
        layer = DataLayer(use_parquet_cache=False)
        layer._gw = gw
        out = layer.get_realtime_bulk(["sh510310", "sh600519"])
        gw.quotes.assert_called_once()
        assert set(out) == {"sh510310", "sh600519"}

    def test_get_realtime_returns_none_on_miss(self):
        gw = _mock_gateway(quote_value=None)
        layer = DataLayer(use_parquet_cache=False)
        layer._gw = gw
        assert layer.get_realtime("NOTEXIST.SH") is None

    def test_zero_optional_fields_become_none(self):
        """gateway.Quote 中 0.0 的可选字段在业务 Quote 中应为 None。"""
        gq = _make_gw_quote()
        gq.pe_ttm = 0.0
        gq.pb = 0.0
        gw = _mock_gateway(quote_value=gq)
        layer = DataLayer(use_parquet_cache=False)
        layer._gw = gw
        q = layer.get_realtime("510310.SH")
        assert q.pe_ttm is None
        assert q.pb is None

    def test_get_bars_forwards_to_gateway_kline(self):
        layer = DataLayer(use_parquet_cache=False)
        df = _make_bar_df(60)
        gw = _mock_gateway(kline_value=df)
        layer._gw = gw
        out = layer.get_bars("510310.SH", days=30)
        # gateway.kline 被调用
        gw.kline.assert_called_once()
        call_kw = gw.kline.call_args
        assert call_kw.kwargs.get("interval") == "daily"
        assert isinstance(out, pd.DataFrame)
        assert {"date", "open", "high", "low", "close", "volume"}.issubset(out.columns)

    def test_get_bars_empty_returns_typed_empty(self):
        layer = DataLayer(use_parquet_cache=False)
        gw = _mock_gateway(kline_value=pd.DataFrame())
        layer._gw = gw
        out = layer.get_bars("X.SH", days=30)
        assert out.empty
        assert set(out.columns) == {"date", "open", "high", "low", "close", "volume"}

    def test_get_minute_bars_forwards(self):
        layer = DataLayer(use_parquet_cache=False)
        df_min = pd.DataFrame({"datetime": [datetime.now()], "open": [1.0],
                               "high": [1.1], "low": [0.9], "close": [1.05],
                               "volume": [100]})
        gw = _mock_gateway(kline_value=df_min)
        layer._gw = gw
        out = layer.get_minute_bars("510310.SH", period="5")
        gw.kline.assert_called_once()
        assert gw.kline.call_args.kwargs.get("interval") == "5m"
        assert not out.empty

    def test_get_north_flow_forwards(self):
        layer = DataLayer(use_parquet_cache=False)
        gw = _mock_gateway(north_value=GwNorthFlow(
            net_north_yi=12.3, net_south_yi=-1.0, direction="BUY",
        ))
        layer._gw = gw
        snap = layer.get_north_flow()
        gw.north_flow.assert_called_once()
        assert snap.net_north_yi == 12.3
        assert snap.direction == "BUY"

    def test_get_north_flow_none_returns_default(self):
        layer = DataLayer(use_parquet_cache=False)
        gw = _mock_gateway(north_value=None)
        layer._gw = gw
        snap = layer.get_north_flow()
        assert snap.direction == "NEUTRAL"

    def test_get_macro_forwards(self):
        layer = DataLayer(use_parquet_cache=False)
        df = pd.DataFrame({"pmi": [50.5]}, index=pd.to_datetime(["2026-05-01"]))
        gw = _mock_gateway(macro_value=df)
        layer._gw = gw
        out = layer.get_macro_data("PMI")
        gw.macro.assert_called_once_with("PMI")
        assert not out.empty

    def test_invalidate_clears_gateway_cache(self):
        layer = DataLayer(use_parquet_cache=False)
        gw = _mock_gateway()
        layer._gw = gw
        layer.invalidate()
        gw.invalidate_cache.assert_called_once()


class TestBacktestDataLayer:
    """BacktestDataLayer — 核心验收：前视偏差防护"""

    def _make_layer(self, n: int = 60) -> BacktestDataLayer:
        df = _make_bar_df(n, start="2024-01-02")
        return BacktestDataLayer({"510310.SH": df})

    def test_get_bars_no_date_returns_last_n(self):
        layer = self._make_layer(60)
        result = layer.get_bars("510310.SH", days=30)
        assert len(result) == 30

    def test_no_lookahead_bias(self):
        layer = self._make_layer(60)
        all_dates = layer.available_dates("510310.SH")
        cutoff = all_dates[20]

        layer.set_date(cutoff)
        bars = layer.get_bars("510310.SH", days=60)

        future = bars[bars["date"] > cutoff]
        assert len(future) == 0, f"存在前视偏差！泄露了 {len(future)} 条未来数据"

    def test_get_bars_respects_cutoff_date(self):
        layer = self._make_layer(60)
        all_dates = layer.available_dates("510310.SH")
        cutoff = all_dates[30]

        layer.set_date(cutoff)
        bars = layer.get_bars("510310.SH", days=60)
        assert bars["date"].max() <= cutoff
        assert (bars["date"] == cutoff).any()

    def test_get_bars_days_limit(self):
        layer = self._make_layer(60)
        result = layer.get_bars("510310.SH", days=10)
        assert len(result) <= 10

    def test_get_bars_unknown_symbol(self):
        layer = self._make_layer(10)
        result = layer.get_bars("UNKNOWN.SH", days=30)
        assert result.empty

    def test_get_realtime_returns_current_date_close(self):
        layer = self._make_layer(60)
        all_dates = layer.available_dates("510310.SH")
        cutoff = all_dates[20]
        layer.set_date(cutoff)

        q = layer.get_realtime("510310.SH")
        assert q is not None
        df = _make_bar_df(60, start="2024-01-02")
        expected = float(df[df["date"] == cutoff]["close"].iloc[0])
        assert abs(q.price - expected) < 0.001

    def test_get_realtime_returns_none_before_data(self):
        layer = self._make_layer(10)
        layer.set_date("2000-01-01")
        assert layer.get_realtime("510310.SH") is None

    def test_get_realtime_bulk(self):
        df1 = _make_bar_df(30, start="2024-01-02")
        df2 = _make_bar_df(30, start="2024-01-02")
        layer = BacktestDataLayer({"510310.SH": df1, "600900.SH": df2})
        result = layer.get_realtime_bulk(["510310.SH", "600900.SH"])
        assert "510310.SH" in result
        assert "600900.SH" in result

    def test_get_north_flow_always_neutral(self):
        layer = self._make_layer(10)
        snap = layer.get_north_flow()
        assert snap.direction == "NEUTRAL"
        assert snap.stale is True

    def test_available_dates_sorted_ascending(self):
        layer = self._make_layer(20)
        dates = layer.available_dates("510310.SH")
        assert dates == sorted(dates)
        assert len(dates) == 20

    def test_set_date_from_string(self):
        layer = self._make_layer(30)
        layer.set_date("2024-02-01")
        assert layer.current_date == pd.Timestamp("2024-02-01")

    def test_string_date_column_normalized(self):
        df = _make_bar_df(20, start="2024-01-02")
        df["date"] = df["date"].astype(str)
        layer = BacktestDataLayer({"510310.SH": df})
        bars = layer.get_bars("510310.SH", days=10)
        assert pd.api.types.is_datetime64_any_dtype(bars["date"])

    def test_multiple_symbols_independent_cutoffs(self):
        df1 = _make_bar_df(60, start="2024-01-02")
        df2 = _make_bar_df(60, start="2024-01-02")
        layer = BacktestDataLayer({"A.SH": df1, "B.SH": df2})
        dates_a = layer.available_dates("A.SH")
        cutoff = dates_a[20]
        layer.set_date(cutoff)

        bars_a = layer.get_bars("A.SH", days=60)
        bars_b = layer.get_bars("B.SH", days=60)
        assert bars_a["date"].max() <= cutoff
        assert bars_b["date"].max() <= cutoff


class TestGlobalSingleton:
    def setup_method(self):
        reset_data_layer()

    def teardown_method(self):
        reset_data_layer()

    def test_returns_same_instance(self):
        a = get_data_layer()
        b = get_data_layer()
        assert a is b

    def test_reset_creates_new_instance(self):
        a = get_data_layer()
        reset_data_layer()
        b = get_data_layer()
        assert a is not b

    def test_instance_is_data_layer(self):
        assert isinstance(get_data_layer(), DataLayer)


# ─── 直接运行（无 pytest）────────────────────────────────────────────────────


def run_all():
    test_classes = [
        TestQuote,
        TestNorthFlowSnapshot,
        TestNormalizeToTencent,
        TestDataLayerForwarding,
        TestBacktestDataLayer,
        TestGlobalSingleton,
    ]
    for cls in test_classes:
        _run_class(cls)

    print(f"\n{'='*60}")
    print(f"DataLayer: {_passed} passed, {_failed} failed")
    if _errors:
        print("Failed:")
        for e in _errors:
            print(f"  - {e}")
    return _failed


if __name__ == "__main__":
    fails = run_all()
    sys.exit(1 if fails else 0)
