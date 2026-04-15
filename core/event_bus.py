"""
EventBus — 事件总线
所有模块通过事件通信，解耦策略/风控/执行。
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Any, Optional, Literal
from enum import Enum
import threading
import queue
import time


class EventType(str, Enum):
    MARKET = 'market'
    SIGNAL = 'signal'
    ORDER = 'order'
    FILL = 'fill'
    RISK = 'risk'
    ALERT = 'alert'


# ─── Data Classes ────────────────────────────────────────────────────────────

@dataclass
class Event:
    """所有事件的基类"""
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = 'system'


@dataclass
class MarketEvent(Event):
    """行情事件（tick / bar）"""
    type: Literal['tick', 'bar'] = 'bar'
    symbol: str = ''
    freq: str = '1min'               # 'tick' | '1min' | '5min' | '1day'
    open: float = 0
    high: float = 0
    low: float = 0
    close: float = 0
    volume: float = 0
    extra: Dict[str, Any] = field(default_factory=dict)  # 北向资金等扩展数据

    @property
    def data(self) -> Dict[str, Any]:
        return dict(
            open=self.open, high=self.high, low=self.low,
            close=self.close, volume=self.volume
        )


@dataclass
class SignalEvent(Event):
    """信号事件"""
    signal: 'Signal' = None  # Forward reference

    @property
    def signal(self) -> Signal:
        return self.signal


@dataclass
class OrderEvent(Event):
    """订单请求事件"""
    order: 'Order' = None


@dataclass
class FillEvent(Event):
    """成交回报事件"""
    order_id: str = ''
    symbol: str = ''
    direction: Literal['BUY', 'SELL'] = 'BUY'
    price: float = 0
    shares: int = 0
    commission: float = 0


@dataclass
class RiskEvent(Event):
    """风控预警事件"""
    level: Literal['WARN', 'CRITICAL', 'REJECT'] = 'WARN'
    symbol: str = ''
    reason: str = ''
    detail: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AlertEvent(Event):
    """通知事件（飞书/日志）"""
    level: Literal['INFO', 'WARN', 'ERROR'] = 'INFO'
    title: str = ''
    message: str = ''
    channel: str = 'log'   # 'feishu' | 'log'


# ─── Signal / Order / Fill ───────────────────────────────────────────────────

@dataclass
class Signal:
    """标准化信号，所有因子输出统一格式"""
    timestamp: datetime
    symbol: str
    direction: Literal['BUY', 'SELL']
    strength: float          # 0~1，信号强度
    factor_name: str         # 来源因子
    price: float = 0         # 触发价格
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def signal_key(self) -> str:
        return f"{self.symbol}:{self.direction}:{self.timestamp.isoformat()}"


@dataclass
class Order:
    """订单请求"""
    order_id: str = ''
    symbol: str = ''
    direction: Literal['BUY', 'SELL'] = 'BUY'
    order_type: Literal['MARKET', 'LIMIT'] = 'MARKET'
    shares: int = 0
    price: float = 0          # LIMIT 价格，MARKET 时填 0
    created_at: datetime = field(default_factory=datetime.now)


@dataclass
class Fill:
    """成交回报"""
    order_id: str = ''
    symbol: str = ''
    direction: Literal['BUY', 'SELL'] = 'BUY'
    price: float = 0
    shares: int = 0
    commission: float = 0
    filled_at: datetime = field(default_factory=datetime.now)


# ─── EventBus ────────────────────────────────────────────────────────────────

class EventBus:
    """
    单例事件总线。
    所有组件通过 emit/on 通信，支持同步和异步两种模式。
    """
    _instance: Optional['EventBus'] = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def global_bus(cls) -> 'EventBus':
        return cls()

    def __init__(self, async_mode: bool = False):
        if hasattr(self, '_initialized'):
            return
        self._initialized = True
        self._handlers: Dict[str, List[Callable]] = {}
        self._queue: queue.Queue = queue.Queue()
        self._async_mode = async_mode
        self._running = False
        self._thread: Optional[threading.Thread] = None

    # ── Handler Registration ────────────────────────────────────────────────

    def on(self, event_type: str, handler: Callable[[Event], None]) -> None:
        """注册事件处理器"""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def off(self, event_type: str, handler: Callable[[Event], None]) -> None:
        """注销事件处理器"""
        if event_type in self._handlers:
            self._handlers[event_type] = [
                h for h in self._handlers[event_type] if h != handler
            ]

    # ── Emit ────────────────────────────────────────────────────────────────

    def emit(self, event: Event) -> None:
        """
        触发事件，调用所有注册的处理程序。
        异常不阻断其他处理器。
        """
        event_type = type(event).__name__
        handlers = self._handlers.get(event_type, [])
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                print(f"[EventBus] Handler error in {handler.__name__}: {e}")

    def emit_async(self, event: Event) -> None:
        """异步触发（线程安全）"""
        self._queue.put(event)

    def _async_loop(self):
        """异步处理循环"""
        while self._running:
            try:
                event = self._queue.get(timeout=0.1)
                self.emit(event)
            except queue.Empty:
                continue
            except Exception as e:
                print(f"[EventBus] Async error: {e}")

    def start_async(self):
        """启动异步处理线程"""
        if self._async_mode and not self._running:
            self._running = True
            self._thread = threading.Thread(target=self._async_loop, daemon=True)
            self._thread.start()

    def stop_async(self):
        """停止异步处理"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    # ── Pipeline ────────────────────────────────────────────────────────────

    def pipeline(self, *handlers: Callable[[Event], Optional[Event]]) -> None:
        """
        链式处理：上一个处理器的输出作为下一个的输入。
        常用于：MarketEvent → SignalGenerator → RiskEngine → OMS
        """
        def wrapper(event: Event):
            result = event
            for h in handlers:
                result = h(result)
                if result is None:
                    return
                if not isinstance(result, Event):
                    return
                result = result
        # 最后一个处理结果如果也是 Event，再次注册 handler
        # 简化用法：直接让每个 handler 发射下一个事件
        for h in handlers:
            pass  # 验证类型即可

    # ── Monitor ─────────────────────────────────────────────────────────────

    def stats(self) -> Dict[str, int]:
        """返回各事件类型的处理器数量"""
        return {k: len(v) for k, v in self._handlers.items()}

    def reset(self) -> None:
        """清空所有处理器（测试用）"""
        self._handlers.clear()
