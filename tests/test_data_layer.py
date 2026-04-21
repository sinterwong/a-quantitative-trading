"""
tests/test_data_layer.py — Phase 1 验收测试：DataLayer

兼容两种运行方式：
  python tests/test_data_layer.py        (无需 pytest)
  pytest tests/test_data_layer.py -v     (有 pytest 时)

覆盖：
  1. Quote / NorthFlowSnapshot 数据类行为
  2. _TTLCache 线程安全 + TTL 过期
  3. _parse_tencent_quote 解析正确性
  4. DataLayer.get_realtime_bulk — 缓存命中（不发网络）
  5. DataLayer.get_bars — 缓存命中、降级、空结果
  6. DataLayer.invalidate — 清除缓存后重新请求
  7. BacktestDataLayer — 前视偏差防护（核心验收）
  8. BacktestDataLayer — 实时行情、批量、北向、可用日期
  9. 全局单例 get_data_layer()
"""

import sys
import os
import time
import threading
import traceback
from datetime import datetime
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.data_layer import (
    Quote,
    NorthFlowSnapshot,
    _TTLCache,
    _symbol_to_tencent,
    _parse_tencent_quote,
    DataLayer,
    BacktestDataLayer,
    get_data_layer,
    reset_data_layer,
)

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
    """运行单个测试方法，捕获异常作为失败"""
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
    """运行测试类中的所有 test_* 方法"""
    _section(cls.__name__)
    obj = cls()
    for name in sorted(dir(obj)):
        if name.startswith("test_"):
            _run_method(obj, name)


# ─── 辅助：构造合成数据 ──────────────────────────────────────────────────────

def _make_bar_df(n: int = 60, start: str = "2024-01-02") -> pd.DataFrame:
    """合成日K线 DataFrame（n 行工作日）"""
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


def _make_tencent_raw_line(
    symbol: str = "600519.SH",
    price: float = 1800.0,
    prev_close: float = 1780.0,
    pct: float = 1.12,
    high: float = 1820.0,
    low: float = 1760.0,
    vol_ratio: float = 1.5,
) -> str:
    """构造腾讯行情原始行（含 v_XXXXXX=" 前缀），共 45 个 ~ 分隔字段"""
    fields = ["-"] * 45
    fields[0]  = "1"
    fields[1]  = "贵州茅台"
    fields[2]  = symbol.replace(".SH", "").replace(".SZ", "")
    fields[3]  = str(price)
    fields[4]  = str(prev_close)
    fields[32] = str(pct)
    fields[33] = str(high)
    fields[34] = str(low)
    fields[38] = str(vol_ratio)
    raw = "~".join(fields)
    tc = ("sh" if symbol.endswith(".SH") else "sz") + symbol[:-3]
    return f'v_{tc}="{raw}"'


# ══════════════════════════════════════════════════════════════════════════════
# 测试类（兼容 pytest 类风格）
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


class TestTTLCache:
    def test_set_and_get(self):
        cache = _TTLCache()
        cache.set("k", 42, ttl=10)
        assert cache.get("k") == 42

    def test_miss_returns_none(self):
        cache = _TTLCache()
        assert cache.get("nonexistent") is None

    def test_ttl_expiry(self):
        cache = _TTLCache()
        cache.set("k", "value", ttl=0.05)
        assert cache.get("k") == "value"
        time.sleep(0.1)
        assert cache.get("k") is None, "过期后应返回 None"

    def test_delete(self):
        cache = _TTLCache()
        cache.set("k", 1, ttl=60)
        cache.delete("k")
        assert cache.get("k") is None

    def test_clear(self):
        cache = _TTLCache()
        cache.set("a", 1, ttl=60)
        cache.set("b", 2, ttl=60)
        cache.clear()
        assert len(cache) == 0

    def test_overwrite(self):
        cache = _TTLCache()
        cache.set("k", "old", ttl=60)
        cache.set("k", "new", ttl=60)
        assert cache.get("k") == "new"

    def test_thread_safety(self):
        """多线程并发读写不崩溃"""
        cache = _TTLCache()
        errors = []

        def worker(i):
            try:
                for j in range(200):
                    cache.set(f"k{i}_{j}", j, ttl=1)
                    cache.get(f"k{i}_{j}")
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, f"线程安全异常: {errors}"


