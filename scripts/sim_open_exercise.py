"""
sim_open_exercise.py — 盘中信号全链路模拟

模拟 9:30 开盘后第一个轮询周期，验证：
  1. StrategyRunner.run_once()  →  FactorPipeline combined_scores
  2. IntradayMonitor._check_new_positions()  →  建仓信号
  3. IntradayMonitor._check_and_push()     →  持仓追加信号
  4. _submit_order_for_signal()             →  分钟级二次确认
  5. ExitEngine DD 信号

用法：
  python scripts/sim_open_exercise.py
"""

import sys, os, logging, time
from datetime import datetime, date
from unittest.mock import patch, MagicMock, PropertyMock

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
sys.path.insert(0, os.path.join(PROJ, 'backend'))

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)-8s] %(name)s — %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger('sim')

# ─────────────────────────────────────────────────────────────────────────────
# 1. 构造 mock 数据
# ─────────────────────────────────────────────────────────────────────────────

MOCK_POSITIONS = [
    {'symbol': '000001.SZ', 'shares': 1000, 'current_price': 12.50,
     'day_chg': 1.2, 'rsi_buy': 25, 'rsi_sell': 65},
    {'symbol': '600519.SH', 'shares': 200, 'current_price': 1680.0,
     'day_chg': -0.8, 'rsi_buy': 30, 'rsi_sell': 70},
    {'symbol': '300750.SZ', 'shares': 500, 'current_price': 260.0,
     'day_chg': 3.5, 'rsi_buy': 28, 'rsi_sell': 68},
]

# Pipeline scores — 覆盖持仓标的 + 两个新候选
# 建仓阈值 0.50，追加阈值 0.30
MOCK_PIPELINE_SCORES = {
    '000001.SZ':  0.3685,   # 持仓，score > 0.30 → 追加信号
    '600519.SH':  0.1492,   # 持仓，score < 0.30 → 无追加
    '300750.SZ':  0.5510,   # 持仓，score > 0.30 → 追加信号
    '601318.SH':  0.6100,   # 新候选，score > 0.50 → 建仓候选
    '000002.SZ':  0.4200,   # 新候选，score < 0.50 → 跳过
}

MOCK_REGIME = None


def make_mock_runner(last_scores: dict):
    """返回一个 mock StrategyRunner，只覆盖 last_scores 和 current_regime。"""
    runner = MagicMock()
    type(runner).last_scores = PropertyMock(return_value=last_scores)
    runner.current_regime = MOCK_REGIME
    return runner


# ─────────────────────────────────────────────────────────────────────────────
# 2. 模拟 IntradayMonitor 全流程（带 mock）
# ─────────────────────────────────────────────────────────────────────────────

