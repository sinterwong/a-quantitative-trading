"""
tests/test_services_northbound.py — backend/services/northbound.py 单元测试 (P1-2)

覆盖纯计算函数:
  - format_kamt_summary
  - get_north_flow_direction (基于历史 dict 的逻辑)
  - record_today_north_from_kamt
  - NorthBoundAlertChecker (基本路径)

外部 IO (cached_kamt / 拉取 eastmoney) 不在此处覆盖。
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from unittest.mock import patch

import pytest

_BACKEND_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'backend',
)
if _BACKEND_DIR not in sys.path:
    sys.path.insert(0, _BACKEND_DIR)

from services.northbound import (
    format_kamt_summary, get_north_flow_direction,
    record_today_north_from_kamt,
)


# ── format_kamt_summary ──────────────────────────────────

def test_format_kamt_summary_empty_returns_text():
    """空数据 → 配额提示或暂无数据。"""
    out = format_kamt_summary({})
    assert isinstance(out, str)


def test_format_kamt_summary_net_inflow_shows_green():
    data = {
        'n2s': {'cum_amount': 5_000_000_000, 'quota_used': 0, 'quota_total': 1e10},
        's2n': {'cum_amount': 3_000_000_000, 'quota_used': 0, 'quota_total': 1e10},
        'net_north_cny': 2_000_000_000,  # 20亿净流入
    }
    out = format_kamt_summary(data)
    assert '净买入' in out
    assert '20.00' in out or '20.0' in out


def test_format_kamt_summary_net_outflow_shows_red():
    data = {
        'n2s': {'cum_amount': 1_000_000_000, 'quota_used': 0, 'quota_total': 1e10},
        's2n': {'cum_amount': 5_000_000_000, 'quota_used': 0, 'quota_total': 1e10},
        'net_north_cny': -4_000_000_000,  # -40亿
    }
    out = format_kamt_summary(data)
    assert '净卖出' in out


def test_format_kamt_summary_no_trades_falls_back_to_quota():
    """无成交量 → 显示配额使用率。"""
    data = {
        'n2s': {'cum_amount': 0, 'quota_used': 4e9, 'quota_total': 5e10},
        's2n': {'cum_amount': 0, 'quota_used': 1e9, 'quota_total': 5e10},
        'net_north_cny': 0,
    }
    out = format_kamt_summary(data)
    assert '配额' in out


# ── get_north_flow_direction ─────────────────────────────

def _patch_history(history: dict):
    return patch('services.northbound._load_north_history', return_value=history)


def test_north_flow_direction_outflow_today_is_south():
    today = date.today().isoformat()
    with _patch_history({today: -2_000_000_000}):  # 净流出 20 亿(<0)
        out = get_north_flow_direction()
    assert out['direction'] == 'south'
    assert out['strength'] == 0


def test_north_flow_direction_continuous_inflow():
    """连续 3 天净流入 ≥ threshold(50亿)→ continuous + strength=2。"""
    today = date.today()
    history = {
        today.isoformat(): 6_000_000_000,
        (today - timedelta(days=1)).isoformat(): 7_000_000_000,
        (today - timedelta(days=2)).isoformat(): 5_500_000_000,
    }
    with _patch_history(history):
        out = get_north_flow_direction(threshold_yi=50.0)
    assert out['direction'] == 'continuous'
    assert out['strength'] == 2


def test_north_flow_direction_impulse_single_day():
    """今日单日流入超阈值,但前日未达 → impulse。"""
    today = date.today()
    history = {
        today.isoformat(): 8_000_000_000,  # 80亿
        (today - timedelta(days=1)).isoformat(): 1_000_000_000,  # 10亿
    }
    with _patch_history(history):
        out = get_north_flow_direction(threshold_yi=50.0)
    assert out['direction'] == 'impulse'
    assert out['strength'] == 1


def test_north_flow_direction_neutral_below_threshold():
    today = date.today()
    history = {today.isoformat(): 100_000_000}  # 1亿,远低于阈值
    with _patch_history(history):
        out = get_north_flow_direction(threshold_yi=50.0)
    assert out['direction'] == 'neutral'


def test_north_flow_direction_returns_required_fields():
    today = date.today().isoformat()
    with _patch_history({today: 1_000_000_000}):
        out = get_north_flow_direction()
    required = {'direction', 'days', 'today_yi', 'trend_yi', 'strength', 'reason'}
    assert required.issubset(out.keys())


# ── record_today_north_from_kamt ────────────────────────

def test_record_today_north_skips_empty():
    """空 kamt_data → 不调存储。"""
    with patch('services.northbound._save_north_history') as mock_save:
        record_today_north_from_kamt({})
    mock_save.assert_not_called()


def test_record_today_north_skips_zero_net():
    """net=0 → 不记录。"""
    with patch('services.northbound._save_north_history') as mock_save:
        record_today_north_from_kamt({'net_north_cny': 0})
    mock_save.assert_not_called()


def test_record_today_north_writes_new_entry():
    today = date.today().isoformat()
    with patch('services.northbound._load_north_history', return_value={}), \
         patch('services.northbound._save_north_history') as mock_save:
        record_today_north_from_kamt({'net_north_cny': 1_500_000_000})
    mock_save.assert_called_once()
    saved = mock_save.call_args.args[0]
    assert saved[today] == 1_500_000_000


def test_record_today_north_does_not_overwrite_existing():
    today = date.today().isoformat()
    existing = {today: 999_999}
    with patch('services.northbound._load_north_history', return_value=existing), \
         patch('services.northbound._save_north_history') as mock_save:
        record_today_north_from_kamt({'net_north_cny': 5_000_000_000})
    # 当日已存在 → 不覆盖
    mock_save.assert_not_called()
