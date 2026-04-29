"""
core/oms.py — Order Management System

BrokerAdapter 协议：所有券商（Paper / 富途 / 老虎 / IBKR）实现同一接口
OMS 类：PreTrade 风控 → 发送订单 → 记录成交

当前实现：
  - PaperBroker: 模拟撮合（复用现有逻辑）
  - OMS: 单例，管理订单路由
"""

from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Literal, Optional, Any, Callable, TYPE_CHECKING
import uuid
import os
import sys
import json
import urllib.request

if TYPE_CHECKING:
    from core.event_bus import EventBus, FillEvent

# ─── Order / Fill ─────────────────────────────────────────────────────────────

@dataclass
class Order:
    """标准订单格式"""
    order_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12].upper())
    symbol: str = ''
    direction: Literal['BUY', 'SELL'] = 'BUY'
    order_type: Literal['MARKET', 'LIMIT'] = 'MARKET'
    shares: int = 0
    price: float = 0          # LIMIT 价格，MARKET 时 = 0
    created_at: datetime = field(default_factory=datetime.now)
    status: Literal['PENDING', 'FILLED', 'CANCELLED', 'REJECTED'] = 'PENDING'
    fill_price: float = 0
    fill_time: Optional[datetime] = None

    def to_dict(self) -> Dict:
        return {
            'order_id': self.order_id,
            'symbol': self.symbol,
            'direction': self.direction,
            'order_type': self.order_type,
            'shares': self.shares,
            'price': self.price,
            'created_at': self.created_at.isoformat(),
            'status': self.status,
            'fill_price': self.fill_price,
        }


@dataclass
class Fill:
    """成交回报"""
    order_id: str = ''
    symbol: str = ''
    direction: Literal['BUY', 'SELL'] = 'BUY'
    shares: int = 0
    price: float = 0
    commission: float = 0
    slippage_bps: float = 0
    filled_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict:
        return {
            'order_id': self.order_id,
            'symbol': self.symbol,
            'direction': self.direction,
            'shares': self.shares,
            'price': self.price,
            'commission': round(self.commission, 4),
            'slippage_bps': round(self.slippage_bps, 1),
            'filled_at': self.filled_at.isoformat(),
        }


@dataclass
class Position:
    """持仓"""
    symbol: str
    shares: int = 0
    avg_price: float = 0
    current_price: float = 0
    unrealized_pnl: float = 0
    realized_pnl: float = 0

    @property
    def market_value(self) -> float:
        return self.shares * self.current_price


# ─── BrokerAdapter Protocol ───────────────────────────────────────────────────

class BrokerAdapter(ABC):
    """
    券商适配器接口。
    所有券商（Paper / Futu / Tiger / IBKR）实现此接口。
    """

    name: str = 'Broker'

    @abstractmethod
    def send(self, order: Order) -> Fill:
        """发送订单，返回成交回报"""
        ...

    @abstractmethod
    def cancel(self, order_id: str) -> bool:
        """取消订单"""
        ...

    @abstractmethod
    def quote(self, symbol: str) -> Dict[str, float]:
        """获取当前报价 {bid, ask, last}"""
        ...

    @abstractmethod
    def get_positions(self) -> List[Position]:
        """获取当前持仓"""
        ...

    def name(self) -> str:
        return self.__class__.__name__


# ─── PaperBroker ─────────────────────────────────────────────────────────────

