"""
回测引擎 v3 - 完整版
- 固定止损止盈
- ATR Chandelier止损
- 动态仓位管理（Kelly/ATR/固定）
- 持仓信息暴露给止损管理器
"""

import os
import json
from datetime import datetime
from typing import List, Dict, Callable, Optional


class TechnicalIndicators:
    """技术指标库"""

    @staticmethod
    def sma(closes: List[float], period: int) -> List[float]:
        if len(closes) < period:
            return []
        result = []
        for i in range(period - 1, len(closes)):
            result.append(sum(closes[i - period + 1:i + 1]) / period)
        return result

    @staticmethod
    def ema(closes: List[float], period: int) -> List[float]:
        if len(closes) < period:
            return []
        multiplier = 2 / (period + 1)
        result = [sum(closes[:period]) / period]
        for i in range(period, len(closes)):
            result.append((closes[i] - result[-1]) * multiplier + result[-1])
        return result

    @staticmethod
    def rsi(closes: List[float], period: int = 14) -> List[float]:
        if len(closes) < period + 1:
            return []
        changes = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [c if c > 0 else 0 for c in changes]
        losses = [-c if c < 0 else 0 for c in changes]
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        result = [50]
        for i in range(period, len(changes)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
            rs = avg_gain / (avg_loss if avg_loss > 0 else 0.0001)
            result.append(100 - (100 / (1 + rs)))
        return result

    @staticmethod
    def bollinger_bands(closes: List[float], period: int = 20, std_dev: float = 2.0):
        if len(closes) < period:
            return None, None, None
        mid = TechnicalIndicators.sma(closes, period)
        upper, lower = [], []
        for i in range(period - 1, len(closes)):
            subset = closes[i - period + 1:i + 1]
            mean = sum(subset) / period
            variance = sum((x - mean) ** 2 for x in subset) / period
            std = variance ** 0.5
            upper.append(mean + std_dev * std)
            lower.append(mean - std_dev * std)
        return mid, upper, lower

    @staticmethod
    def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> List[float]:
        if len(closes) < period + 1:
            return []
        trs = []
        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i-1]),
                abs(lows[i] - closes[i-1])
            )
            trs.append(tr)
        atr = [sum(trs[:period]) / period]
        for i in range(period, len(trs)):
            atr.append((atr[-1] * (period - 1) + trs[i]) / period)
        return atr

    @staticmethod
    def rsrs(highs: List[float], lows: List[float], period: int = 18) -> List[float]:
        if len(highs) < period or len(lows) < period:
            return []
        result = []
        for i in range(period - 1, len(highs)):
            window_highs = highs[i - period + 1:i + 1]
            window_lows = lows[i - period + 1:i + 1]
            n = period
            x_mean = (n - 1) / 2
            high_mean = sum(window_highs) / n
            low_mean = sum(window_lows) / n
            cov_h = sum((j - x_mean) * (window_highs[j] - high_mean) for j in range(n))
            var_x = sum((j - x_mean) ** 2 for j in range(n))
            slope_h = cov_h / (var_x if var_x > 0 else 1)
            result.append(slope_h)
        return result