def simulate_open_cycle():
    print("\n" + "═" * 70)
    print("  模拟开盘第一次轮询 — 9:35 CST")
    print("═" * 70)

    # ── 2a. PortfolioService mock ────────────────────────────────────────
    from services.intraday_monitor import IntradayMonitor
    from services.portfolio import PortfolioService
    from services.broker import PaperBroker

    svc = MagicMock(spec=PortfolioService)
    svc.refresh_prices = MagicMock()
    svc.get_positions.return_value = MOCK_POSITIONS
    svc.get_portfolio_summary.return_value = {'total_equity': 500_000.0, 'cash': 300_000.0}
    svc.get_cash.return_value = 300_000.0

    # ── 2b. Broker mock — 记录所有下单调用 ─────────────────────────────
    class MockOrderResult:
        status = 'filled'
        avg_price = 100.0
        filled_shares = 0

    order_calls = []

    def _submit_order(symbol, direction, shares, price, price_type, **kwargs):
        order_calls.append({
            'symbol': symbol, 'direction': direction,
            'shares': shares, 'price': price, 'price_type': price_type,
        })
        r = MockOrderResult()
        r.filled_shares = shares
        r.avg_price = price
        return r

    broker = MagicMock(spec=PaperBroker)
    broker.submit_order = MagicMock(side_effect=_submit_order)
    broker.connect = MagicMock()

    # ── 2c. IntradayMonitor init ─────────────────────────────────────────
    monitor = IntradayMonitor(
        svc=svc, broker=broker,
        check_interval=300, max_position_pct=0.20,
        llm_service=None,
    )

    # ── 2d. StrategyRunner mock ─────────────────────────────────────────
    mock_runner = make_mock_runner(MOCK_PIPELINE_SCORES)
    monitor.set_strategy_runner(mock_runner)
    logger.info("StrategyRunner injected — last_scores: %s", MOCK_PIPELINE_SCORES)

    # ── 2e. 单次轮询模拟 ─────────────────────────────────────────────────
    now = datetime(2026, 5, 4, 9, 35, 0)   # 模拟周一开盘
    print(f"\n{'─' * 70}")
    print(f"  [模拟时间] {now.strftime('%Y-%m-%d %H:%M')} CST")
    print(f"{'─' * 70}")

    # 阶段1：_check_new_positions() — 建仓候选
    print("\n📌 阶段1：_check_new_positions() — 建仓信号检查（阈值 0.50）")
    logger.info("=== _check_new_positions START ===")
    with patch('services.signals.fetch_realtime') as mock_fetch:
        mock_fetch.return_value = {
            'close': 100.0, 'pct': 0.5, 'day_chg': 0.5, 'volume_ratio': 1.2
        }
        with patch('services.signals.get_minute_rsi', return_value=40.0):
            with patch('services.signals.confirm_signal_minute',
                       return_value=(True, 40.0, '15min RSI=40<55，确认买入动力充足')):
                monitor._check_new_positions(now)
    logger.info("=== _check_new_positions END ===")

    # 阶段2：_check_and_push() — 持仓追加 + 板块/大盘/退出检查
    print("\n📌 阶段2：_check_and_push() — 持仓追加（阈值 0.30）")
    logger.info("=== _check_and_push START ===")
    with patch('services.signals.fetch_realtime') as mock_fetch:
        mock_fetch.return_value = {
            'close': 100.0, 'pct': 0.5, 'day_chg': 0.5, 'volume_ratio': 1.2
        }
        with patch('services.signals.get_minute_rsi', return_value=40.0):
            with patch('services.signals.confirm_signal_minute',
                       return_value=(True, 40.0, '15min RSI=40<55，确认买入动力充足')):
                monitor._check_and_push(now)
    logger.info("=== _check_and_push END ===")

    # ── 汇总 ─────────────────────────────────────────────────────────────
    print("\n" + "═" * 70)
    print("  模拟结果汇总")
    print("═" * 70)
    print(f"\nPipeline scores:\n  " + "\n  ".join(
        f"{k}: {v:.4f}" for k, v in sorted(MOCK_PIPELINE_SCORES.items())))
    print(f"\n持仓列表: {[p['symbol'] for p in MOCK_POSITIONS]}")
    print(f"\n建仓信号阈值: 0.50 | 持仓追加阈值: 0.30")
    new_candidates = [k for k, v in MOCK_PIPELINE_SCORES.items()
                      if v > 0.50 and k not in [p['symbol'] for p in MOCK_POSITIONS]]
    add_signals   = [p['symbol'] for p in MOCK_POSITIONS
                     if MOCK_PIPELINE_SCORES.get(p['symbol'], 0) > 0.30]
    print(f"  → 触发建仓候选: {new_candidates}")
    print(f"  → 触发持仓追加: {add_signals}")
    print(f"\n  broker.submit_order 调用次数: {broker.submit_order.call_count}")
    for i, call in enumerate(order_calls, 1):
        print(f"    [{i}] {call['direction']} {call['symbol']} "
              f"{call['shares']}股 @ {call['price']}")


if __name__ == '__main__':
    simulate_open_cycle()
