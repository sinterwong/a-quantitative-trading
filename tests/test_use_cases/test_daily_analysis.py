"""
tests/test_use_cases/test_daily_analysis.py — daily_analysis use case 单元测试。

覆盖:
  - happy: DynamicStockSelector mock 返回数据 → 完整 DailyAnalysisResponse
  - degraded: portfolio_svc=None → 不写 daily_meta,不写 signals,response 仍生成
  - error: 持久化失败不抛 (best-effort)
  - 持久化文件命名 + 内容包含 timestamp
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def fake_selector():
    """构造一个 DynamicStockSelector 的轻量 mock。"""
    selector = MagicMock()
    selector.get_top_bk_sectors.return_value = [
        ('BK001', {'name': '芯片', 'total': 88.5, 'change_pct': 3.21}),
        ('BK002', {'name': '电力', 'total': 75.0, 'change_pct': 1.50}),
    ]
    selector.get_news_summary.return_value = [{'title': 'news A'}]
    selector.get_stock_with_context.return_value = [{'symbol': '000001.SZ'}]
    selector._last_news_source = 'tencent'
    selector._last_source = 'eastmoney'
    return selector


@pytest.fixture
def patch_selector(fake_selector):
    """把 scripts.dynamic_selector.DynamicStockSelector 替换为 fake。"""
    # daily_analysis 在函数体内做 sys.path 操作 + 局部 import
    # 用一个 fake module 注入 sys.modules 中,绕过磁盘 import
    fake_module = MagicMock()
    fake_module.DynamicStockSelector = MagicMock(return_value=fake_selector)
    with patch.dict(sys.modules, {'dynamic_selector': fake_module}):
        yield fake_selector


def test_run_daily_analysis_happy_path(patch_selector, tmp_path):
    from core.use_cases.daily_analysis import DailyAnalysisRequest, run_daily_analysis

    svc = MagicMock()
    svc.get_portfolio_summary.return_value = {'total_equity': 50000, 'cash': 10000}
    svc.get_trades.return_value = [{'timestamp': '2026-05-15 09:30:00'}]

    req = DailyAnalysisRequest(output_dir=str(tmp_path))
    resp = run_daily_analysis(req, portfolio_svc=svc)

    # 响应结构
    assert resp.sources == {'news': 'tencent', 'sectors': 'eastmoney'}
    assert len(resp.top_sectors) == 2
    assert resp.top_sectors[0]['bk'] == 'BK001'
    assert resp.top_sectors[0]['name'] == '芯片'
    assert resp.news_summary == [{'title': 'news A'}]

    # 信号已记录
    assert svc.record_signal.call_count == 2
    # daily_meta 已写
    svc.record_daily_meta.assert_called_once()

    # 文件已持久化
    files = list(tmp_path.glob('analysis_*.json'))
    assert len(files) == 1
    data = json.loads(files[0].read_text())
    assert 'timestamp' in data
    assert data['sources']['news'] == 'tencent'


def test_run_daily_analysis_without_portfolio_svc(patch_selector, tmp_path):
    """portfolio_svc=None → 不调 record_signal / record_daily_meta,但仍返回 response。"""
    from core.use_cases.daily_analysis import DailyAnalysisRequest, run_daily_analysis

    req = DailyAnalysisRequest(output_dir=str(tmp_path))
    resp = run_daily_analysis(req, portfolio_svc=None)

    assert len(resp.top_sectors) == 2
    files = list(tmp_path.glob('analysis_*.json'))
    assert len(files) == 1


def test_run_daily_analysis_persist_failure_is_swallowed(patch_selector):
    """output_dir 指向不可写路径 → 仍返回 response,不抛异常。"""
    from core.use_cases.daily_analysis import DailyAnalysisRequest, run_daily_analysis

    # /proc 下不能创建文件
    req = DailyAnalysisRequest(output_dir='/proc/cannot_write_here')
    resp = run_daily_analysis(req, portfolio_svc=None)
    assert len(resp.top_sectors) == 2  # 主流程不受影响


def test_run_daily_analysis_daily_meta_failure_is_swallowed(patch_selector, tmp_path):
    """portfolio_svc.record_daily_meta 抛异常 → 主流程不受影响。"""
    from core.use_cases.daily_analysis import DailyAnalysisRequest, run_daily_analysis

    svc = MagicMock()
    svc.get_portfolio_summary.return_value = {'total_equity': 0, 'cash': 0}
    svc.get_trades.return_value = []
    svc.record_daily_meta.side_effect = RuntimeError('db locked')

    req = DailyAnalysisRequest(output_dir=str(tmp_path))
    resp = run_daily_analysis(req, portfolio_svc=svc)
    assert len(resp.top_sectors) == 2


def test_daily_analysis_response_to_dict_round_trip(patch_selector, tmp_path):
    from core.use_cases.daily_analysis import DailyAnalysisRequest, run_daily_analysis

    req = DailyAnalysisRequest(output_dir=str(tmp_path))
    resp = run_daily_analysis(req, portfolio_svc=None)
    d = resp.to_dict()
    assert set(d.keys()) == {'sources', 'top_sectors', 'news_summary', 'selected_stocks'}
