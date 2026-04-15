"""
Kelly 公式仓位管理器 (S3-T3)
================================
基于历史交易统计计算最优仓位。
- 全 Kelly: f* = W/R（W=胜率，R=盈亏比）
- 半 Kelly（更保守）: f* × 0.5
- 最小仓位：5%，最大仓位：30%
"""

from typing import Optional


def compute_kelly(win_rate: float, avg_win: float, avg_loss: float) -> float:
    """
    计算 Kelly 公式最优仓位比例。

    Args:
        win_rate: 胜率（0.0 ~ 1.0），例如 0.58 表示 58% 胜率
        avg_win:  平均每笔盈利（元），例如 500
        avg_loss: 平均每笔亏损（元），例如 300（填正数）

    Returns:
        float: 半 Kelly 仓位比例（0.0 ~ 1.0）
              例如返回 0.15 表示用 15% 的可交易资金开仓
              最小 5%，最大 30%
    """
    # 亏损为正数时转负
    avg_loss = abs(avg_loss)
    if avg_loss <= 0:
        return 0.05
    if win_rate <= 0:
        return 0.05
    if win_rate >= 1.0:
        return 0.30  # 100% 胜率 Kelly 上限 30%

    # 盈亏比
    R = avg_win / avg_loss

    # Kelly 公式: f* = W - (1-W)/R
    kelly_full = win_rate - (1.0 - win_rate) / R

    # 半 Kelly 更稳健（避免单笔过大的回撤）
    kelly_half = kelly_full * 0.5

    # 边界限制
    MIN_POS = 0.05   # 5%
    MAX_POS = 0.30   # 30%

    f = max(MIN_POS, min(MAX_POS, kelly_half))
    return round(f, 4)


def compute_kelly_from_trades(trades: list) -> float:
    """
    从交易记录列表计算 Kelly 仓位。

    trades: [{pnl: float}, ...] — pnl 可正可负
    """
    if not trades or len(trades) < 5:
        return 0.10  # 数据不足，用默认 10%

    wins = [t['pnl'] for t in trades if t.get('pnl', 0) > 0]
    losses = [abs(t['pnl']) for t in trades if t.get('pnl', 0) < 0]

    if not wins or not losses:
        return 0.10

    win_rate = len(wins) / len(trades)
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0

    return compute_kelly(win_rate, avg_win, avg_loss)


def calc_shares_for_kelly(cash: float, price: float, kelly_pct: float) -> int:
    """
    根据 Kelly 仓位比例计算可买股数（整手 100 股）。

    Args:
        cash: 可用资金（元）
        price: 当前股价（元）
        kelly_pct: Kelly 仓位比例（0.0 ~ 1.0）

    Returns:
        int: 整手股数（100 的整数倍，最小 100）
    """
    if price <= 0 or cash <= 0:
        return 0
    raw = (cash * kelly_pct) / price
    shares = int(raw // 100 * 100)
    return max(100, shares)


# ── 单元测试 ──────────────────────────────────────────
if __name__ == '__main__':
    print('=== Kelly 公式测试 ===')

    # 基准：58% 胜率，盈亏比 1.5:1
    f = compute_kelly(0.58, 500, 300)
    print('胜率 58%% 盈 500 亏 300 → Kelly=%.1f%%' % (f * 100))
    assert 0.05 <= f <= 0.30

    # 高胜率 + 低盈亏比
    f2 = compute_kelly(0.65, 400, 400)
    print('胜率 65%% 盈 400 亏 400 -> Kelly=%.1f%%' % (f2 * 100))
    assert f2 == 0.15  # R=1 -> kelly_full=0.30, 半 Kelly=0.15
    assert 0.05 <= f2 <= 0.30

    # 低胜率高盈亏比
    f3 = compute_kelly(0.40, 1000, 200)
    print('胜率 40%% 盈 1000 亏 200 → Kelly=%.1f%%' % (f3 * 100))
    assert f3 > 0.05
    print()

    # 模拟交易记录测试
    mock_trades = [
        {'pnl': 500}, {'pnl': -300}, {'pnl': 600}, {'pnl': -200},
        {'pnl': 400}, {'pnl': -250}, {'pnl': 550}, {'pnl': -180},
        {'pnl': 480}, {'pnl': -310},
    ]
    f4 = compute_kelly_from_trades(mock_trades)
    print('从 10 笔交易记录 → Kelly=%.1f%%' % (f4 * 100))
    assert f4 > 0

    # 股数计算
    shares = calc_shares_for_kelly(10000, 4.50, f4)
    print('可用资金 10000，价 4.50，Kelly %.1f%% → 可买 %d 股' % (f4 * 100, shares))
    assert shares % 100 == 0

    print()
    print('ALL TESTS PASSED')