class TestSymbolToTencent:
    def test_sh(self):
        assert _symbol_to_tencent("600519.SH") == "sh600519"

    def test_sz(self):
        assert _symbol_to_tencent("000001.SZ") == "sz000001"

    def test_lowercase_suffix(self):
        assert _symbol_to_tencent("510310.sh") == "sh510310"

    def test_etf_sh(self):
        assert _symbol_to_tencent("510310.SH") == "sh510310"


class TestParseTencentQuote:
    def test_valid_line(self):
        raw = _make_tencent_raw_line("600519.SH", price=1800.0, prev_close=1780.0,
                                     pct=1.12, high=1820.0, low=1760.0, vol_ratio=1.5)
        q = _parse_tencent_quote("600519.SH", raw)
        assert q is not None
        assert q.symbol == "600519.SH"
        assert abs(q.price - 1800.0) < 0.01
        assert abs(q.prev_close - 1780.0) < 0.01
        assert abs(q.pct_change - 1.12) < 0.01
        assert abs(q.high - 1820.0) < 0.01
        assert abs(q.low - 1760.0) < 0.01
        assert q.vol_ratio is not None
        assert abs(q.vol_ratio - 1.5) < 0.01

    def test_too_few_fields_returns_none(self):
        q = _parse_tencent_quote("000001.SZ", 'v_sz000001="a~b~c"')
        assert q is None

    def test_returns_quote_on_zero_price(self):
        """价格为 0 是合法解析结果（业务层过滤），不应崩溃"""
        raw = _make_tencent_raw_line("600519.SH", price=0.0, prev_close=0.0,
                                     pct=0.0, high=0.0, low=0.0, vol_ratio=0.0)
        q = _parse_tencent_quote("600519.SH", raw)
        assert q is not None


