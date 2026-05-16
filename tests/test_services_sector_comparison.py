"""
tests/test_services_sector_comparison.py — backend/services/sector_comparison.py
单元测试 (P1-2)

覆盖:
  - SectorComparisonResult.to_dict 结构
  - _compute_percentile 边界
  - compare_sector 未知行业 → ValueError
  - compare_symbols 空列表 → ValueError
  - compare_symbols 网络失败 → 返回带 warning 的空 result(不抛)
  - 完整对比 (mock quotes)
"""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

_BACKEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'backend',
)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from services.sector_comparison import (
    SectorComparisonResult, SECTOR_STOCKS,
    compare_sector, compare_symbols, _compute_percentile,
)


def _fake_quote(sym, name, price=100.0, pct=2.5, pe=15.0, pb=2.5):
    return {
        sym: {
            'name': name, 'price': price, 'pct_change': pct,
            'pe': pe, 'pb': pb, 'volume_ratio': 1.0,
        }
    }


# ── _compute_percentile ──────────────────────────────────

def test_percentile_empty_returns_none():
    assert _compute_percentile(10.0, []) is None


def test_percentile_value_zero_returns_none():
    """value=0(PE/PB 无效)→ None。"""
    assert _compute_percentile(0.0, [10, 20, 30]) is None


def test_percentile_all_zero_values_returns_none():
    assert _compute_percentile(10.0, [0, 0, 0]) is None


def test_percentile_basic():
    """20 在 [10,20,30,40] 中,<=20 的有 2 个 / 4 个 → 50%。"""
    assert _compute_percentile(20.0, [10, 20, 30, 40]) == 50.0


def test_percentile_smallest_value():
    """value 是最小的非零 → 25% (1/4)。"""
    assert _compute_percentile(10.0, [10, 20, 30, 40]) == 25.0


# ── compare_sector ───────────────────────────────────────

def test_compare_sector_unknown_raises():
    with pytest.raises(ValueError, match='未知行业'):
        compare_sector('火星矿业XXXYZ')


def test_compare_sector_known_sector_exists():
    """白酒是预定义行业。SECTOR_STOCKS 中应存在。"""
    assert '白酒' in SECTOR_STOCKS
    assert len(SECTOR_STOCKS['白酒']) >= 1


# ── compare_symbols ──────────────────────────────────────

def test_compare_symbols_empty_raises():
    with pytest.raises(ValueError, match='symbols 不能为空'):
        compare_symbols([])


def test_compare_symbols_dedupes_input():
    """重复 symbol 应去重。"""
    with patch('services.sector_comparison._fetch_batch_tencent', return_value={}):
        result = compare_symbols(['600519.SH', '600519.SH', '000858.SZ'])
    assert result.stock_count == 2


def test_compare_symbols_quotes_failure_returns_warning():
    """quotes 拉取失败 → 返回 result 带 warning,不抛。"""
    with patch('services.sector_comparison._fetch_batch_tencent', return_value={}):
        result = compare_symbols(['600519.SH', '000858.SZ'], sector_name='白酒')
    assert result.sector_name == '白酒'
    assert result.stocks == []
    assert any('行情' in w or '网络' in w for w in result.warnings)


def test_compare_symbols_full_round_trip():
    """完整路径:quotes 返回 → 算 avg_pe/pb,标 is_base。"""
    quotes = {
        'sh603369': {'name': '今世缘', 'price': 28.13, 'pct_change': 2.11,
                     'pe': 14.96, 'pb': 3.47, 'volume_ratio': 1.0,
                     'market_cap': 350.0},
        'sz000858': {'name': '五粮液', 'price': 130.0, 'pct_change': 1.2,
                     'pe': 22.0, 'pb': 5.0, 'volume_ratio': 1.0,
                     'market_cap': 5000.0},
    }
    with patch('services.sector_comparison._fetch_batch_tencent', return_value=quotes):
        result = compare_symbols(
            ['603369.SH', '000858.SZ'],
            sector_name='白酒',
            base_symbol='603369.SH',
        )
    assert result.stock_count == 2
    assert len(result.stocks) == 2
    assert result.avg_pe > 0
    assert result.avg_pb > 0
    base = next(s for s in result.stocks if s.get('is_base'))
    # symbol 字段实际为 '603369.SH',不是 'sh603369'
    assert '603369' in base['symbol']


def test_sector_comparison_result_to_dict():
    r = SectorComparisonResult(
        sector_name='白酒', stock_count=3,
        avg_pe=20.0, avg_pb=4.0,
    )
    d = r.to_dict()
    assert set(d.keys()) == {
        'sector_name', 'stock_count', 'avg_pe', 'avg_pb', 'stocks', 'warnings',
    }
    assert d['stocks'] == []
    assert d['warnings'] == []
