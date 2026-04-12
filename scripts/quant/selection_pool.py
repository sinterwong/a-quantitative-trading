"""
选股池 + 机构持仓信号过滤系统
基于机构重仓评分构建优质选股池
"""

import os
import sys
import json
from datetime import datetime

quant_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, quant_dir)

from data_loader import DataLoader
import institutional_live as inst_live
from backtest import TechnicalIndicators as TI


class StockSelectionPool:
    """选股池系统"""

    def __init__(self):
        self.loader = DataLoader()
        self.pool = []
        self.current_signals = {}

    def add_to_pool(self, symbol, score, signal):
        self.pool.append({
            'symbol': symbol,
            'score': score,
            'signal': signal,
            'added_time': datetime.now().strftime('%Y-%m-%d')
        })

    def filter_by_score(self, min_score=5.0):
        return [s for s in self.pool if s['score'] >= min_score]

    def filter_by_volatility(self, data, max_atr_pct=5.0):
        if len(data) < 15:
            return True

        closes = [d['close'] for d in data]
        highs = [d['high'] for d in data]
        lows = [d['low'] for d in data]

        trs = []
        for i in range(1, len(data)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            trs.append(tr)

        atr = sum(trs[-14:]) / 14
        current_price = closes[-1]
        atr_pct = (atr / current_price) * 100
        return atr_pct <= max_atr_pct

    def should_enter(self, symbol, data):
        if len(data) < 30:
            return False

        inst = inst_live.get_etf_institutional_score(symbol, '20243')
        if inst.get('score', 0) < 5:
            return False
        if inst.get('signal') == 'sell':
            return False
        if not self.filter_by_volatility(data):
            return False

        closes = [d['close'] for d in data]
        rsi_vals = TI.rsi(closes, 21)
        if len(rsi_vals) < 2:
            return False

        rsi = rsi_vals[-1]
        rsi_prev = rsi_vals[-2]
        if rsi_prev < 35 <= rsi:
            return True
        return False

    def should_exit(self, symbol, data, entry_price):
        if len(data) < 2:
            return 'hold'

        closes = [d['close'] for d in data]
        current_price = closes[-1]

        rsi_vals = TI.rsi(closes, 21)
        if len(rsi_vals) < 2:
            return 'hold'

        rsi = rsi_vals[-1]
        rsi_prev = rsi_vals[-2]
        pnl_pct = (current_price - entry_price) / entry_price

        if pnl_pct <= -0.05:
            return 'stop_loss'
        if pnl_pct >= 0.20:
            return 'take_profit'
        if rsi_prev > 65 >= rsi:
            return 'rsi_sell'

        inst = inst_live.get_etf_institutional_score(symbol, '20243')
        if inst.get('signal') == 'sell':
            return 'inst_sell'
        return 'hold'


def build_default_pool():
    """构建默认选股池（机构重仓股）"""
    default_stocks = [
        ('688981.SH', '中芯国际', 17.15, 4),
        ('600276.SH', '恒瑞医药', 3.85, 3),
        ('600519.SH', '贵州茅台', 2.59, 3),
        ('000858.SZ', '五粮液', 4.03, 5),
        ('601318.SH', '中国平安', 3.89, 2),
        ('300750.SZ', '宁德时代', 2.77, 2),
        ('600309.SH', '万华化学', 3.11, 3),
        ('601919.SH', '中远海控', 2.60, 3),
    ]

    pool = StockSelectionPool()
    for symbol, name, hold_ratio, fund_count in default_stocks:
        score = hold_ratio * fund_count
        signal = 'buy' if score > 5 else ('sell' if score < 2 else 'hold')
        pool.add_to_pool(symbol, score, signal)

    return pool


def test_pool():
    print("=" * 50)
    print("Stock Selection Pool Test")
    print("=" * 50)

    pool = build_default_pool()
    print(f"\nTotal stocks: {len(pool.pool)}")

    print("\nDefault Pool (sorted by score):")
    print("-" * 50)
    for s in sorted(pool.pool, key=lambda x: x['score'], reverse=True):
        print(f"  {s['symbol']}: score={s['score']:.2f}, signal={s['signal']}")

    print("\nFiltered (score >= 5):")
    for s in pool.filter_by_score(5.0):
        print(f"  {s['symbol']}: score={s['score']:.2f}")


if __name__ == '__main__':
    test_pool()
