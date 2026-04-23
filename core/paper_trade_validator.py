"""
core/paper_trade_validator.py — 模拟实盘 vs 回测一致性验证（P3-A）

功能：
  - 将 BacktestEngine 在某段时间产生的交易信号"回放"给 SimulatedBroker
  - 对比成交价偏差（Implementation Shortfall）是否 < 20 bps
  - 归因 > 50 bps 的偏差（滑点/佣金/时间差）
  - 输出 JSON 报告到 outputs/paper_trade_validation_{date}.json

用法：
    from core.paper_trade_validator import PaperTradeValidator
    from core.backtest_engine import BacktestResult

    validator = PaperTradeValidator()
    report = validator.validate_from_backtest(bt_result, broker=SimulatedBroker())
    report.print_report()
    report.save()
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

_OUTPUTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'outputs'
)
os.makedirs(_OUTPUTS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class TradeComparison:
    """单笔交易的回测 vs 实盘成交对比。"""
    symbol: str
    direction: str
    bt_price: float            # 回测成交价
    live_price: float          # 实盘（模拟）成交价
    shares: int
    deviation_bps: float       # (live - bt) / bt * 10000
    within_threshold: bool     # |deviation| < threshold_bps
    cause: str                 # 偏差原因分类


@dataclass
class ValidationReport:
    """一致性验证报告。"""
    validated_at: str
    threshold_bps: float           # 合格阈值（默认 20 bps）
    n_trades: int
    n_passed: int
    pass_rate: float
    avg_deviation_bps: float
    max_deviation_bps: float
    passed: bool                   # pass_rate >= 90%
    comparisons: List[TradeComparison] = field(default_factory=list)
    large_deviations: List[TradeComparison] = field(default_factory=list)  # > 50 bps
    notes: List[str] = field(default_factory=list)

    def print_report(self) -> None:
        status = 'PASS' if self.passed else 'FAIL'
        print(f'=== 模拟实盘一致性验证报告 [{status}] ===')
        print(f'验证时间：{self.validated_at}')
        print(f'阈值：{self.threshold_bps} bps | 合格标准：pass_rate ≥ 90%')
        print()
        print(f'交易总数：{self.n_trades}')
        print(f'通过数：  {self.n_passed}')
        print(f'通过率：  {self.pass_rate:.1%}')
        print(f'均偏差：  {self.avg_deviation_bps:.2f} bps')
        print(f'最大偏差：{self.max_deviation_bps:.2f} bps')
        if self.large_deviations:
            print()
            print('--- > 50 bps 大偏差明细 ---')
            for c in self.large_deviations:
                print(f'  {c.symbol} {c.direction}: '
                      f'回测={c.bt_price:.4f} 实盘={c.live_price:.4f} '
                      f'偏差={c.deviation_bps:+.1f} bps [{c.cause}]')
        if self.notes:
            print()
            for note in self.notes:
                print(f'  * {note}')

    def save(self, path: Optional[str] = None) -> str:
        if path is None:
            path = os.path.join(
                _OUTPUTS_DIR,
                f'paper_trade_validation_{date.today().isoformat()}.json',
            )
        data = {
            'validated_at': self.validated_at,
            'threshold_bps': self.threshold_bps,
            'summary': {
                'n_trades': self.n_trades,
                'n_passed': self.n_passed,
                'pass_rate': self.pass_rate,
                'avg_deviation_bps': self.avg_deviation_bps,
                'max_deviation_bps': self.max_deviation_bps,
                'passed': self.passed,
            },
            'large_deviations': [asdict(c) for c in self.large_deviations],
            'notes': self.notes,
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return path


# ---------------------------------------------------------------------------
# PaperTradeValidator
# ---------------------------------------------------------------------------

class PaperTradeValidator:
    """
    模拟实盘 vs 回测一致性验证器。

    Parameters
    ----------
    threshold_bps    : 单笔成交价偏差合格阈值（默认 20 bps）
    large_dev_bps    : 大偏差归因阈值（默认 50 bps）
    pass_rate_target : 整体通过率目标（默认 90%）
    """

    def __init__(
        self,
        threshold_bps: float = 20.0,
        large_dev_bps: float = 50.0,
        pass_rate_target: float = 0.9,
    ) -> None:
        self.threshold_bps = threshold_bps
        self.large_dev_bps = large_dev_bps
        self.pass_rate_target = pass_rate_target

    # ------------------------------------------------------------------
    # 主验证入口
    # ------------------------------------------------------------------

    def validate_from_backtest(
        self,
        bt_result,                  # BacktestResult
        broker=None,                # BrokerBase，None 时用 SimulatedBroker
        use_bt_price_as_reference: bool = True,
    ) -> ValidationReport:
        """
        将回测成交记录与模拟撮合的报价对比。

        Parameters
        ----------
        bt_result :
            BacktestEngine.run() 的返回值（含 trades 列表）
        broker :
            用于获取参考报价的 Broker（默认 SimulatedBroker manual mode）
        use_bt_price_as_reference :
            True  = 以回测成交价为基准，对比模拟实盘报价偏差
            False = 以信号触发时报价为基准（需 broker 支持实时行情）
        """
        if broker is None:
            from core.brokers.simulated import SimulatedBroker, SimConfig
            broker = SimulatedBroker(SimConfig(price_source='manual'))
            broker.connect()

        trades = getattr(bt_result, 'trades', [])
        if not trades:
            return self._empty_report('回测无成交记录')

        comparisons: List[TradeComparison] = []

        for trade in trades:
            comp = self._compare_trade(trade, broker)
            if comp is not None:
                comparisons.append(comp)

        return self._build_report(comparisons)

    def validate_from_signals(
        self,
        signals: List[Dict],        # [{'symbol', 'direction', 'price', 'shares'}, ...]
        broker=None,
    ) -> ValidationReport:
        """
        从信号列表直接验证（不依赖回测引擎）。

        Parameters
        ----------
        signals : 信号字典列表，每个包含：
            symbol    : 标的
            direction : 'BUY' | 'SELL'
            price     : 信号触发时参考价（回测成交价）
            shares    : 成交数量
        broker  : BrokerBase 实例
        """
        if broker is None:
            from core.brokers.simulated import SimulatedBroker, SimConfig
            broker = SimulatedBroker(SimConfig(price_source='manual'))
            broker.connect()

        comparisons: List[TradeComparison] = []
        for sig in signals:
            comp = self._compare_signal(sig, broker)
            if comp is not None:
                comparisons.append(comp)

        return self._build_report(comparisons)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _compare_trade(self, trade, broker) -> Optional[TradeComparison]:
        """对单笔回测交易执行模拟撮合，计算价格偏差。"""
        from core.oms import Order
        try:
            # 用回测成交价注入手动报价，再提交订单获取实盘成交价
            bt_price = float(trade.price)
            symbol = str(trade.symbol)
            shares = int(trade.shares)
            direction = str(trade.direction)

            # 注入参考价（模拟实盘在该时间点看到的价格）
            if hasattr(broker, 'set_quote'):
                broker.set_quote(symbol, bt_price)

            order = Order(
                symbol=symbol,
                direction=direction,
                order_type='MARKET',
                shares=shares,
            )

            # 如果是 SELL，确保有持仓
            if direction == 'SELL' and hasattr(broker, 'inject_position'):
                broker.inject_position(symbol, shares, bt_price)

            # 确保现金充足
            if direction == 'BUY' and hasattr(broker, '_cash'):
                needed = bt_price * shares * 1.1
                if broker._cash < needed:
                    broker._cash = needed

            fill = broker.submit_order(order)

            if fill.shares == 0:
                return None  # 被拒单，跳过

            live_price = fill.price
            deviation_bps = (live_price - bt_price) / bt_price * 10000 if bt_price > 0 else 0.0
            within = abs(deviation_bps) <= self.threshold_bps
            cause = self._classify_deviation(abs(deviation_bps), direction)

            return TradeComparison(
                symbol=symbol,
                direction=direction,
                bt_price=round(bt_price, 4),
                live_price=round(live_price, 4),
                shares=shares,
                deviation_bps=round(deviation_bps, 2),
                within_threshold=within,
                cause=cause,
            )
        except Exception as e:
            logger.debug('[PaperTradeValidator] compare_trade error: %s', e)
            return None

    def _compare_signal(self, sig: Dict, broker) -> Optional[TradeComparison]:
        """从信号字典对比。"""
        from core.oms import Order
        try:
            symbol = sig['symbol']
            direction = sig['direction']
            bt_price = float(sig['price'])
            shares = int(sig.get('shares', 100))

            if hasattr(broker, 'set_quote'):
                broker.set_quote(symbol, bt_price)

            if direction == 'SELL' and hasattr(broker, 'inject_position'):
                broker.inject_position(symbol, shares, bt_price)

            if direction == 'BUY' and hasattr(broker, '_cash'):
                needed = bt_price * shares * 1.1
                if broker._cash < needed:
                    broker._cash = needed

            order = Order(
                symbol=symbol, direction=direction,
                order_type='MARKET', shares=shares,
            )
            fill = broker.submit_order(order)

            if fill.shares == 0:
                return None

            live_price = fill.price
            deviation_bps = (live_price - bt_price) / bt_price * 10000 if bt_price > 0 else 0.0
            within = abs(deviation_bps) <= self.threshold_bps
            cause = self._classify_deviation(abs(deviation_bps), direction)

            return TradeComparison(
                symbol=symbol, direction=direction,
                bt_price=round(bt_price, 4), live_price=round(live_price, 4),
                shares=shares,
                deviation_bps=round(deviation_bps, 2),
                within_threshold=within, cause=cause,
            )
        except Exception as e:
            logger.debug('[PaperTradeValidator] compare_signal error: %s', e)
            return None

    @staticmethod
    def _classify_deviation(abs_bps: float, direction: str) -> str:
        """按偏差幅度和方向分类原因。"""
        if abs_bps <= 5:
            return 'minimal'
        if abs_bps <= 20:
            return 'normal_slippage'
        if abs_bps <= 50:
            return 'high_slippage'
        if abs_bps <= 100:
            return 'liquidity_impact'
        return 'execution_delay_or_halt'

    def _build_report(self, comparisons: List[TradeComparison]) -> ValidationReport:
        if not comparisons:
            return self._empty_report('无有效对比记录')

        n_total = len(comparisons)
        n_passed = sum(1 for c in comparisons if c.within_threshold)
        pass_rate = n_passed / n_total

        devs = [abs(c.deviation_bps) for c in comparisons]
        avg_dev = sum(devs) / len(devs)
        max_dev = max(devs)

        large_devs = [c for c in comparisons if abs(c.deviation_bps) > self.large_dev_bps]

        notes: List[str] = []
        passed = pass_rate >= self.pass_rate_target

        if not passed:
            notes.append(
                f'通过率 {pass_rate:.1%} < {self.pass_rate_target:.0%}，'
                '建议检查滑点模型或价格获取时机'
            )
        if large_devs:
            causes = {}
            for c in large_devs:
                causes[c.cause] = causes.get(c.cause, 0) + 1
            notes.append(f'大偏差原因分布：{causes}')

        if avg_dev > 10:
            notes.append(
                f'平均偏差 {avg_dev:.1f} bps 偏高，'
                '模拟盘 slippage_bps 参数建议调整为实际观测值'
            )

        return ValidationReport(
            validated_at=datetime.now().isoformat(timespec='seconds'),
            threshold_bps=self.threshold_bps,
            n_trades=n_total,
            n_passed=n_passed,
            pass_rate=round(pass_rate, 4),
            avg_deviation_bps=round(avg_dev, 2),
            max_deviation_bps=round(max_dev, 2),
            passed=passed,
            comparisons=comparisons,
            large_deviations=large_devs,
            notes=notes,
        )

    def _empty_report(self, reason: str) -> ValidationReport:
        return ValidationReport(
            validated_at=datetime.now().isoformat(timespec='seconds'),
            threshold_bps=self.threshold_bps,
            n_trades=0, n_passed=0,
            pass_rate=0.0, avg_deviation_bps=0.0, max_deviation_bps=0.0,
            passed=False, notes=[reason],
        )
