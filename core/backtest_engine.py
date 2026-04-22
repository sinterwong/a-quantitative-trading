"""
core/backtest_engine.py — 事件驱动回测引擎

Phase 6 核心组件：

1. BacktestEngine        — 事件驱动回测（支持多标的/组合/风控）
2. PerformanceAnalyzer    — 绩效归因（夏普/最大回撤/IC/IR/胜率/滑点）
3. FactorResearcher       — 多因子研究（网格搜索/IC分析/WFA）
4. SignalBacktester       — 快速单因子回测（用于因子筛选）
5. WalkForwardAnalyzer    — Walk-Forward 滚动验证

设计原则：
  - 回测代码 = 实盘代码（同一 Signal 接口）
  - 事件驱动：每根 K 线触发一次 signal → order → fill → risk 循环
  - 滑点/佣金模型：可配置
  - 支持多标的组合同时回测
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional, Literal, Tuple
from collections import defaultdict
import numpy as np
import pandas as pd

from core.factors.base import Factor, Signal
from core.oms import Order


# ─── 回测数据结构 ───────────────────────────────────────────────────────────

@dataclass
class TradeRecord:
    """成交记录（回测日志）"""
    timestamp: datetime
    symbol: str
    direction: Literal['BUY', 'SELL']
    price: float
    shares: int
    value: float          # 成交金额
    commission: float     # 佣金
    slippage_bps: float  # 滑点（bp）
    signal_reason: str    # 信号来源
    signal_strength: float
    holding_period: int   # 持仓秒数
    pnl: float = 0        # 闭环盈亏（平仓时填）
    realized_pnl: float = 0


@dataclass
class PositionSnapshot:
    """持仓快照"""
    symbol: str
    shares: int = 0
    avg_price: float = 0
    current_price: float = 0
    unrealized_pnl: float = 0
    unrealized_pnl_pct: float = 0
    entry_high: float = 0
    holding_secs: int = 0


@dataclass
class DailyStats:
    """每日统计"""
    date: date
    equity: float
    position_value: float
    cash: float
    daily_return: float
    daily_pnl: float
    n_trades: int
    n_positions: int


@dataclass
class BacktestConfig:
    """回测配置"""
    initial_equity: float = 100_000
    commission_rate: float = 0.0003   # 万3
    min_commission: float = 5.0        # 最低佣金
    stamp_tax_rate: float = 0.001      # 印花税 0.1%（A 股卖出单向）
    slippage_bps: float = 5.0          # 滑点 5bp
    risk_free_rate: float = 0.03       # 无风险利率
    allow_short: bool = False
    max_position_pct: float = 0.25    # 单标的最大仓位
    bar_freq: str = 'daily'            # 'daily' | 'hourly' | 'minute'


@dataclass
class BacktestResult:
    """回测结果"""
    equity_curve: pd.Series            # 净值曲线
    daily_stats: List[DailyStats]
    trades: List[TradeRecord]
    positions: Dict[str, PositionSnapshot]  # 当前持仓
    config: BacktestConfig
    total_days: int
    n_trades: int

    # 绩效指标
    total_return: float = 0
    annual_return: float = 0
    annual_vol: float = 0
    sharpe: float = 0
    max_drawdown: float = 0
    max_drawdown_pct: float = 0
    win_rate: float = 0
    profit_factor: float = 0
    avg_holding_period: float = 0       # 平均持仓时长（秒）
    calmar_ratio: float = 0
    sortino_ratio: float = 0

    # 因子绩效
    factor_ic: float = 0               # IC（预测相关性）
    factor_ir: float = 0               # IC / std(IC)

    def summary(self) -> str:
        return (
            f"回测结果：\n"
            f"  总收益: {self.total_return*100:.2f}%  年化: {self.annual_return*100:.2f}%\n"
            f"  夏普: {self.sharpe:.3f}  卡玛: {self.calmar_ratio:.3f}  索提诺: {self.sortino_ratio:.3f}\n"
            f"  最大回撤: {self.max_drawdown_pct*100:.2f}%\n"
            f"  胜率: {self.win_rate*100:.1f}%  盈亏比: {self.profit_factor:.2f}\n"
            f"  交易次数: {self.n_trades}  均持仓: {self.avg_holding_period/3600:.1f}h\n"
            f"  IC: {self.factor_ic:.4f}  IR: {self.factor_ir:.4f}"
        )


# ─── BacktestEngine ───────────────────────────────────────────────────────────

class BacktestEngine:
    """
    事件驱动回测引擎。

    用法：
      engine = BacktestEngine(config=BacktestConfig(...))
      engine.load_data(symbol, data_df)  # data_df: columns=[open,high,low,close,volume]
      engine.add_strategy(factor, signal_threshold=1.0)
      result = engine.run()

    事件循环：
      for each bar in data:
          emit MarketEvent
          for each strategy:
              signal = strategy.evaluate(bar)
              if signal:
                  risk_check(signal)
                  order = signal_to_order(signal)
                  fill = simulate_fill(order)
                  update_position(fill)
                  record_trade(fill)
    """

    def __init__(self, config: Optional[BacktestConfig] = None):
        self.config = config or BacktestConfig()
        self._data: Dict[str, pd.DataFrame] = {}   # symbol → bars
        self._strategies: List[Tuple[Factor, float, dict]] = []  # (factor, threshold, params)
        self._equity = self.config.initial_equity
        self._cash = self.config.initial_equity
        self._positions: Dict[str, PositionSnapshot] = {}  # symbol → snapshot
        self._trades: List[TradeRecord] = []
        self._daily_stats: List[DailyStats] = []
        self._equity_curve: List[float] = []
        self._pending_orders: Dict[str, Order] = {}  # symbol → order

        # 统计
        self._wins = 0
        self._losses = 0
        self._total_profit = 0.0
        self._total_loss = 0.0
        self._holding_periods: List[int] = []
        self._position_entries: Dict[str, datetime] = {}  # symbol → entry time

    def load_data(
        self,
        symbol: str,
        df: pd.DataFrame,
        adj_type: str = 'qfq',
    ) -> 'BacktestEngine':
        """
        加载 K 线数据。
        df 必须包含: open, high, low, close, volume 列。
        索引为 datetime。

        adj_type: 复权类型，'qfq'=前复权（默认），'hfq'=后复权，'none'=不复权。
        回测要求前复权数据以避免因复权引起的虚假信号。
        """
        required = {'open', 'high', 'low', 'close', 'volume'}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Missing columns: {missing}")
        if adj_type not in ('qfq', 'hfq', 'none'):
            raise ValueError(f"adj_type must be 'qfq', 'hfq', or 'none', got '{adj_type}'")
        data = df.copy()
        # 标记停牌日（成交量为 0）
        if 'is_suspended' not in data.columns:
            data['is_suspended'] = data['volume'] == 0
        data.attrs['adj_type'] = adj_type
        self._data[symbol] = data
        return self

    def add_strategy(
        self,
        factor: Factor,
        threshold: float = 1.0,
        **params,
    ) -> 'BacktestEngine':
        """添加策略因子"""
        self._strategies.append((factor, threshold, params))
        return self

    def _bar_secs(self) -> int:
        """根据 bar 频率返回每根 bar 对应的秒数"""
        freq = self.config.bar_freq
        if freq == 'daily':
            return 86400
        elif freq == 'hourly':
            return 3600
        elif freq == 'minute':
            return 60
        return 86400

    def run(self) -> BacktestResult:
        """执行回测"""
        if not self._data:
            raise ValueError("No data loaded. Call load_data() first.")

        # 按时间对齐所有标的
        all_dates = set()
        for df in self._data.values():
            all_dates.update(df.index)
        all_dates = sorted(all_dates)

        self._reset()

        for i, dt in enumerate(all_dates):
            # 下一根 bar 的 open（用于无前视偏差的成交价）
            next_dt = all_dates[i + 1] if i + 1 < len(all_dates) else None
            self._on_bar(dt, next_dt)

        return self._make_result()

    def _reset(self):
        self._equity = self.config.initial_equity
        self._cash = self.config.initial_equity
        self._positions.clear()
        self._trades.clear()
        self._daily_stats.clear()
        self._equity_curve.clear()
        self._wins = 0
        self._losses = 0
        self._total_profit = 0.0
        self._total_loss = 0.0
        self._holding_periods.clear()

    def _on_bar(self, dt: datetime, next_dt: Optional[datetime] = None):
        """处理每根 K 线

        信号用截止到当前 bar *之前*（排除当前 bar）的数据生成，
        成交价用*下一根* bar 的 open（消除收盘价前视偏差）。
        若已是最后一根 bar（next_dt 为 None），不开新仓，只更新持仓。
        """
        bar_secs = self._bar_secs()

        for symbol, df in self._data.items():
            if dt not in df.index:
                continue

            bar = df.loc[dt]
            is_suspended = bool(bar.get('is_suspended', False))
            pos = self._positions.get(symbol)

            # 更新持仓当前价（停牌日维持停牌前收盘价，不更新 entry_high）
            if pos and pos.shares > 0:
                if not is_suspended:
                    pos.current_price = float(bar['close'])
                    if pos.current_price > pos.entry_high:
                        pos.entry_high = pos.current_price
                pos.unrealized_pnl = (pos.current_price - pos.avg_price) * pos.shares
                pos.unrealized_pnl_pct = (pos.current_price - pos.avg_price) / pos.avg_price if pos.avg_price else 0
                # 正确累加持仓时长（按 bar 频率换算秒数）
                pos.holding_secs += bar_secs

            if next_dt is None:
                # 最后一根 bar，不生成新信号（无法用下一根 open 成交）
                continue

            if is_suspended:
                # 停牌日跳过开仓信号，但允许已有持仓的收盘更新（已在上方处理）
                continue

            # 生成信号时仅用截止到*上一根* bar 的历史（排除当前 bar，消除前视偏差）
            signals = self._generate_signals(symbol, df, dt, bar)

            for sig in signals:
                # 下一根 bar 的 open 作为成交价
                next_bar = df.loc[next_dt] if next_dt in df.index else None
                if next_bar is None:
                    continue
                # 若下一根 bar 也是停牌日，跳过成交
                if bool(next_bar.get('is_suspended', False)):
                    continue
                self._process_signal(sig, next_dt, next_bar)

        # 更新日终统计
        self._update_daily(dt)

    def _generate_signals(
        self,
        symbol: str,
        df: pd.DataFrame,
        dt: datetime,
        bar: pd.Series,
    ) -> List[Signal]:
        """用截止到当前 bar *之前*的历史数据计算因子信号（消除前视偏差）"""
        signals = []

        # 排除当前 bar：只取 dt 之前的数据
        idx = df.index.get_loc(dt)
        if idx == 0:
            return signals  # 没有历史数据，跳过
        hist = df.iloc[max(0, idx - 100):idx]  # 最多 100 根历史 bar

        # 信号强度参考当前 bar 的 close（仅用于 strength 计算，不用于成交）
        ref_price = float(bar['close'])

        for factor, threshold, params in self._strategies:
            try:
                fv = factor.evaluate(hist)
                if len(fv) == 0:
                    continue
                sigs = factor.signals(fv, price=ref_price)
                signals.extend(sigs)
            except Exception:
                pass

        return signals

    def _process_signal(self, sig: Signal, dt: datetime, bar: pd.Series):
        """处理信号：风控检查 → 下单 → 成交

        dt / bar 均为*下一根* bar（next bar），成交价取该 bar 的 open。
        这样彻底消除以收盘价成交的前视偏差。
        """
        sym = sig.symbol
        pos = self._positions.get(sym)

        # 用下一根 bar 的 open 作为基准成交价
        exec_price = float(bar['open'])

        if sig.direction == 'BUY':
            # 检查是否已有持仓
            if pos and pos.shares > 0:
                return  # 已有持仓，不加仓

            # 风控：仓位上限
            if not self._can_buy(exec_price, sig.metadata.get('shares')):
                return

            shares = sig.metadata.get('shares', self._calc_shares_price(exec_price))
            if shares <= 0:
                return

            fill_price = self._simulate_fill(sig.direction, exec_price)
            self._execute_buy(sym, fill_price, shares, sig, dt)

        elif sig.direction == 'SELL':
            if not pos or pos.shares == 0:
                return  # 无持仓

            fill_price = self._simulate_fill(sig.direction, exec_price)
            self._execute_sell(sym, fill_price, pos.shares, sig, dt)

    def _can_buy(self, price: float, shares: int = None) -> bool:
        """PreTrade 风控"""
        est_cost = price * (shares or self._calc_shares_from_equity(price))
        total_mv = sum(
            p.shares * p.current_price for p in self._positions.values() if p.shares > 0
        )
        if (total_mv + est_cost) / self._equity > self.config.max_position_pct * 4:
            return False
        return True

    def _calc_kelly_params(self) -> tuple[float, float, float]:
        """从历史已平仓交易动态计算 win_rate / avg_win / avg_loss。
        前 N 笔不足时退回默认值。"""
        closed = [t for t in self._trades if t.realized_pnl != 0]
        if len(closed) < 10:
            # 历史不足，使用保守默认值
            return 0.50, 0.015, 0.010
        wins = [t.realized_pnl for t in closed if t.realized_pnl > 0]
        losses = [abs(t.realized_pnl) for t in closed if t.realized_pnl < 0]
        win_rate = len(wins) / len(closed)
        avg_win_pnl = float(np.mean(wins)) if wins else 0.015
        avg_loss_pnl = float(np.mean(losses)) if losses else 0.010
        # 转换为收益率（相对于当前权益）
        equity = max(self._get_equity(), 1)
        avg_win = avg_win_pnl / equity
        avg_loss = avg_loss_pnl / equity
        return win_rate, max(avg_win, 1e-6), max(avg_loss, 1e-6)

    def _calc_shares_price(self, price: float) -> int:
        """基于动态 Kelly 公式计算买入份额"""
        try:
            equity = self._get_equity()
            win_rate, avg_win, avg_loss = self._calc_kelly_params()
            # Kelly 公式: f = (p*b - q) / b，其中 b = avg_win/avg_loss
            b = avg_win / avg_loss
            kelly = (win_rate * b - (1 - win_rate)) / b
            kelly = max(kelly, 0) * 0.5   # 半 Kelly
            # 再叠加仓位上限约束
            kelly = min(kelly, self.config.max_position_pct)
            shares = int(equity * kelly / price)
            shares = (shares // 100) * 100
            return max(shares, 0)
        except Exception:
            return 0

    def _calc_shares(self, sig: Signal) -> int:
        """兼容旧接口（转发给 _calc_shares_price）"""
        return self._calc_shares_price(sig.price)

    def _calc_shares_from_equity(self, price: float) -> int:
        shares = int(self._equity * 0.25 / price)
        return (shares // 100) * 100

    def _simulate_fill(self, direction: Literal['BUY', 'SELL'], price: float) -> float:
        """模拟成交价（滑点）"""
        slippage = self.config.slippage_bps / 10000
        if direction == 'BUY':
            return round(price * (1 + slippage), 2)
        else:
            return round(price * (1 - slippage), 2)

    def _execute_buy(self, symbol: str, price: float, shares: int, sig: Signal, dt: datetime):
        """执行买入"""
        value = price * shares
        commission = max(value * self.config.commission_rate, self.config.min_commission)
        total_cost = value + commission

        if total_cost > self._cash:
            shares = int((self._cash * 0.95) / (price * (1 + self.config.commission_rate)))
            shares = (shares // 100) * 100
            if shares < 100:
                return

        pos = self._positions.get(symbol) or PositionSnapshot(symbol=symbol)
        pos.shares += shares
        pos.avg_price = (pos.avg_price * (pos.shares - shares) + price * shares) / pos.shares
        pos.current_price = price
        pos.entry_high = max(pos.entry_high, price)
        pos.holding_secs = 0
        self._positions[symbol] = pos
        self._position_entries[symbol] = dt

        self._cash -= (price * shares + commission)
        self._update_equity()

        trade = TradeRecord(
            timestamp=dt,
            symbol=symbol,
            direction='BUY',
            price=price,
            shares=shares,
            value=price * shares,
            commission=commission,
            slippage_bps=self.config.slippage_bps,
            signal_reason=sig.factor_name,
            signal_strength=sig.strength,
            holding_period=0,
        )
        self._trades.append(trade)

    def _execute_sell(self, symbol: str, price: float, shares: int, sig: Signal, dt: datetime):
        """执行卖出（含 A 股印花税）"""
        pos = self._positions.get(symbol)
        if not pos or pos.shares == 0:
            return

        actual_shares = min(shares, pos.shares)
        value = price * actual_shares
        commission = max(value * self.config.commission_rate, self.config.min_commission)
        stamp_tax = value * self.config.stamp_tax_rate   # 卖出印花税（A 股 0.1%）
        total_fees = commission + stamp_tax
        pnl = (price - pos.avg_price) * actual_shares - total_fees

        self._cash += (value - commission - stamp_tax)
        pos.shares -= actual_shares
        # 平仓时计算实际持仓时长
        entry_time = self._position_entries.get(symbol, dt)
        holding = int((dt - entry_time).total_seconds()) if isinstance(entry_time, datetime) else 0

        if pos.shares == 0:
            del self._positions[symbol]
            self._position_entries.pop(symbol, None)
        else:
            pos.current_price = price

        self._update_equity()

        if pos.shares == 0:
            if pnl > 0:
                self._wins += 1
                self._total_profit += pnl
            else:
                self._losses += 1
                self._total_loss += abs(pnl)

            self._holding_periods.append(holding)

            # 回填平仓交易的盈亏
            trade = TradeRecord(
                timestamp=dt,
                symbol=symbol,
                direction='SELL',
                price=price,
                shares=actual_shares,
                value=value,
                commission=commission + stamp_tax,  # 含印花税
                slippage_bps=self.config.slippage_bps,
                signal_reason=sig.factor_name,
                signal_strength=sig.strength,
                holding_period=holding,
                pnl=pnl,
                realized_pnl=pnl,
            )
            self._trades.append(trade)

    def _get_equity(self) -> float:
        mv = sum(p.shares * p.current_price for p in self._positions.values() if p.shares > 0)
        return self._cash + mv

    def _update_equity(self):
        self._equity = self._get_equity()
        self._equity_curve.append(self._equity)

    def _update_daily(self, dt: datetime):
        """日终统计"""
        d = dt.date() if isinstance(dt, datetime) else dt
        mv = sum(p.shares * p.current_price for p in self._positions.values() if p.shares > 0)
        equity = self._cash + mv

        if len(self._equity_curve) > 1:
            prev_equity = self._equity_curve[-2]
            daily_return = (equity - prev_equity) / prev_equity if prev_equity else 0
        else:
            daily_return = 0

        stats = DailyStats(
            date=d,
            equity=equity,
            position_value=mv,
            cash=self._cash,
            daily_return=daily_return,
            daily_pnl=equity - self._equity_curve[0] if self._equity_curve else 0,
            n_trades=sum(1 for t in self._trades if t.timestamp.date() == d),
            n_positions=sum(1 for p in self._positions.values() if p.shares > 0),
        )
        self._daily_stats.append(stats)

    def _make_result(self) -> BacktestResult:
        """生成回测报告"""
        # equity_curve 和 daily_stats 可能长度不同（每日多次bar调用_update_daily）
        # 只取 daily_stats 有记录的日期对应的 equity
        if self._daily_stats and self._equity_curve:
            n = min(len(self._daily_stats), len(self._equity_curve))
            dates = [s.date for s in self._daily_stats[-n:]]
            eq_values = self._equity_curve[-n:]
            equity_series = pd.Series(eq_values, index=dates, name='equity')
        elif self._equity_curve:
            equity_series = pd.Series(self._equity_curve, name='equity')
        else:
            equity_series = pd.Series(name='equity')

        # 计算日收益
        if len(self._daily_stats) > 1:
            returns = pd.Series([s.daily_return for s in self._daily_stats[1:]])
        else:
            returns = pd.Series([0])

        total_return = (self._equity - self.config.initial_equity) / self.config.initial_equity
        annual_return = total_return / (len(self._daily_stats) / 252) if self._daily_stats else 0
        annual_vol = returns.std() * np.sqrt(252) if len(returns) > 1 else 0
        sharpe = (annual_return - self.config.risk_free_rate) / annual_vol if annual_vol > 0 else 0

        # 最大回撤
        cummax = equity_series.cummax()
        drawdown = (equity_series - cummax) / cummax
        max_dd = drawdown.min()
        max_dd_pct = abs(max_dd) if not pd.isna(max_dd) else 0

        # 卡玛
        calmar = annual_return / max_dd_pct if max_dd_pct > 0 else 0

        # 索提诺（下行波动）
        downside_returns = returns[returns < 0]
        downside_vol = downside_returns.std() * np.sqrt(252) if len(downside_returns) > 1 else 0
        sortino = (annual_return - self.config.risk_free_rate) / downside_vol if downside_vol > 0 else 0

        # 胜率
        win_rate = self._wins / (self._wins + self._losses) if (self._wins + self._losses) > 0 else 0
        profit_factor = self._total_profit / self._total_loss if self._total_loss > 0 else float('inf')

        # 平均持仓
        avg_holding = np.mean(self._holding_periods) if self._holding_periods else 0

        return BacktestResult(
            equity_curve=equity_series,
            daily_stats=self._daily_stats,
            trades=self._trades,
            positions=dict(self._positions),
            config=self.config,
            total_days=len(self._daily_stats),
            n_trades=len(self._trades),
            total_return=total_return,
            annual_return=annual_return,
            annual_vol=annual_vol,
            sharpe=sharpe,
            max_drawdown=equity_series.min(),
            max_drawdown_pct=max_dd_pct,
            win_rate=win_rate,
            profit_factor=profit_factor,
            avg_holding_period=avg_holding,
            calmar_ratio=calmar,
            sortino_ratio=sortino,
        )


# ─── PerformanceAnalyzer ──────────────────────────────────────────────────────

class PerformanceAnalyzer:
    """
    绩效分析器。
    在 BacktestResult 基础上计算更深入的归因指标。
    """

    @staticmethod
    def analyze(trades: List[TradeRecord], daily_stats: List[DailyStats]) -> Dict:
        """完整绩效分析"""
        if not daily_stats:
            return {}

        _ = pd.Series([s.equity for s in daily_stats])          # reserved for future use
        _ = pd.Series([s.daily_return for s in daily_stats])    # reserved for future use

        # 按信号来源分组
        by_signal = defaultdict(list)
        for t in trades:
            if t.pnl != 0:
                by_signal[t.signal_reason].append(t.pnl)

        signal_stats = {}
        for reason, pnls in by_signal.items():
            wins = sum(1 for p in pnls if p > 0)
            signal_stats[reason] = {
                'n_trades': len(pnls),
                'win_rate': wins / len(pnls) if pnls else 0,
                'total_pnl': sum(pnls),
                'avg_pnl': np.mean(pnls) if pnls else 0,
                'max_win': max(pnls) if pnls else 0,
                'max_loss': min(pnls) if pnls else 0,
            }

        # 亏损分层
        losses_only = [t for t in trades if t.pnl < 0]
        if losses_only:
            sorted_losses = sorted([abs(t.pnl) for t in losses_only], reverse=True)
            p95_loss = sorted_losses[int(len(sorted_losses) * 0.05)] if sorted_losses else 0
            p99_loss = sorted_losses[int(len(sorted_losses) * 0.01)] if sorted_losses else 0
        else:
            p95_loss = p99_loss = 0

        return {
            'signal_stats': signal_stats,
            'loss_percentile_95': p95_loss,
            'loss_percentile_99': p99_loss,
            'avg_slippage_bps': np.mean([t.slippage_bps for t in trades]) if trades else 0,
            'total_commission': sum(t.commission for t in trades),
        }
