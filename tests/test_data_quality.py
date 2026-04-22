"""
tests/test_data_quality.py — Phase 1-C 数据质量检验 + 数据层加固验收测试

覆盖：
  1. DataQualityChecker.check_and_mark：零成交量/异常涨跌/跳空标记
  2. DataQualityReport：质量评分、is_clean、summary
  3. DataQualityChecker.drop_anomalies：剔除异常 bar
  4. check_data_quality 便捷函数
  5. ParquetCache：save/load/upsert/exists/latest_date/delete
  6. DataLayer.get_minute_bars：接口存在且返回正确格式（mock AKShare）
"""

import sys
import os
import tempfile
import traceback
from datetime import datetime, date, timedelta
from unittest.mock import patch, MagicMock

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.data_quality import (
    DataQualityChecker,
    DataQualityReport,
    AnomalyRecord,
    check_data_quality,
)
from core.data_layer import ParquetCache, DataLayer

# ─── 测试框架 ──────────────────────────────────────────────────────────────────

_passed = 0
_failed = 0
_errors = []


def check(cond: bool, msg: str):
    global _passed, _failed
    if cond:
        _passed += 1
        print(f"  PASS: {msg}")
    else:
        _failed += 1
        _errors.append(msg)
        print(f"  FAIL: {msg}")


def section(name: str):
    print(f"\n=== {name} ===")


# ─── 辅助：合成数据 ────────────────────────────────────────────────────────────

def make_clean_df(n: int = 30) -> pd.DataFrame:
    """干净的日线 DataFrame（DatetimeIndex，无异常）"""
    idx = pd.date_range('2023-01-03', periods=n, freq='B')
    price = 10.0 + np.arange(n) * 0.1
    return pd.DataFrame({
        'open':   price * 0.99,
        'high':   price * 1.01,
        'low':    price * 0.98,
        'close':  price,
        'volume': np.ones(n) * 1_000_000,
    }, index=idx)


def inject_zero_volume(df: pd.DataFrame, idx: int) -> pd.DataFrame:
    out = df.copy()
    out.iloc[idx, out.columns.get_loc('volume')] = 0
    return out


def inject_abnormal_move(df: pd.DataFrame, idx: int, pct: float = 0.25) -> pd.DataFrame:
    """将第 idx 根 bar 的 close 调整为前一根的 (1 + pct) 倍"""
    out = df.copy()
    if idx > 0:
        prev_close = out.iloc[idx - 1]['close']
        new_close = prev_close * (1 + pct)
        out.iloc[idx, out.columns.get_loc('close')] = new_close
        out.iloc[idx, out.columns.get_loc('high')] = new_close * 1.005
    return out


def inject_gap(df: pd.DataFrame, cut_idx: int, gap_days: int = 15) -> pd.DataFrame:
    """在 cut_idx 处人为插入跳空：保留前半段，后半段日期整体偏移 gap_days"""
    first_half = df.iloc[:cut_idx].copy()
    second_half = df.iloc[cut_idx:].copy()
    second_half.index = second_half.index + pd.Timedelta(days=gap_days)
    return pd.concat([first_half, second_half])


# ─── Section 1: 干净数据 ─────────────────────────────────────────────────────

section("DataQualityChecker — 干净数据")

df_clean = make_clean_df(20)
checker = DataQualityChecker(symbol='TEST', max_gap_days=7, abnormal_move_pct=20.0)
df_marked = checker.check_and_mark(df_clean)

check(checker.report is not None, "report 不为 None")
check(checker.report.is_clean, "干净数据 is_clean=True")
check(checker.report.n_zero_volume == 0, "零成交量=0")
check(checker.report.n_abnormal_moves == 0, "异常涨跌=0")
check(checker.report.n_gaps == 0, "跳空=0")
check(checker.report.quality_score == 100.0, f"质量评分=100，实际={checker.report.quality_score}")
check('quality_flag' in df_marked.columns, "包含 quality_flag 列")
check(all(df_marked['quality_flag'] == 'ok'), "全部 bar quality_flag='ok'")
check('is_zero_volume' in df_marked.columns, "包含 is_zero_volume 列")
check('is_abnormal_move' in df_marked.columns, "包含 is_abnormal_move 列")
check('is_gap' in df_marked.columns, "包含 is_gap 列")

# ─── Section 2: 零成交量检测 ─────────────────────────────────────────────────

section("DataQualityChecker — 零成交量检测")

df_zvol = inject_zero_volume(make_clean_df(20), 5)
checker2 = DataQualityChecker('TEST')
df_m2 = checker2.check_and_mark(df_zvol)

check(checker2.report.n_zero_volume == 1, f"应检测到 1 个零成交量，实际={checker2.report.n_zero_volume}")
check(df_m2['is_zero_volume'].sum() == 1, "is_zero_volume 列有 1 个 True")
check(df_m2['quality_flag'].iloc[5] == 'zero_volume', "第 5 根 bar 标记为 zero_volume")
check(not checker2.report.is_clean, "存在异常，is_clean=False")
check(checker2.report.quality_score < 100, f"质量评分 < 100，实际={checker2.report.quality_score:.1f}")

