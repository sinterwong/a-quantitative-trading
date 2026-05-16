"""
RiskMixin 单元测试 — Kelly 仓位裁剪 / 新闻情绪过滤 / 行业集中度。
"""

from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from backend.services.intraday.risk import (
    RiskMixin, MAX_POSITION_PCT,
)


# ── _calc_shares ────────────────────────────────────────

def test_calc_shares_returns_zero_with_no_cash(monitor):
    monitor._svc.get_cash.return_value = 0
    assert RiskMixin._calc_shares(monitor, '600519.SH', 100.0) == 0


def test_calc_shares_returns_zero_with_zero_price(monitor):
    monitor._svc.get_cash.return_value = 10000
    assert RiskMixin._calc_shares(monitor, '600519.SH', 0) == 0


def test_calc_shares_kelly_constraint_dominates(monitor):
    """cash×kelly 较小 → 仓位由 Kelly 决定。"""
    monitor._svc.get_cash.return_value = 100_000
    monitor._svc.get_total_equity.return_value = 1_000_000
    monitor._svc.get_position.return_value = None
    monitor._kelly_pct = 0.10        # kelly_cost = 10_000
    # max_pos_cost = 1e6 * 0.25 = 250_000 (远大于 10_000)
    n = RiskMixin._calc_shares(monitor, '600519.SH', 10.0)
    # budget = min(10000, 250000) = 10000, raw_shares = 1000, 整手 1000
    assert n == 1000


def test_calc_shares_position_pct_constraint_dominates(monitor):
    """已持仓接近上限 → max_pos_cost 决定加仓量。"""
    monitor._svc.get_cash.return_value = 1_000_000  # 现金充足
    monitor._svc.get_total_equity.return_value = 100_000  # 总权益小
    monitor._svc.get_position.return_value = {'shares': 2400}  # 已持仓 2400*10 = 24000
    monitor._kelly_pct = 1.0         # kelly_cost 不约束
    # max_pos_value = 100000 * 0.25 = 25000
    # existing_value = 2400 * 10 = 24000
    # max_pos_cost = 1000,raw_shares = 100,整手 100
    n = RiskMixin._calc_shares(monitor, 'X', 10.0)
    assert n == 100


def test_calc_shares_returns_zero_below_lot(monitor):
    """budget 不足以买 100 股 → 返回 0。"""
    monitor._svc.get_cash.return_value = 5000
    monitor._svc.get_total_equity.return_value = 5000
    monitor._svc.get_position.return_value = None
    monitor._kelly_pct = 0.10  # kelly_cost=500,价 10 元 → 50 股 < 100
    assert RiskMixin._calc_shares(monitor, 'X', 10.0) == 0


def test_calc_shares_handles_svc_exception(monitor):
    """svc.get_cash 抛异常 → 返回 0,不传播。"""
    monitor._svc.get_cash.side_effect = RuntimeError('db locked')
    assert RiskMixin._calc_shares(monitor, 'X', 10.0) == 0


# ── _check_news_sentiment ────────────────────────────────

def test_news_sentiment_no_llm_returns_no_block(monitor):
    """无 LLM 服务 → 不阻止,返回 (False, None, None, None)。"""
    monitor._llm = None
    blocked, sent, conf, summ = RiskMixin._check_news_sentiment(monitor, 'X')
    assert blocked is False
    assert sent is None


def _attach_news_attrs(monitor):
    """补充类属性 — Mixin 通过 self.* 访问。"""
    monitor.BEARISH_BLOCK_CONFIDENCE = RiskMixin.BEARISH_BLOCK_CONFIDENCE


def test_news_sentiment_bearish_with_high_confidence_blocks(monitor):
    """LLM 返回 bearish 且 conf >= 0.6 → 阻止建仓。"""
    _attach_news_attrs(monitor)
    monitor._llm = MagicMock()
    result_mock = MagicMock()
    result_mock.sentiment = 'bearish'
    result_mock.confidence = 0.85
    result_mock.summary = '业绩暴雷'
    monitor._llm.analyze_news.return_value = result_mock
    monitor._get_params = MagicMock(return_value={'name': '茅台'})

    blocked, sent, conf, summ = RiskMixin._check_news_sentiment(monitor, '600519.SH')
    assert blocked is True
    assert sent == 'bearish'
    assert conf == 0.85


def test_news_sentiment_neutral_does_not_block(monitor):
    """中性情绪 → 不阻止。"""
    _attach_news_attrs(monitor)
    monitor._llm = MagicMock()
    result_mock = MagicMock()
    result_mock.sentiment = 'neutral'
    result_mock.confidence = 0.9
    result_mock.summary = ''
    monitor._llm.analyze_news.return_value = result_mock
    monitor._get_params = MagicMock(return_value={'name': 'X'})

    blocked, _, _, _ = RiskMixin._check_news_sentiment(monitor, 'X')
    assert blocked is False


def test_news_sentiment_bearish_low_confidence_does_not_block(monitor):
    """bearish 但置信度 < 0.6 → 不阻止。"""
    _attach_news_attrs(monitor)
    monitor._llm = MagicMock()
    result_mock = MagicMock()
    result_mock.sentiment = 'bearish'
    result_mock.confidence = 0.3
    result_mock.summary = ''
    monitor._llm.analyze_news.return_value = result_mock
    monitor._get_params = MagicMock(return_value={'name': 'X'})

    blocked, _, _, _ = RiskMixin._check_news_sentiment(monitor, 'X')
    assert blocked is False


def test_news_sentiment_cache_hit_skips_llm_call(monitor):
    """同一天再次查询 → 不重复调 LLM。"""
    _attach_news_attrs(monitor)
    monitor._llm = MagicMock()
    monitor._sentiment_cache = {'X': ('bullish', 0.7, '利好')}
    monitor._sentiment_cache_date = date.today().isoformat()

    blocked, sent, conf, summ = RiskMixin._check_news_sentiment(monitor, 'X')
    assert blocked is False
    assert sent == 'bullish'
    monitor._llm.analyze_news.assert_not_called()


def test_news_sentiment_llm_error_returns_unknown(monitor):
    """LLM 抛异常 → 返回 ('unknown', 0, '') 且不阻止。"""
    _attach_news_attrs(monitor)
    monitor._llm = MagicMock()
    monitor._llm.analyze_news.side_effect = TimeoutError('LLM down')
    monitor._get_params = MagicMock(return_value={'name': 'X'})

    blocked, sent, _, _ = RiskMixin._check_news_sentiment(monitor, 'X')
    assert blocked is False
    assert sent == 'unknown'


# ── _check_sector_concentration ──────────────────────────

def test_check_sector_concentration_no_violation_skips_alert(monitor):
    monitor.BEARISH_BLOCK_CONFIDENCE = 0.60
    with patch('services.portfolio.check_sector_concentration', return_value=[]):
        positions = [{'symbol': 'X', 'shares': 100, 'sector': 'TECH'}]
        RiskMixin._check_sector_concentration(monitor, positions)
        monitor._deliver_alert.assert_not_called()


def test_check_sector_concentration_violation_triggers_alert(monitor):
    monitor._broker = None  # 不做减仓
    violation = [{
        'sector': 'TECH', 'pct': 45.0,
        'reduce_value': 5000.0, 'reduce_pct': 5,
    }]
    with patch('services.portfolio.check_sector_concentration', return_value=violation):
        positions = [{'symbol': 'X', 'shares': 100, 'sector': 'TECH'}]
        RiskMixin._check_sector_concentration(monitor, positions)
        monitor._deliver_alert.assert_called_once()