class PaperBroker(BrokerAdapter):
    """
    模拟撮合 Paper Broker（复用现有 backend/services/broker.py 逻辑）。
    市价单：取腾讯实时报价 × 0.9995（买入）/ 1.0005（卖出）估算滑点
    涨停/跌停股无法成交
    """

    name = 'PaperBroker'

    def __init__(self):
        self._orders: Dict[str, Order] = {}
        self._positions: Dict[str, Position] = {}
        self._load_positions()

    def _load_positions(self):
        """从 Backend API 加载当前持仓"""
        try:
            import urllib.request, json as jsonlib
            base = 'http://127.0.0.1:5555'
            resp = urllib.request.urlopen(f'{base}/positions', timeout=5)
            data = jsonlib.loads(resp.read())
            for p in data.get('positions', []):
                self._positions[p['symbol']] = Position(
                    symbol=p['symbol'],
                    shares=int(p.get('shares', 0)),
                    avg_price=float(p.get('avg_price', 0)),
                    current_price=float(p.get('current_price', 0)),
                )
        except Exception as e:
            print(f"[PaperBroker] Failed to load positions: {e}")

    def send(self, order: Order) -> Fill:
        """模拟撮合"""
        self._orders[order.order_id] = order

        # 获取实时报价
        quote = self.quote(order.symbol)
        last = quote.get('last', 0)

        if last <= 0:
            order.status = 'REJECTED'
            return Fill(
                order_id=order.order_id,
                symbol=order.symbol,
                direction=order.direction,
                shares=order.shares,
                price=0,
                commission=0,
            )

        # 模拟滑点：买入稍贵（-0.05%），卖出稍便宜（+0.05%）
        slippage_bps = 5.0
        if order.direction == 'BUY':
            fill_price = round(last * 1.0005, 2)
        else:
            fill_price = round(last * 0.9995, 2)

        # 涨跌停检查
        limit_up = last * 1.10
        limit_down = last * 0.90
        if order.direction == 'BUY' and fill_price >= limit_up:
            order.status = 'REJECTED'
            return Fill(order_id=order.order_id, symbol=order.symbol,
                        direction=order.direction, shares=0, price=0)
        if order.direction == 'SELL' and fill_price <= limit_down:
            order.status = 'REJECTED'
            return Fill(order_id=order.order_id, symbol=order.symbol,
                        direction=order.direction, shares=0, price=0)

        commission = max(5.0, fill_price * order.shares * 0.0003)  # 佣金≥5元，万3

        order.status = 'FILLED'
        order.fill_price = fill_price
        order.fill_time = datetime.now()

        fill = Fill(
            order_id=order.order_id,
            symbol=order.symbol,
            direction=order.direction,
            shares=order.shares,
            price=fill_price,
            commission=commission,
            slippage_bps=slippage_bps,
        )

        # 更新模拟持仓
        self._update_position(fill)

        # 持久化到 Backend API
        self._persist_fill(fill)

        return fill

    def _update_position(self, fill: Fill):
        pos = self._positions.get(fill.symbol)
        if pos is None:
            pos = Position(symbol=fill.symbol)
            self._positions[fill.symbol] = pos

        if fill.direction == 'BUY':
            total_cost = pos.shares * pos.avg_price + fill.shares * fill.price
            pos.shares += fill.shares
            pos.avg_price = total_cost / pos.shares if pos.shares > 0 else 0
        else:
            realized = (fill.price - pos.avg_price) * fill.shares
            pos.realized_pnl += realized
            pos.shares -= fill.shares
            if pos.shares == 0:
                pos.avg_price = 0
                del self._positions[fill.symbol]

    def _persist_fill(self, fill: Fill):
        """持久化成交到 Backend API"""
        try:
            import urllib.request, json as jsonlib
            base = 'http://127.0.0.1:5555'
            payload = jsonlib.dumps({
                'symbol': fill.symbol,
                'direction': fill.direction,
                'shares': fill.shares,
                'price': fill.price,
                'commission': fill.commission,
                'slippage_bps': fill.slippage_bps,
                'order_id': fill.order_id,
            }).encode()
            req = urllib.request.Request(
                f'{base}/trades',
                data=payload,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=5) as r:
                jsonlib.loads(r.read())
        except Exception as e:
            print(f"[PaperBroker] persist_fill error: {e}")

    def cancel(self, order_id: str) -> bool:
        if order_id in self._orders:
            self._orders[order_id].status = 'CANCELLED'
            return True
        return False

    def quote(self, symbol: str) -> Dict[str, float]:
        """腾讯实时报价"""
        try:
            env = os.environ.copy()
            env.pop('HTTP_PROXY', None)
            env.pop('HTTPS_PROXY', None)
            sym = symbol.replace('.SH', '').replace('.SZ', '')
            url = f'https://qt.gtimg.cn/q={sym}'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=8) as r:
                raw = r.read().decode('gbk')
            fields = raw.split('~')
            if len(fields) > 5:
                return {
                    'last': float(fields[3]),
                    'open': float(fields[5]),
                    'high': float(fields[33]),
                    'low': float(fields[34]),
                    'volume': float(fields[6]),
                }
        except Exception as e:
            print(f"[PaperBroker] quote error for {symbol}: {e}")
        return {'last': 0}

    def get_positions(self) -> List[Position]:
        return list(self._positions.values())