# ─── Section 3: 异常涨跌检测 ─────────────────────────────────────────────────

section("DataQualityChecker — 异常涨跌检测")

df_jump = inject_abnormal_move(make_clean_df(20), 10, pct=0.25)  # 25% 涨幅
checker3 = DataQualityChecker('TEST', abnormal_move_pct=20.0)
df_m3 = checker3.check_and_mark(df_jump)

check(checker3.report.n_abnormal_moves >= 1, f"应检测到 ≥1 异常涨跌，实际={checker3.report.n_abnormal_moves}")
check(df_m3['is_abnormal_move'].any(), "is_abnormal_move 列存在 True")
check(df_m3.loc[df_m3['is_abnormal_move'], 'quality_flag'].iloc[0] in ('abnormal_move', 'multi'),
      "异常涨跌 bar 标记正确")

# 验证低于阈值时不触发
df_small = inject_abnormal_move(make_clean_df(20), 10, pct=0.05)  # 5% 涨幅
checker3b = DataQualityChecker('TEST', abnormal_move_pct=20.0)
checker3b.check_and_mark(df_small)
check(checker3b.report.n_abnormal_moves == 0, "5% 涨幅不触发异常（阈值20%）")

# ─── Section 4: 跳空检测 ─────────────────────────────────────────────────────

section("DataQualityChecker — 跳空检测")

df_gap = inject_gap(make_clean_df(20), 10, gap_days=15)
checker4 = DataQualityChecker('TEST', max_gap_days=7)
df_m4 = checker4.check_and_mark(df_gap)

check(checker4.report.n_gaps >= 1, f"应检测到 ≥1 跳空，实际={checker4.report.n_gaps}")
check(df_m4['is_gap'].any(), "is_gap 列存在 True")

# 正常工作日间隔（3 天含周末），不应触发
# 正常 freq='B' 相邻 bar 间隔 1~3 天（周五→周一=3天）
df_normal = make_clean_df(30)
checker4b = DataQualityChecker('TEST', max_gap_days=7)
checker4b.check_and_mark(df_normal)
check(checker4b.report.n_gaps == 0, "正常工作日数据无跳空")

# ─── Section 5: drop_anomalies ────────────────────────────────────────────────

section("DataQualityChecker — drop_anomalies")

df_mixed = inject_zero_volume(make_clean_df(20), 5)
checker5 = DataQualityChecker('TEST')
df_m5 = checker5.check_and_mark(df_mixed)
df_clean5 = checker5.drop_anomalies(df_m5, drop_zero_volume=True)

check(len(df_clean5) == len(df_mixed) - 1, f"剔除 1 个零成交量后，行数={len(df_clean5)}")
check(df_clean5['is_zero_volume'].sum() == 0, "剔除后无零成交量")

# 不剔除时，行数不变
df_kept5 = checker5.drop_anomalies(df_m5, drop_zero_volume=False)
check(len(df_kept5) == len(df_mixed), "不剔除时行数不变")

# ─── Section 6: DataQualityReport ────────────────────────────────────────────

section("DataQualityReport")

# 构造报告
report = DataQualityReport(
    symbol='510300', total_bars=100,
    anomalies=[
        AnomalyRecord(date=pd.Timestamp('2023-01-05'), anomaly_type='zero_volume',
                      detail='停牌', value=0.0),
    ],
    n_gaps=0, n_abnormal_moves=0, n_zero_volume=1,
    completeness_pct=99.0,
)
check(not report.is_clean, "有异常时 is_clean=False")
check(0 < report.quality_score <= 100, f"质量评分在 (0,100]，实际={report.quality_score}")
summary = report.summary()
check('510300' in summary, "summary 含 symbol")
check('质量评分' in summary, "summary 含质量评分")

# 空数据报告
empty_report = DataQualityReport(symbol='X', total_bars=0)
check(empty_report.quality_score == 0.0, "空数据质量评分=0")

# ─── Section 7: check_data_quality 便捷函数 ──────────────────────────────────

section("check_data_quality 便捷函数")

df_test = inject_zero_volume(make_clean_df(25), 3)
report2 = check_data_quality(df_test, symbol='TEST')
check(isinstance(report2, DataQualityReport), "返回 DataQualityReport")
check(report2.n_zero_volume == 1, f"检测到 1 个零成交量，实际={report2.n_zero_volume}")
check(report2.symbol == 'TEST', "symbol 记录正确")

# ─── Section 8: ParquetCache ─────────────────────────────────────────────────

section("ParquetCache — 基本操作")