class BacktestEngine:
    """
    回测引擎 v3

    支持:
    - 固定比例止损/止盈
    - ATR Chandelier止损（跟踪止损）
    - 动态仓位管理
    - 持仓状态回调（让外部管理器决定止损）
    """

    def __init__(self, initial_capital: float = 1000000, commission: float = 0.0003,
                 stop_loss: float = None, take_profit: float = None,
                 trailing_stop: float = None,
                 use_atr_stop: bool = False,
                 atr_period: int = 14, atr_multiplier: float = 2.0,
                 position_method: str = 'fixed',  # 'fixed' | 'kelly' | 'atr' | 'volatility'
                 kelly_fraction: float = 0.5,
                 max_position_pct: float = 0.30):
        """
        Args:
            initial_capital: 初始资金
            commission: 交易佣金
            stop_loss: 固定止损比例
            take_profit: 固定止盈比例
            trailing_stop: 跟踪止损比例（从最高点回撤）
            use_atr_stop: 是否使用ATR Chandelier止损
            atr_period: ATR周期
            atr_multiplier: ATR倍数
            position_method: 仓位计算方式
            kelly_fraction: Kelly公式折扣（默认半Kelly=0.5）
            max_position_pct: 最大仓位比例
        """
        self.initial_capital = initial_capital
        self.commission = commission
        self.stop_loss = stop_loss
        self.take_profit = take_profit
        self.trailing_stop = trailing_stop
        self.use_atr_stop = use_atr_stop
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.position_method = position_method
        self.kelly_fraction = kelly_fraction
        self.max_position_pct = max_position_pct

        self.trades: List[Dict] = []
        self.equity_curve: List[Dict] = []

    def _calculate_position_pct(self, data: List[Dict], i: int, signal: str,
                                 capital: float, entry_price: float = 0) -> float:
        """
        计算仓位比例
        """
        if signal != 'buy':
            return 0.0

        closes = [d['close'] for d in data]
        highs = [d['high'] for d in data]
        lows = [d['low'] for d in data]
        current_price = closes[i]

        if self.position_method == 'kelly':
            # Kelly公式 (带最小仓位保护)
            if i >= 60:
                returns = [(closes[j] - closes[j-1]) / closes[j-1]
                          for j in range(max(1, i-60), i)]
                wins = [r for r in returns if r > 0]
                losses = [r for r in returns if r < 0]
                if wins and losses:
                    win_rate = len(wins) / len(returns)
                    avg_win = sum(wins) / len(wins)
                    avg_loss = abs(sum(losses) / len(losses)) if losses else avg_win
                    b = avg_win / avg_loss if avg_loss > 0 else 1
                    raw_kelly = (b * win_rate - (1 - win_rate)) / b
                    kelly = raw_kelly * self.kelly_fraction
                    # Kelly可能为负(胜率<50%)，设置最小仓位5%
                    kelly = max(0.05, min(kelly, 0.5))
                else:
                    kelly = 0.10
            else:
                kelly = 0.10
            return min(kelly, self.max_position_pct)

        elif self.position_method == 'atr':
            # ATR波动率调整仓位
            atr_vals = TechnicalIndicators.atr(highs, lows, closes, self.atr_period)
            if atr_vals:
                atr = atr_vals[-1]
                atr_pct = atr / current_price
                target_risk = 0.02  # 每笔最多亏2%
                risk_per_share = atr * self.atr_multiplier
                position_pct = min(target_risk * current_price / risk_per_share, self.max_position_pct)
                return position_pct
            return self.max_position_pct

        elif self.position_method == 'volatility':
            # 波动率倒数仓位
            if i >= 20:
                returns = [(closes[j] - closes[j-1]) / closes[j-1] for j in range(max(1, i-20), i)]
                vol = (sum(r**2 for r in returns) / len(returns)) ** 0.5
                target_vol = 0.15
                position_pct = min(target_vol / (vol + 0.001), self.max_position_pct)
                return position_pct
            return 0.20

        else:
            # 固定仓位
            return self.max_position_pct

    def _get_atr_stop_price(self, data: List[Dict], entry_idx: int,
                             entry_price: float, current_idx: int) -> float:
        """ATR Chandelier止损价"""
        highs = [d['high'] for d in data]
        lows = [d['low'] for d in data]
        closes = [d['close'] for d in data]

        atr_vals = TechnicalIndicators.atr(highs, lows, closes, self.atr_period)

        # 用入场以来的最高价计算
        window_highs = highs[entry_idx:current_idx+1]
        peak_price = max(window_highs) if window_highs else entry_price

        # ATR值
        atr_idx = min(current_idx, len(atr_vals) - 1)
        atr = atr_vals[atr_idx] if atr_vals else entry_price * 0.02

        stop_price = peak_price - self.atr_multiplier * atr
        # 止损价不能低于成本价-15%
        stop_price = max(stop_price, entry_price * 0.85)

        return stop_price

    def run(self, data: List[Dict], signal_func: Callable,
            strategy_name: str = "strategy") -> Dict:
        """运行回测"""

        cash = self.initial_capital
        position = 0
        entry_price = 0
        entry_date = None
        entry_idx = 0
        peak_price = 0

        wins = 0
        losses = 0
        self.trades = []
        self.equity_curve = []
        stop_triggers = {'atr_stop': 0, 'trailing_stop': 0, 'stop_loss': 0, 'take_profit': 0}

        closes = [d['close'] for d in data]
        highs = [d['high'] for d in data]
        lows = [d['low'] for d in data]

        for i in range(1, len(data)):
            row = data[i]
            current_price = row['close']

            # 更新权益曲线
            portfolio_value = cash + position * current_price
            self.equity_curve.append({
                'date': row['date'],
                'value': portfolio_value,
                'position': position,
                'cash': cash,
                'price': current_price
            })

            # ========== 止损/止盈检查（持仓中）==========
            if position > 0:
                pnl_pct = (current_price - entry_price) / entry_price

                # 更新峰值价格
                if current_price > peak_price:
                    peak_price = current_price

                stop_triggered = False
                stop_reason = None

                # === ATR Chandelier止损 ===
                if self.use_atr_stop:
                    atr_stop_price = self._get_atr_stop_price(data, entry_idx, entry_price, i)
                    if current_price <= atr_stop_price:
                        stop_triggered = True
                        stop_reason = 'atr_stop'

                # === 跟踪止损 ===
                if not stop_triggered and self.trailing_stop:
                    drawdown = (peak_price - current_price) / peak_price
                    if drawdown >= self.trailing_stop:
                        stop_triggered = True
                        stop_reason = 'trailing_stop'

                # === 固定止损 ===
                if not stop_triggered and self.stop_loss and pnl_pct <= -self.stop_loss:
                    stop_triggered = True
                    stop_reason = 'stop_loss'

                # === 固定止盈 ===
                if not stop_triggered and self.take_profit and pnl_pct >= self.take_profit:
                    stop_triggered = True
                    stop_reason = 'take_profit'

                # === 执行止损/止盈 ===
                if stop_triggered:
                    stop_triggers[stop_reason] += 1
                    revenue = position * current_price * (1 - self.commission)
                    self.trades.append({
                        'action': 'sell',
                        'date': row['date'],
                        'price': current_price,
                        'shares': position,
                        'value': revenue,
                        'pnl_pct': pnl_pct * 100,
                        'reason': stop_reason,
                        'pnl_type': 'win' if revenue > position * entry_price else 'loss',
                        'holding_days': self._days_between(entry_date, row['date'])
                    })

                    if revenue > position * entry_price:
                        wins += 1
                    else:
                        losses += 1

                    cash += revenue
                    position = 0
                    entry_price = 0
                    entry_idx = 0
                    peak_price = 0
                    continue

            # ========== 买入信号检查 ==========
            if position == 0:
                signal = signal_func(data, i)
                if signal == 'buy':
                    # 计算仓位比例
                    pos_pct = self._calculate_position_pct(data, i, signal, cash)
                    position_value = cash * pos_pct
                    shares = int(position_value / (current_price * (1 + self.commission)))

                    if shares > 0:
                        cost = shares * current_price * (1 + self.commission)
                        position = shares
                        cash -= cost
                        entry_price = current_price
                        entry_date = row['date']
                        entry_idx = i
                        peak_price = current_price

                        self.trades.append({
                            'action': 'buy',
                            'date': row['date'],
                            'price': current_price,
                            'shares': position,
                            'value': cost,
                            'position_pct': pos_pct
                        })

            # ========== 卖出信号检查（持仓中）==========
            elif position > 0:
                signal = signal_func(data, i)
                if signal == 'sell':
                    revenue = position * current_price * (1 - self.commission)
                    pnl_pct_val = (current_price - entry_price) / entry_price * 100

                    self.trades.append({
                        'action': 'sell',
                        'date': row['date'],
                        'price': current_price,
                        'shares': position,
                        'value': revenue,
                        'pnl_pct': pnl_pct_val,
                        'reason': 'signal',
                        'pnl_type': 'win' if revenue > position * entry_price else 'loss',
                        'holding_days': self._days_between(entry_date, row['date'])
                    })

                    if revenue > position * entry_price:
                        wins += 1
                    else:
                        losses += 1

                    cash += revenue
                    position = 0
                    entry_price = 0
                    entry_idx = 0
                    peak_price = 0

        # === 最后一日强制平仓 ===
        if position > 0:
            final_price = data[-1]['close']
            revenue = position * final_price * (1 - self.commission)
            pnl_pct_val = (final_price - entry_price) / entry_price * 100
            self.trades.append({
                'action': 'close_final',
                'date': data[-1]['date'],
                'price': final_price,
                'shares': position,
                'value': revenue,
                'pnl_pct': pnl_pct_val,
                'reason': 'end_of_data',
                'pnl_type': 'win' if revenue > position * entry_price else 'loss',
                'holding_days': self._days_between(entry_date, data[-1]['date'])
            })
            if revenue > position * entry_price:
                wins += 1
            else:
                losses += 1
            cash += revenue
            position = 0

        return self._compute_metrics(wins, losses, cash, data, strategy_name, stop_triggers)

    def _days_between(self, d1, d2):
        if isinstance(d1, str):
            d1 = datetime.strptime(d1.split()[0], '%Y-%m-%d')
        if isinstance(d2, str):
            d2 = datetime.strptime(d2.split()[0], '%Y-%m-%d')
        return max((d2 - d1).days, 0)

    def _compute_metrics(self, wins, losses, cash, data, strategy_name, stop_triggers) -> Dict:
        final_value = cash
        total_trades = wins + losses
        win_rate = wins / total_trades * 100 if total_trades > 0 else 0
        total_return_pct = (final_value - self.initial_capital) / self.initial_capital * 100

        try:
            start_str = data[0]['date'].split()[0] if isinstance(data[0]['date'], str) else str(data[0]['date'])
            end_str = data[-1]['date'].split()[0] if isinstance(data[-1]['date'], str) else str(data[-1]['date'])
            start = datetime.strptime(start_str, '%Y-%m-%d')
            end = datetime.strptime(end_str, '%Y-%m-%d')
            years = max((end - start).days / 365, 0.01)
            annualized = ((final_value / self.initial_capital) ** (1 / years) - 1) * 100
        except:
            years = 1
            annualized = total_return_pct

        # 夏普
        returns = []
        for j in range(1, len(self.equity_curve)):
            ret = (self.equity_curve[j]['value'] - self.equity_curve[j-1]['value']) / self.equity_curve[j-1]['value']
            returns.append(ret)
        if len(returns) > 1:
            mean_ret = sum(returns) / len(returns)
            std_ret = (sum((r - mean_ret) ** 2 for r in returns) / len(returns)) ** 0.5
            sharpe = (mean_ret / std_ret * (252 ** 0.5)) if std_ret > 0 else 0
        else:
            sharpe = 0

        # 最大回撤
        peak = self.initial_capital
        max_dd = 0
        for eq in self.equity_curve:
            if eq['value'] > peak:
                peak = eq['value']
            dd = (peak - eq['value']) / peak * 100
            if dd > max_dd:
                max_dd = dd

        # 止损/止盈触发统计
        stop_triggers = {'atr_stop': 0, 'trailing_stop': 0, 'stop_loss': 0, 'take_profit': 0}
        for t in self.trades:
            if t.get('action') == 'sell' and t.get('reason') in stop_triggers:
                stop_triggers[t['reason']] += 1

        return {
            'strategy': strategy_name,
            'initial_capital': self.initial_capital,
            'final_value': final_value,
            'total_return_pct': total_return_pct,
            'annualized_return_pct': annualized,
            'sharpe_ratio': sharpe,
            'max_drawdown_pct': max_dd,
            'total_trades': total_trades,
            'wins': wins,
            'losses': losses,
            'win_rate_pct': win_rate,
            'years': years,
            'position_method': self.position_method,
            'stop_triggers': stop_triggers,
            'use_atr_stop': self.use_atr_stop,
            'atr_multiplier': self.atr_multiplier if self.use_atr_stop else None,
            'stop_loss': self.stop_loss,
            'take_profit': self.take_profit,
            'trailing_stop': self.trailing_stop
        }

    def get_trades(self) -> List[Dict]:
        return self.trades

    def get_equity_curve(self) -> List[Dict]:
        return self.equity_curve
