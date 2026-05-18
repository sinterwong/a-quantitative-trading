"""
AlertsMixin 单元测试 — 事件日志 / get_status / LLM 终极审核降级 / Feishu 推送跳过。
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

from backend.services.intraday.alerts import AlertsMixin


# ── 事件日志记录 ────────────────────────────────────────

def test_record_signal_appends_entry(monitor):
    monitor._signal_log = []
    AlertsMixin._record_signal(monitor, '600519.SH', 'BUY', 100.0, 'RSI low', 'filled')
    assert len(monitor._signal_log) == 1
    entry = monitor._signal_log[0]
    assert entry['symbol'] == '600519.SH'
    assert entry['signal'] == 'BUY'
    assert entry['price'] == 100.0
    assert entry['result'] == 'filled'


def test_record_signal_caps_at_50(monitor):
    monitor._signal_log = [{} for _ in range(50)]
    AlertsMixin._record_signal(monitor, 'X', 'BUY', 10.0, 'r', 'ok')
    assert len(monitor._signal_log) == 50


def test_record_skip_caps_at_50(monitor):
    monitor._skip_log = [{} for _ in range(50)]
    AlertsMixin._record_skip(monitor, 'X', 'cooldown active', 'cooldown')
    assert len(monitor._skip_log) == 50


def test_record_skip_captures_category(monitor):
    monitor._skip_log = []
    AlertsMixin._record_skip(monitor, 'X', 'reason text', 'kelly_insufficient')
    assert monitor._skip_log[0]['category'] == 'kelly_insufficient'


def test_record_llm_review_captures_decision(monitor):
    monitor._llm_review_log = []
    AlertsMixin._record_llm_review(monitor, 'X', 'BUY', True, 'OK signal', 0.85)
    assert monitor._llm_review_log[0] == {
        'time': monitor._llm_review_log[0]['time'],  # 不验时间戳
        'symbol': 'X', 'direction': 'BUY',
        'approved': True, 'reason': 'OK signal', 'confidence': 0.85,
    }


def test_record_position_alert_swallows_errors(monitor):
    """services.alert_history.record_alert 抛异常 → 不传播。"""
    with patch('services.alert_history.record_alert', side_effect=RuntimeError('db locked')):
        # 不应抛
        AlertsMixin._record_position_alert(
            monitor, 'POSITION', 'X', 'msg', 10.0, 1.5)


# ── get_status ──────────────────────────────────────────

def test_get_status_returns_required_fields(monitor):
    monitor._cooldown._last = {}
    monitor._thread = None
    monitor._running = False
    monitor._signal_log = []
    monitor._skip_log = []
    monitor._llm_review_log = []
    status = AlertsMixin.get_status(monitor)
    required = {'running', 'thread_alive', 'trading_mode', 'interval_seconds',
                'last_scan_time', 'last_scan_symbol', 'scan_count',
                'error_count', 'last_error', 'kelly_pct', 'kelly_last_updated',
                'dd_warn', 'dd_stop', 'peak_equity', 'risk_warn_fired',
                'risk_stop_fired', 'cooldown_active',
                'signals', 'skips', 'llm_reviews'}
    assert required.issubset(status.keys())


def test_get_status_cooldown_active_counts_entries(monitor):
    monitor._cooldown.size.return_value = 2
    monitor._thread = None
    monitor._signal_log = []
    monitor._skip_log = []
    monitor._llm_review_log = []
    status = AlertsMixin.get_status(monitor)
    assert status['cooldown_active'] == 2


# ── _llm_review_signal 降级 ─────────────────────────────

def test_llm_review_auto_approves_when_no_llm(monitor):
    """无 LLM → 自动放行,返回 (True, ..., 0.5, 'full')。"""
    monitor._llm = None
    alert = MagicMock()
    alert.symbol = 'X'
    approved, reason, conf, size_rec = AlertsMixin._llm_review_signal(
        monitor, alert, 'BUY',
    )
    assert approved is True
    assert size_rec == 'full'
    assert conf == 0.5


def test_llm_review_rejects_on_inner_exception(monitor, monkeypatch):
    """LLM provider 抛异常 → fail-closed(拒单),不传播。"""
    monkeypatch.delenv('LLM_REVIEW_FAIL_OPEN', raising=False)
    monitor._llm = MagicMock()
    monitor._llm.provider.chat.side_effect = RuntimeError('LLM API down')
    monitor._get_params = MagicMock(return_value={'name': 'X'})
    monitor._svc.get_cash.return_value = 10000
    monitor._svc.get_positions.return_value = []
    monitor._svc.get_position.return_value = None

    alert = MagicMock()
    alert.symbol = 'X'
    alert.signal = 'BUY'
    alert.price = 100.0
    alert.reason = 'test'
    alert.prev_rsi = 25
    approved, reason, _conf, size_rec = AlertsMixin._llm_review_signal(
        monitor, alert, 'BUY',
    )
    assert approved is False
    assert size_rec == 'skip'
    assert 'LLM' in reason or '异常' in reason


def test_llm_review_fail_open_env_restores_legacy_behavior(monitor, monkeypatch):
    """LLM_REVIEW_FAIL_OPEN=1 时,LLM 异常退回旧的自动放行行为(应急用)。"""
    monkeypatch.setenv('LLM_REVIEW_FAIL_OPEN', '1')
    monitor._llm = MagicMock()
    monitor._llm.provider.chat.side_effect = RuntimeError('LLM API down')
    monitor._get_params = MagicMock(return_value={'name': 'X'})
    monitor._svc.get_cash.return_value = 10000
    monitor._svc.get_positions.return_value = []
    monitor._svc.get_position.return_value = None

    alert = MagicMock()
    alert.symbol = 'X'
    alert.signal = 'BUY'
    alert.price = 100.0
    alert.reason = 'test'
    alert.prev_rsi = 25
    approved, _reason, _conf, size_rec = AlertsMixin._llm_review_signal(
        monitor, alert, 'BUY',
    )
    assert approved is True
    assert size_rec == 'full'


# ── _deliver_alert 不配置时静默跳过 ─────────────────────

def test_deliver_alert_skips_without_feishu_config(monitor, monkeypatch):
    """FEISHU_APP_ID 等未配置 → 直接返回,不抛网络异常。"""
    monkeypatch.delenv('FEISHU_APP_ID', raising=False)
    monkeypatch.delenv('FEISHU_APP_SECRET', raising=False)
    monkeypatch.delenv('FEISHU_USER_OPEN_ID', raising=False)
    AlertsMixin._deliver_alert(monitor, '测试消息')
    # 不应有任何网络副作用
