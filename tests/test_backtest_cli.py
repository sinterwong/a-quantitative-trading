# -*- coding: utf-8 -*-
"""tests/test_backtest_cli.py — backtest_cli 与 core.use_cases.backtest 等值回归。

路线图 "回测入口收敛到 core/use_cases" 验收要求：CLI 跑通且关键指标
（CAGR / Sharpe / MaxDD / 交易笔数）与直接调 ``core.use_cases.backtest.run_backtest``
完全一致。本测试用确定性合成 K 线 + 同一 BacktestRequest 走两条路径，
断言 ``to_dict()`` 输出逐字段相等。

通过 monkeypatch ``core.data_gateway.get_gateway`` 注入伪 gateway，避免依赖
网络与真实行情，使测试在 CI 中可复现。
"""

from __future__ import annotations

import argparse
from typing import Any
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from core.use_cases.backtest import BacktestRequest, StrategySpec, run_backtest
from scripts.quant import backtest_cli


def _deterministic_kline(n: int = 600) -> pd.DataFrame:
    """构造可复现 OHLCV，index 已是 DatetimeIndex 让 normalize_kline_index 成 no-op。"""
    rng = np.random.default_rng(seed=42)
    dates = pd.date_range('2022-01-03', periods=n, freq='B')
    closes = 100 + np.cumsum(rng.normal(0, 1.0, size=n))
    closes = np.maximum(closes, 1.0)
    opens = closes + rng.normal(0, 0.3, size=n)
    highs = np.maximum(opens, closes) + np.abs(rng.normal(0, 0.5, size=n))
    lows = np.minimum(opens, closes) - np.abs(rng.normal(0, 0.5, size=n))
    volumes = rng.integers(100_000, 1_000_000, size=n).astype(float)
    df = pd.DataFrame(
        {
            'open': opens,
            'high': highs,
            'low': lows,
            'close': closes,
            'volume': volumes,
        },
        index=dates,
    )
    df.index.name = 'timestamp'
    return df


class _FakeGateway:
    """最小化 gateway 桩：kline() 返回固定 DataFrame 的副本，忽略所有参数。"""

    def __init__(self, df: pd.DataFrame) -> None:
        self._df = df

    def kline(self, *_args: Any, **_kwargs: Any) -> pd.DataFrame:
        return self._df.copy()


def _cli_args(symbol: str = '600519.SH') -> argparse.Namespace:
    """与 backtest_cli._parse_args() 默认值一致的 Namespace。"""
    return argparse.Namespace(
        command='single',
        symbol=symbol,
        start=None,
        end=None,
        days=730,
        capital=200_000.0,
        commission=0.0003,
        rsi_buy=35.0,
        rsi_sell=65.0,
        rsi_period=14,
        train_months=18,
        test_months=6,
        output=None,
    )


def _equivalent_request(args: argparse.Namespace) -> BacktestRequest:
    """复刻 backtest_cli.run_single 里的 BacktestRequest 构造逻辑。"""
    return BacktestRequest(
        symbol=args.symbol,
        start=args.start,
        end=args.end,
        days=args.days,
        initial_equity=args.capital,
        commission_rate=args.commission,
        strategies=[
            StrategySpec(
                factor_name='RSI',
                threshold=0.0,
                params={
                    'period': args.rsi_period,
                    'buy_threshold': args.rsi_buy,
                    'sell_threshold': args.rsi_sell,
                },
            ),
        ],
    )


@pytest.fixture
def fake_gateway() -> Any:
    """同进程内 patch get_gateway，CLI 与直接调用都走伪 gateway。"""
    df = _deterministic_kline()
    fake = _FakeGateway(df)
    with patch('core.data_gateway.get_gateway', return_value=fake):
        yield fake


def test_cli_single_matches_use_case_run(
    fake_gateway: _FakeGateway, capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI single 命令 to_dict() 与直接调 run_backtest().to_dict() 逐字段相等。"""
    args = _cli_args()

    cli_result = backtest_cli.run_single(args)
    capsys.readouterr()  # 丢弃 summary 打印输出

    direct_result = run_backtest(_equivalent_request(args)).to_dict()

    # 关键指标（路线图验收点名的全部 + summary 文本）必须完全相等
    assert cli_result == direct_result


def test_cli_single_returns_runnable_factor_name(
    fake_gateway: _FakeGateway, capsys: pytest.CaptureFixture[str],
) -> None:
    """回归：factor_name 必须解析到注册表里。曾用 'RSIFactor' 名导致 UNKNOWN_FACTOR。"""
    args = _cli_args()
    result = backtest_cli.run_single(args)
    capsys.readouterr()
    # 不抛 UseCaseError 且回测产出有结构化指标即视为路径打通
    assert 'sharpe' in result
    assert 'n_bars' in result
    assert result['n_bars'] > 0