class TestDataLayerCache:
    """DataLayer 缓存行为测试（全部 mock 网络，不发真实请求）"""

    def _make_quote(self, symbol: str = "510310.SH") -> Quote:
        return Quote(symbol, price=5.0, prev_close=4.9,
                     pct_change=2.04, high=5.1, low=4.8, vol_ratio=1.2)

    def test_get_realtime_bulk_caches_result(self):
        """相同标的第二次调用命中缓存，不触发 HTTP"""
        layer = DataLayer()
        sym = "510310.SH"
        fake = {sym: self._make_quote(sym)}
        call_count = [0]

        def fake_fetch(syms):
            call_count[0] += 1
            return fake

        with patch("core.data_layer._fetch_realtime_bulk_raw", side_effect=fake_fetch):
            layer.get_realtime_bulk([sym])
            layer.get_realtime_bulk([sym])

        assert call_count[0] == 1, f"应只请求1次，实际{call_count[0]}次"

    def test_get_realtime_bulk_partial_cache(self):
        """A已缓存，B未缓存，只请求B"""
        layer = DataLayer()
        q_a = self._make_quote("510310.SH")
        q_b = self._make_quote("600900.SH")

        with patch("core.data_layer._fetch_realtime_bulk_raw",
                   return_value={"510310.SH": q_a}):
            layer.get_realtime_bulk(["510310.SH"])

        request_args = []
        def capture(syms):
            request_args.append(list(syms))
            return {"600900.SH": q_b}

        with patch("core.data_layer._fetch_realtime_bulk_raw", side_effect=capture):
            result = layer.get_realtime_bulk(["510310.SH", "600900.SH"])

        assert request_args == [["600900.SH"]], "只应请求未缓存的 B"
        assert "510310.SH" in result
        assert "600900.SH" in result

    def test_get_bars_caches_result(self):
        """get_bars 第二次调用不触发 HTTP"""
        layer = DataLayer()
        df = _make_bar_df(60)
        call_count = [0]

        def fake_fetch(sym, days):
            call_count[0] += 1
            return df

        with patch("core.data_layer._fetch_daily_bars_tencent", side_effect=fake_fetch):
            layer.get_bars("510310.SH", days=60)
            layer.get_bars("510310.SH", days=60)

        assert call_count[0] == 1

    def test_get_bars_fallback_to_sina(self):
        """腾讯失败时降级到新浪"""
        layer = DataLayer()
        df = _make_bar_df(30)
        sina_called = [False]

        def fake_sina(sym, days):
            sina_called[0] = True
            return df

        with patch("core.data_layer._fetch_daily_bars_tencent", return_value=None), \
             patch("core.data_layer._fetch_daily_bars_sina", side_effect=fake_sina):
            result = layer.get_bars("600900.SH", days=30)

        assert sina_called[0], "腾讯失败后应调用新浪"
        assert not result.empty

    def test_get_bars_both_fail_returns_empty_df(self):
        """两源都失败时返回空 DataFrame（不抛异常）"""
        layer = DataLayer()
        with patch("core.data_layer._fetch_daily_bars_tencent", return_value=None), \
             patch("core.data_layer._fetch_daily_bars_sina", return_value=None):
            result = layer.get_bars("UNKNOWN.SH", days=30)
        assert isinstance(result, pd.DataFrame)
        assert result.empty

    def test_get_bars_has_required_columns(self):
        """返回 DataFrame 必须含标准列"""
        layer = DataLayer()
        df = _make_bar_df(10)
        with patch("core.data_layer._fetch_daily_bars_tencent", return_value=df):
            result = layer.get_bars("510310.SH", days=10)
        required = {"date", "open", "high", "low", "close", "volume"}
        assert required.issubset(set(result.columns))

    def test_invalidate_symbol_clears_quote_cache(self):
        """invalidate(sym) 后再次请求触发 HTTP"""
        layer = DataLayer()
        sym = "510310.SH"
        q = self._make_quote(sym)
        call_count = [0]

        def fake_fetch(syms):
            call_count[0] += 1
            return {sym: q}

        with patch("core.data_layer._fetch_realtime_bulk_raw", side_effect=fake_fetch):
            layer.get_realtime_bulk([sym])
            layer.invalidate(sym)
            layer.get_realtime_bulk([sym])

        assert call_count[0] == 2

    def test_invalidate_all_clears_cache(self):
        """invalidate() 清全部缓存"""
        layer = DataLayer()
        sym = "510310.SH"
        q = self._make_quote(sym)
        df = _make_bar_df(10)

        with patch("core.data_layer._fetch_realtime_bulk_raw", return_value={sym: q}), \
             patch("core.data_layer._fetch_daily_bars_tencent", return_value=df):
            layer.get_realtime_bulk([sym])
            layer.get_bars(sym, days=10)

        assert len(layer._cache) > 0
        layer.invalidate()
        assert len(layer._cache) == 0

    def test_get_realtime_delegates_to_bulk(self):
        """get_realtime(sym) 等同 get_realtime_bulk([sym])[sym]"""
        layer = DataLayer()
        q = self._make_quote("600519.SH")
        with patch("core.data_layer._fetch_realtime_bulk_raw", return_value={"600519.SH": q}):
            result = layer.get_realtime("600519.SH")
        assert result is not None
        assert result.symbol == "600519.SH"

    def test_get_realtime_returns_none_on_miss(self):
        """找不到标的时返回 None 不崩溃"""
        layer = DataLayer()
        with patch("core.data_layer._fetch_realtime_bulk_raw", return_value={}):
            result = layer.get_realtime("NOTEXIST.SH")
        assert result is None