with tempfile.TemporaryDirectory() as tmpdir:
    cache = ParquetCache(cache_dir=tmpdir)

    # exists：不存在时返回 False
    check(not cache.exists('510300'), "初始状态不存在缓存")

    # load：不存在时返回 None
    check(cache.load('510300') is None, "load 不存在时返回 None")

    # 构造测试数据
    df_pc = make_clean_df(10)
    ok = cache.save('510300', df_pc)
    check(ok, "save 返回 True")
    check(cache.exists('510300'), "save 后 exists=True")

    # load
    df_loaded = cache.load('510300')
    check(df_loaded is not None, "load 返回非 None")
    check(len(df_loaded) == len(df_pc), f"行数相同: {len(df_loaded)}")
    check(pd.api.types.is_datetime64_any_dtype(df_loaded.index), "索引为 DatetimeIndex")

    # latest_date
    ld = cache.latest_date('510300')
    check(ld is not None, "latest_date 非 None")
    check(ld == df_pc.index[-1], f"latest_date 匹配最后一行 ({ld})")

    # upsert：追加新数据
    df_new = make_clean_df(5)
    df_new.index = pd.date_range(df_pc.index[-1] + pd.Timedelta(days=1), periods=5, freq='B')
    df_merged = cache.upsert('510300', df_new)
    check(len(df_merged) == len(df_pc) + len(df_new), f"合并后行数={len(df_merged)}")
    check(cache.latest_date('510300') == df_new.index[-1], "upsert 后 latest_date 更新")

    # upsert 幂等（重复插入不增加行数）
    df_again = cache.upsert('510300', df_new)
    check(len(df_again) == len(df_merged), "upsert 幂等，重复插入不增行")

    # delete
    deleted = cache.delete('510300')
    check(deleted, "delete 返回 True")
    check(not cache.exists('510300'), "delete 后 exists=False")
    check(cache.delete('nonexistent') is False, "删除不存在文件返回 False")

section("ParquetCache — 特殊字符标的")

with tempfile.TemporaryDirectory() as tmpdir:
    cache2 = ParquetCache(cache_dir=tmpdir)
    df_sh = make_clean_df(5)
    ok = cache2.save('510300.SH', df_sh)
    check(ok, "含点号标的可以保存")
    check(cache2.exists('510300.SH'), "含点号标的 exists=True")
    loaded = cache2.load('510300.SH')
    check(loaded is not None and len(loaded) == 5, "含点号标的可以加载")

# ─── Section 9: DataLayer.get_minute_bars（mock AKShare）────────────────────

section("DataLayer.get_minute_bars — 接口测试")

# 构造 mock AKShare 返回数据
mock_minute_df = pd.DataFrame({
    '时间': pd.date_range('2024-01-15 09:30', periods=10, freq='1min'),
    '开盘': [10.0 + i * 0.01 for i in range(10)],
    '收盘': [10.01 + i * 0.01 for i in range(10)],
    '最高': [10.02 + i * 0.01 for i in range(10)],
    '最低': [9.99 + i * 0.01 for i in range(10)],
    '成交量': [100_000] * 10,
})

with tempfile.TemporaryDirectory() as tmpdir:
    layer = DataLayer(use_parquet_cache=False)

    # AKShare 不可用时返回空 DataFrame
    with patch.dict('sys.modules', {'akshare': None}):
        df_min = layer.get_minute_bars('510300', period='1')
        check(isinstance(df_min, pd.DataFrame), "AKShare 不可用时返回 DataFrame")

    # AKShare 可用时返回正确格式
    mock_ak = MagicMock()
    mock_ak.stock_zh_a_minute.return_value = mock_minute_df

    with patch.dict('sys.modules', {'akshare': mock_ak}):
        df_min2 = layer.get_minute_bars('510300', period='1', adjust='qfq')
        # 因为 mock 替换了模块，实际调用路径在 _fetch_minute_bars_akshare 内部
        # 此处只验证接口存在且不崩溃
        check(isinstance(df_min2, pd.DataFrame), "get_minute_bars 返回 DataFrame")

# 缓存：连续两次调用，第二次走内存缓存
layer2 = DataLayer(use_parquet_cache=False)
with patch('core.data_layer._fetch_minute_bars_akshare', return_value=None):
    r1 = layer2.get_minute_bars('510300', period='5')
    r2 = layer2.get_minute_bars('510300', period='5')  # 应命中缓存
    check(isinstance(r2, pd.DataFrame), "缓存命中后返回 DataFrame")

# ─── Section 10: DataLayer — Parquet 集成 ────────────────────────────────────

section("DataLayer — use_parquet_cache 参数")

layer_no_cache = DataLayer(use_parquet_cache=False)
check(layer_no_cache._parquet is None, "use_parquet_cache=False 时 _parquet=None")

with tempfile.TemporaryDirectory() as tmpdir:
    with patch('core.data_layer._PARQUET_DIR', tmpdir):
        layer_with_cache = DataLayer(use_parquet_cache=True)
        check(layer_with_cache._parquet is not None, "use_parquet_cache=True 时 _parquet 非 None")

# ─── Summary ─────────────────────────────────────────────────────────────────

print(f"\n{'='*50}")
print(f"DataQuality+DataLayer Phase 1-C: {_passed} passed, {_failed} failed")
if _errors:
    for e in _errors:
        print(f"  ✗ {e}")
if _failed > 0:
    sys.exit(1)