# ─── OMS ─────────────────────────────────────────────────────────────────────

class OMS:
    """
    订单管理系统。
    PreTrade 风控 → 发送订单 → 记录成交 → 发射 FillEvent

    EventBus 集成：
      - 监听 SignalEvent
      - 发射 OrderEvent / FillEvent / RiskEvent
    """

    _instance: Optional['OMS'] = None

    def __new__(cls, broker: Optional[BrokerAdapter] = None):
        if cls._instance is not None:
            return cls._instance
        instance = super().__new__(cls)
        cls._instance = instance
        return instance

    def __init__(self, broker: Optional[BrokerAdapter] = None):
        if hasattr(self, '_initialized') and self._initialized:
            return
        self._initialized = True
        self.broker = broker or PaperBroker()
        self.bus: Optional['EventBus'] = None
        self._order_book: Dict[str, Order] = {}
        self._position_book: Dict[str, Position] = {}
        self._pending_signals: set = set()  # 防止重复下单
        self._load_positions_from_backend()

    def set_bus(self, bus: 'EventBus'):
        self.bus = bus
        # 注册信号监听
        from core.event_bus import SignalEvent
        bus.on('SignalEvent', self._on_signal)

    def _on_signal(self, event):
        """SignalEvent → 尝试下单"""
        signal = event.signal
        signal_key = f"{signal.symbol}:{signal.direction}:{signal.timestamp.isoformat()}"

        # 防重：同一标的同一方向 5 分钟内不重复
        if signal_key in self._pending_signals:
            return
        self._pending_signals.add(signal_key)

        import threading
        def clear():
            import time
            time.sleep(300)  # 5min
            self._pending_signals.discard(signal_key)

        threading.Thread(target=clear, daemon=True).start()

        try:
            fill = self.submit_from_signal(signal)
            if fill and fill.shares > 0:
                self._update_position_book(fill)  # 同步更新本地持仓快照
                # 写入合规审计日志
                try:
                    from core.audit_log import log_fill
                    log_fill(fill, signal=signal, risk_passed=True)
                except Exception:
                    pass
                if self.bus:
                    from core.event_bus import FillEvent
                    fe = FillEvent(
                        order_id=fill.order_id,
                        symbol=fill.symbol,
                        direction=fill.direction,
                        price=fill.price,
                        shares=fill.shares,
                        commission=fill.commission,
                    )
                    self.bus.emit(fe)
        except Exception as e:
            print(f"[OMS] submit error: {e}")
            if self.bus:
                from core.event_bus import RiskEvent
                self.bus.emit(RiskEvent(
                    level='CRITICAL',
                    symbol=signal.symbol,
                    reason=f'OMS submit failed: {e}',
                ))

    def submit_from_signal(self, signal, shares: Optional[int] = None) -> Optional[Fill]:
        """
        从 Signal 生成订单并提交。
        shares=None 时使用 signal.metadata 中的 shares 或 Kelly 计算。
        """
        # 份额决策
        if shares is None:
            shares = signal.metadata.get('shares')
            if shares is None:
                shares = self._kelly_shares(signal)
            if shares is None or shares <= 0:
                return None

        order = Order(
            symbol=signal.symbol,
            direction=signal.direction,
            order_type='MARKET',
            shares=shares,
            price=signal.price,
        )

        # PreTrade 风控
        result = self._pre_trade_check(order)
        if not result.passed:
            if self.bus:
                from core.event_bus import RiskEvent
                self.bus.emit(RiskEvent(
                    level='REJECT',
                    symbol=order.symbol,
                    reason=result.reason,
                ))
            return None

        fill = self.broker.send(order)
        self._order_book[order.order_id] = order
        return fill

    def _kelly_shares(self, signal) -> int:
        """Kelly 公式计算仓位份额（简化版）"""
        try:
            import urllib.request, json as jsonlib
            base = 'http://127.0.0.1:5555'
            with urllib.request.urlopen(f'{base}/portfolio/summary', timeout=5) as r:
                summary = jsonlib.loads(r.read())
            equity = summary.get('position_value', 0) + summary.get('cash', 0)
            win_rate = 0.55
            avg_win = 0.02   # 2% avg gain
            avg_loss = 0.01  # 1% avg loss
            kelly = (win_rate * avg_win - (1 - win_rate) * avg_loss) / (avg_win * avg_loss)
            kelly = max(kelly, 0) * 0.5  # 半 Kelly
            shares = int(equity * kelly / signal.price)
            shares = (shares // 100) * 100  # 整手
            return max(shares, 0)
        except Exception:
            return 0

    def _update_position_book(self, fill) -> None:
        """成交后更新本地持仓快照，供当次 run_once() 周期内的 PreTrade 检查使用。"""
        sym = fill.symbol
        pos = self._position_book.get(sym)
        if fill.direction == 'BUY':
            if pos:
                total_cost = pos.shares * pos.avg_price + fill.shares * fill.price
                pos.shares += fill.shares
                pos.avg_price = total_cost / pos.shares if pos.shares > 0 else 0
                pos.current_price = fill.price
            else:
                self._position_book[sym] = Position(
                    symbol=sym,
                    shares=fill.shares,
                    avg_price=fill.price,
                    current_price=fill.price,
                )
        elif fill.direction == 'SELL' and pos:
            pos.shares = max(0, pos.shares - fill.shares)
            if pos.shares == 0:
                del self._position_book[sym]

    def _load_positions_from_backend(self) -> None:
        """启动时从 Backend API 加载持仓快照，初始化 _position_book。"""
        try:
            import urllib.request, json as jsonlib
            base = 'http://127.0.0.1:5555'
            with urllib.request.urlopen(f'{base}/positions', timeout=5) as r:
                data = jsonlib.loads(r.read())
            for p in data.get('positions', []):
                sym = p.get('symbol', '')
                if not sym:
                    continue
                self._position_book[sym] = Position(
                    symbol=sym,
                    shares=int(p.get('shares', 0)),
                    avg_price=float(p.get('avg_price', 0)),
                    current_price=float(p.get('current_price', 0)),
                )
        except Exception as e:
            # Backend 未启动时静默跳过；_position_book 保持空字典
            import logging
            logging.getLogger('core.oms').debug('[OMS] position pre-load skipped: %s', e)

    @dataclass
    class PreTradeResult:
        passed: bool = True
        reason: str = ''

    def _pre_trade_check(self, order: Order) -> PreTradeResult:
        """PreTrade 风控检查"""
        # 1. 仓位上限 25%
        pos = self._position_book.get(order.symbol)
        if pos and pos.shares > 0:
            if order.direction == 'BUY':
                try:
                    import urllib.request, json as jsonlib
                    base = 'http://127.0.0.1:5555'
                    with urllib.request.urlopen(f'{base}/portfolio/summary', timeout=5) as r:
                        s = jsonlib.loads(r.read())
                    equity = s.get('position_value', 0) + s.get('cash', 0)
                    new_value = (pos.shares * pos.avg_price + order.shares * order.price)
                    if new_value / equity > 0.25:
                        return self.PreTradeResult(
                            passed=False,
                            reason=f'Position {order.symbol} exceeds 25% limit'
                        )
                except Exception:
                    pass

        # 2. 止损检查（已有持仓价格相比下跌超 5% → 禁止加仓）
        if pos and order.direction == 'BUY':
            if pos.avg_price > 0:
                loss_pct = (pos.avg_price - order.price) / pos.avg_price
                if loss_pct > 0.05:
                    return self.PreTradeResult(
                        passed=False,
                        reason=f'Existing position in loss {loss_pct*100:.1f}%, no adding'
                    )

        return self.PreTradeResult(passed=True)

    def submit_algo_order(
        self,
        algo: str,
        symbol: str,
        direction: str,
        total_shares: int,
        duration_minutes: int = 60,
        reference_price: float = 0.0,
        slice_interval: int = 5,
        volume_profile: Optional[List[float]] = None,
    ) -> 'AlgoOrderResult':
        """
        提交算法订单（VWAP / TWAP），返回模拟执行结果。

        此方法用于模拟仿真（SimulatedBroker）：
          - 生成子单列表
          - 用当前持仓价格模拟成交（均价成交，无滑点估算时使用 reference_price）
          - 统计市场冲击

        Parameters
        ----------
        algo : str
            算法类型：'VWAP' 或 'TWAP'
        symbol : str
            标的代码
        direction : str
            'BUY' 或 'SELL'
        total_shares : int
            目标总股数
        duration_minutes : int
            执行时长（分钟）
        reference_price : float
            参考价格（用于计算滑点）
        slice_interval : int
            子单间隔（分钟）
        volume_profile : List[float] or None
            成交量分布（VWAP 用，None 时自动使用默认 U 型分布）

        Returns
        -------
        AlgoOrderResult
        """
        from core.execution.vwap_executor import VWAPExecutor
        from core.execution.twap_executor import TWAPExecutor
        from core.execution.impact_estimator import ImpactEstimator

        algo_upper = algo.upper()
        if algo_upper == 'VWAP':
            executor = VWAPExecutor(
                symbol=symbol,
                direction=direction,
                total_shares=total_shares,
                duration_minutes=duration_minutes,
                reference_price=reference_price,
                slice_interval=slice_interval,
            )
        elif algo_upper == 'TWAP':
            executor = TWAPExecutor(
                symbol=symbol,
                direction=direction,
                total_shares=total_shares,
                duration_minutes=duration_minutes,
                reference_price=reference_price,
                slice_interval=slice_interval,
            )
        else:
            raise ValueError(f"Unknown algo: {algo}. Supported: VWAP, TWAP")

        slices = executor.generate_slices(volume_profile)

        # 简单模拟：所有子单均以 reference_price 成交
        import random
        total_filled = 0
        total_value = 0.0
        for sl in slices:
            price = reference_price * (1 + random.gauss(0, 0.0005))
            sl.filled_shares = sl.target_shares
            sl.fill_price = round(max(price, 0.01), 3)
            sl.status = 'FILLED'
            total_filled += sl.filled_shares
            total_value += sl.filled_shares * sl.fill_price

        avg_price = total_value / total_filled if total_filled > 0 else reference_price
        slippage_bps = (
            abs(avg_price - reference_price) / reference_price * 10_000
            if reference_price > 0 else 0.0
        )

        # 市场冲击估算（假设日均成交量 = total_shares / 0.05，即参与率约 5%）
        assumed_daily_vol = total_shares / 0.05
        impact_bps = ImpactEstimator.estimate(total_shares, assumed_daily_vol)

        from core.execution.algo_base import AlgoOrderResult
        result = AlgoOrderResult(
            order_id=executor.order_id,
            symbol=symbol,
            direction=direction,
            target_shares=total_shares,
            filled_shares=total_filled,
            avg_fill_price=round(avg_price, 3),
            slippage_bps=round(slippage_bps, 2),
            market_impact_bps=round(impact_bps, 2),
            n_slices=len(slices),
            duration_minutes=duration_minutes,
            slices=slices,
        )
        return result

    def cancel(self, order_id: str) -> bool:
        return self.broker.cancel(order_id)

    def get_pending_orders(self) -> List[Order]:
        return [o for o in self._order_book.values() if o.status == 'PENDING']

    def get_filled_orders(self) -> List[Order]:
        return [o for o in self._order_book.values() if o.status == 'FILLED']