class TestBacktestDataLayer:
    """BacktestDataLayer — 核心验收：前视偏差防护"""

    def _make_layer(self, n: int = 60) -> BacktestDataLayer:
        df = _make_bar_df(n, start="2024-01-02")
        return BacktestDataLayer({"510310.SH": df})

    def test_get_bars_no_date_returns_last_n(self):
        """未设置日期，返回全量最后 N 条"""
        layer = self._make_layer(60)
        result = layer.get_bars("510310.SH", days=30)
        assert len(result) == 30

    def test_no_lookahead_bias(self):
        """最核心验收：当前日期之后的数据不出现在 get_bars 结果中"""
        layer = self._make_layer(60)
        all_dates = layer.available_dates("510310.SH")
        cutoff = all_dates[20]

        layer.set_date(cutoff)
        bars = layer.get_bars("510310.SH", days=60)

        future = bars[bars["date"] > cutoff]
        assert len(future) == 0, f"存在前视偏差！泄露了 {len(future)} 条未来数据"

    def test_get_bars_respects_cutoff_date(self):
        """cutoff 当天的数据应包含，之后的不包含"""
        layer = self._make_layer(60)
        all_dates = layer.available_dates("510310.SH")
        cutoff = all_dates[30]

        layer.set_date(cutoff)
        bars = layer.get_bars("510310.SH", days=60)
        assert bars["date"].max() <= cutoff
        # cutoff 当天本身也应在结果中
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
        """get_realtime 返回 current_date 当日收盘价"""
        layer = self._make_layer(60)
        all_dates = layer.available_dates("510310.SH")
        cutoff = all_dates[20]
        layer.set_date(cutoff)

        q = layer.get_realtime("510310.SH")
        assert q is not None
        # 对比原始数据
        df = _make_bar_df(60, start="2024-01-02")
        expected = float(df[df["date"] == cutoff]["close"].iloc[0])
        assert abs(q.price - expected) < 0.001

    def test_get_realtime_returns_none_before_data(self):
        """设置比所有数据都早的日期时返回 None"""
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
        """回测中北向固定中性"""
        layer = self._make_layer(10)
        snap = layer.get_north_flow()
        assert snap.direction == "NEUTRAL"
        assert snap.stale is True

    def test_available_dates_sorted_ascending(self):
        layer = self._make_layer(20)
        dates = layer.available_dates("510310.SH")
        assert dates == sorted(dates), "available_dates 应升序"
        assert len(dates) == 20

    def test_set_date_from_string(self):
        layer = self._make_layer(30)
        layer.set_date("2024-02-01")
        assert layer.current_date == pd.Timestamp("2024-02-01")

    def test_string_date_column_normalized(self):
        """传入字符串 date 列时应自动转为 datetime64"""
        df = _make_bar_df(20, start="2024-01-02")
        df["date"] = df["date"].astype(str)
        layer = BacktestDataLayer({"510310.SH": df})
        bars = layer.get_bars("510310.SH", days=10)
        assert pd.api.types.is_datetime64_any_dtype(bars["date"])

    def test_multiple_symbols_independent_cutoffs(self):
        """多标的各自独立，不相互影响"""
        df1 = _make_bar_df(60, start="2024-01-02")
        df2 = _make_bar_df(60, start="2024-01-02")
        layer = BacktestDataLayer({"A.SH": df1, "B.SH": df2})
        dates_a = layer.available_dates("A.SH")
        cutoff = dates_a[20]
        layer.set_date(cutoff)

        bars_a = layer.get_bars("A.SH", days=60)
        bars_b = layer.get_bars("B.SH", days=60)
        # 两者都受同一 cutoff 约束
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
        assert a is b, "全局单例应返回相同实例"

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
        TestTTLCache,
        TestSymbolToTencent,
        TestParseTencentQuote,
        TestDataLayerCache,
        TestBacktestDataLayer,
        TestGlobalSingleton,
    ]
    for cls in test_classes:
        _run_class(cls)

    print(f"\n{'='*60}")
    print(f"Phase 1 DataLayer: {_passed} passed, {_failed} failed")
    if _errors:
        print("Failed:")
        for e in _errors:
            print(f"  - {e}")
    return _failed


if __name__ == "__main__":
    fails = run_all()
    sys.exit(1 if fails else 0)
