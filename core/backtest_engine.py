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
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Callable, Any, Literal, Tuple
from collections import defaultdict
import numpy as np
import pandas as pd
import copy

from core.event_bus import EventBus, MarketEvent, SignalEvent, FillEvent
from core.factors.base import Factor, Signal
from core.oms import Order, Fill
from core.portfolio import PortfolioResult


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
    slippage_bps: float = 5.0          # 滑点 5bp
    risk_free_rate: float = 0.03       # 无风险利率
    allow_short: bool = False
    max_position_pct: float = 0.25    # 单标的最大仓位


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

    def load_data(self, symbol: str, df: pd.DataFrame) -> 'BacktestEngine':
        """
        加载 K 线数据。
        df 必须包含: open, high, low, close, volume 列。
        索引为 datetime。
        """
        required = {'open', 'high', 'low', 'close', 'volume'}
        missing = required - set(df.columns)
        if missing:
            raise ValueError(f"Missing columns: {missing}")
        self._data[symbol] = df.copy()
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

        for dt in all_dates:
            self._on_bar(dt)

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

    def _on_bar(self, dt: datetime):
        """处理每根 K 线"""
        for symbol, df in self._data.items():
            if dt not in df.index:
                continue

            bar = df.loc[dt]
            pos = self._positions.get(symbol)

            # 更新持仓当前价
            if pos and pos.shares > 0:
                pos.current_price = float(bar['close'])
                pos.unrealized_pnl = (pos.current_price - pos.avg_price) * pos.shares
                pos.unrealized_pnl_pct = (pos.current_price - pos.avg_price) / pos.avg_price if pos.avg_price else 0
                if pos.current_price > pos.entry_high:
                    pos.entry_high = pos.current_price
                pos.holding_secs += 1

            # 生成信号
            signals = self._generate_signals(symbol, df, dt, bar)

            for sig in signals:
                self._process_signal(sig, dt, bar)

        # 更新日终统计
        self._update_daily(dt)

    def _generate_signals(
        self,
        symbol: str,
        df: pd.DataFrame,
        dt: datetime,
        bar: pd.Series,
    ) -> List[Signal]:
        """用已发生的历史数据（含当前bar）计算因子信号"""
        signals = []

        # 取到当前时间点的所有历史数据
        hist = df.loc[:dt].tail(100)  # 最多100根

        for factor, threshold, params in self._strategies:
            try:
                fv = factor.evaluate(hist)
                if len(fv) == 0:
                    continue
                sigs = factor.signals(fv, price=float(bar['close']))
                signals.extend(sigs)
            except Exception:
                pass

        return signals

    def _process_signal(self, sig: Signal, dt: datetime, bar: pd.Series):
        """处理信号：风控检查 → 下单 → 成交"""
        sym = sig.symbol
        pos = self._positions.get(sym)

        if sig.direction == 'BUY':
            # 检查是否已有持仓
            if pos and pos.shares > 0:
                return  # 已有持仓，不加仓

            # 风控：仓位上限
            if not self._can_buy(sig.price, sig.metadata.get('shares')):
                return

            shares = sig.metadata.get('shares', self._calc_shares(sig))
            if shares <= 0:
                return

            fill_price = self._simulate_fill(sig.direction, sig.price)
            self._execute_buy(sym, fill_price, shares, sig, dt)

        elif sig.direction == 'SELL':
            if not pos or pos.shares == 0:
                return  # 无持仓

            fill_price = self._simulate_fill(sig.direction, sig.price)
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

    def _calc_shares(self, sig: Signal) -> int:
        """Kelly 半仓计算份额"""
        try:
            equity = self._get_equity()
            win_rate = 0.55
            avg_win = 0.02
            avg_loss = 0.01
            kelly = (win_rate * avg_win - (1 - win_rate) * avg_loss) / (avg_win * avg_loss)
            kelly = max(kelly, 0) * 0.5
            shares = int(equity * kelly / sig.price)
            shares = (shares // 100) * 100
            return max(shares, 0)
        except Exception:
            return 0

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
        """执行卖出"""
        pos = self._positions.get(symbol)
        if not pos or pos.shares == 0:
            return

        actual_shares = min(shares, pos.shares)
        value = price * actual_shares
        commission = max(value * self.config.commission_rate, self.config.min_commission)
        pnl = (price - pos.avg_price) * actual_shares - commission

        self._cash += (value - commission)
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
                commission=commission,
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
        closed_trades = [t for t in self._trades if t.realized_pnl != 0]
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

        equity = pd.Series([s.equity for s in daily_stats])
        returns = pd.Series([s.daily_return for s in daily_stats])

        # 按信号来源分组
        by_signal = defaultdict(list)
        for t in trades:
            if t.pnl != 0:
                by_signal[t.signal_reason].append(t.pnl)

        signal_stats = {}
        for reason, pnls in by_signal.items():
            wins = sum(1 for p in pnls if p > 0)
            losses = len(pnls) - wins
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
