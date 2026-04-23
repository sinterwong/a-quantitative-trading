"""
core/brokers/base.py — 统一券商接口（BrokerBase）

设计原则：
  - 所有券商（SimulatedBroker / Futu / Tiger / IBKR / ...）实现同一接口
  - 新增券商只需继承 BrokerBase 并实现全部 abstract 方法
  - SafetyMode 由 BrokerFactory 在外层控制，Broker 本身不做安全检查
  - 支持 A股 / 港股 / 美股 / 期货，通过 supported_markets() 声明

接口层次：
  BrokerAdapter (core/oms.py)   — 最小接口（OMS 使用）
      └── BrokerBase            — 完整接口（新券商基类）
              ├── SimulatedBroker  — 模拟撮合（无须网络）
              ├── FutuBroker       — 富途（stub）
              ├── TigerBroker      — 老虎（stub）
              └── IBBroker         — IBKR（stub）

实现新券商示例：
    from core.brokers.base import BrokerBase, AccountInfo, OrderStatus
    from core.oms import Order, Fill, Position

    class MyBroker(BrokerBase):
        name = 'MyBroker'

        def connect(self) -> bool:
            # 建立连接，返回是否成功
            ...

        def disconnect(self) -> None:
            ...

        def get_account(self) -> AccountInfo:
            ...

        def get_cash(self) -> float:
            ...

        def get_positions(self) -> List[Position]:
            ...

        def get_quote(self, symbol: str) -> QuoteData:
            ...

        def submit_order(self, order: Order) -> Fill:
            ...

        def cancel_order(self, order_id: str) -> bool:
            ...

        def get_orders(self, status: Optional[str] = None) -> List[Order]:
            ...

        def get_fills(self, since: Optional[datetime] = None) -> List[Fill]:
            ...
"""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Literal, Optional, Set

from core.oms import BrokerAdapter, Fill, Order, Position


# ---------------------------------------------------------------------------
# 市场类型
# ---------------------------------------------------------------------------

class MarketType(str, Enum):
    A_SHARE   = 'a_share'    # 沪深 A 股
    HK_STOCK  = 'hk_stock'   # 港股
    US_STOCK  = 'us_stock'   # 美股
    FUTURES   = 'futures'    # 期货
    OPTIONS   = 'options'    # 期权
    FOREX     = 'forex'      # 外汇


# ---------------------------------------------------------------------------
# 账户信息
# ---------------------------------------------------------------------------

@dataclass
class AccountInfo:
    """券商账户摘要。"""
    account_id: str
    broker_name: str
    currency: str = 'CNY'
    total_assets: float = 0.0       # 总资产
    net_assets: float = 0.0         # 净资产
    cash: float = 0.0               # 可用现金
    frozen_cash: float = 0.0        # 冻结资金（挂单中）
    market_value: float = 0.0       # 持仓市值
    unrealized_pnl: float = 0.0     # 浮动盈亏
    realized_pnl_today: float = 0.0 # 今日已实现盈亏
    margin_ratio: float = 0.0       # 保证金占用比（期货用）
    fetched_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict:
        return {
            'account_id': self.account_id,
            'broker_name': self.broker_name,
            'currency': self.currency,
            'total_assets': self.total_assets,
            'net_assets': self.net_assets,
            'cash': self.cash,
            'frozen_cash': self.frozen_cash,
            'market_value': self.market_value,
            'unrealized_pnl': self.unrealized_pnl,
            'realized_pnl_today': self.realized_pnl_today,
            'fetched_at': self.fetched_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# 实时报价
# ---------------------------------------------------------------------------

@dataclass
class QuoteData:
    """实时报价快照。"""
    symbol: str
    last: float = 0.0           # 最新价
    bid: float = 0.0            # 买一价
    ask: float = 0.0            # 卖一价
    bid_size: int = 0           # 买一量（手）
    ask_size: int = 0           # 卖一量（手）
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    prev_close: float = 0.0
    volume: int = 0             # 总成交量（手）
    amount: float = 0.0         # 总成交额（元）
    change_pct: float = 0.0     # 涨跌幅 %
    is_tradable: bool = True    # 是否可交易（未停牌/涨跌停）
    fetched_at: datetime = field(default_factory=datetime.now)

    @property
    def spread(self) -> float:
        return self.ask - self.bid if self.ask > 0 and self.bid > 0 else 0.0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2 if self.bid > 0 and self.ask > 0 else self.last

    def to_dict(self) -> Dict:
        return {
            'symbol': self.symbol,
            'last': self.last,
            'bid': self.bid,
            'ask': self.ask,
            'change_pct': self.change_pct,
            'is_tradable': self.is_tradable,
        }


# ---------------------------------------------------------------------------
# 订单状态（扩展版）
# ---------------------------------------------------------------------------

class OrderStatus(str, Enum):
    PENDING           = 'PENDING'            # 已提交，等待撮合
    PARTIALLY_FILLED  = 'PARTIALLY_FILLED'   # 部分成交
    FILLED            = 'FILLED'             # 全部成交
    CANCELLED         = 'CANCELLED'          # 已撤销
    REJECTED          = 'REJECTED'           # 被拒绝（风控/规则）
    EXPIRED           = 'EXPIRED'            # 已过期（当日有效单）


# ---------------------------------------------------------------------------
# BrokerBase — 统一接口
# ---------------------------------------------------------------------------

class BrokerBase(BrokerAdapter):
    """
    统一券商接口基类。

    继承 BrokerAdapter（保持 OMS 兼容），扩展完整接口：
      - 连接管理：connect / disconnect / is_connected
      - 账户信息：get_account / get_cash
      - 持仓查询：get_positions
      - 行情数据：get_quote / is_market_open
      - 订单管理：submit_order / cancel_order / get_orders / get_fills
      - 市场能力：supported_markets

    实现新券商时，**所有 abstract 方法都必须实现**。
    非 abstract 的方法提供默认实现，可选择性覆盖。
    """

    name: str = 'BrokerBase'

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    @abstractmethod
    def connect(self) -> bool:
        """
        建立与券商的连接。

        Returns
        -------
        True = 连接成功，False = 连接失败（可重试）

        实现说明：
          - 认证（API key / OAuth token / OpenD）
          - 建立 TCP/WebSocket 连接
          - 订阅账户推送（如支持）
          - 失败时 NOT 抛出异常，返回 False 并记录日志
        """
        ...

    @abstractmethod
    def disconnect(self) -> None:
        """
        断开连接，释放资源。

        实现说明：
          - 取消所有订阅
          - 关闭 TCP/WebSocket
          - 清理内部状态
          - 不应抛出异常（即使连接已断开）
        """
        ...

    @abstractmethod
    def is_connected(self) -> bool:
        """当前是否处于已连接状态。"""
        ...

    # ------------------------------------------------------------------
    # 账户信息
    # ------------------------------------------------------------------

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """
        获取账户摘要（总资产 / 净资产 / 现金 / 持仓市值）。

        实现说明：
          - 应从券商 API 实时获取（不缓存超过 30 秒）
          - 连接断开时返回带 0 值的 AccountInfo，不抛出异常
        """
        ...

    @abstractmethod
    def get_cash(self) -> float:
        """
        获取可用现金（CNY）。

        实现说明：
          - 返回扣除冻结资金后的可用余额
          - 可直接调用 get_account().cash 实现
        """
        ...

    # ------------------------------------------------------------------
    # 持仓
    # ------------------------------------------------------------------

    @abstractmethod
    def get_positions(self) -> List[Position]:
        """
        获取当前全部持仓。

        Returns
        -------
        Position 列表，空持仓时返回 []

        实现说明：
          - 包含所有持有份额 > 0 的标的
          - Position.current_price 应为实时价格（或最近可获取价格）
        """
        ...

    def get_position(self, symbol: str) -> Optional[Position]:
        """获取单个标的持仓（默认实现：遍历 get_positions()）。"""
        for p in self.get_positions():
            if p.symbol == symbol:
                return p
        return None

    # ------------------------------------------------------------------
    # 行情
    # ------------------------------------------------------------------

    @abstractmethod
    def get_quote(self, symbol: str) -> QuoteData:
        """
        获取单标的实时报价。

        实现说明：
          - 应返回买一/卖一价以及最新价
          - 行情获取失败时返回 last=0 的 QuoteData，不抛异常
          - 可缓存 1~3 秒避免频繁请求

        对应旧接口 quote() 的替代：
          旧: quote(symbol) -> Dict[str, float]
          新: get_quote(symbol) -> QuoteData
        """
        ...

    @abstractmethod
    def is_market_open(self, market: MarketType = MarketType.A_SHARE) -> bool:
        """
        判断指定市场当前是否处于交易时段。

        实现说明：
          A股：9:30-11:30，13:00-15:00，周一至周五（法定节假日除外）
          港股：9:30-12:00，13:00-16:00
          美股：9:30-16:00 ET
          期货：视品种（商品/金融/夜盘）
        """
        ...

    # ------------------------------------------------------------------
    # 订单操作
    # ------------------------------------------------------------------

    @abstractmethod
    def submit_order(self, order: Order) -> Fill:
        """
        提交订单并返回成交回报。

        实现说明：
          - 市价单（MARKET）：以当前最优价立即撮合
          - 限价单（LIMIT）：挂单等待，立即返回 PENDING Fill
          - 成交失败时返回 shares=0 的 Fill（status=REJECTED），不抛异常
          - 必须记录到内部 order book

        A股合规要求：
          - 最小下单单位：100 股（1 手）
          - 涨停板不可买入，跌停板不可卖出
          - 卖出时扣除印花税 0.1%（仅卖出方向）
        """
        ...

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """
        撤销订单。

        Returns
        -------
        True = 撤销成功，False = 订单不存在或已成交

        实现说明：
          - 已 FILLED 的订单无法撤销，返回 False
          - 部分成交（PARTIALLY_FILLED）的订单撤销后，已成交部分保留
        """
        ...

    @abstractmethod
    def get_orders(
        self,
        status: Optional[str] = None,
        since: Optional[datetime] = None,
    ) -> List[Order]:
        """
        查询订单列表。

        Parameters
        ----------
        status : 过滤状态（'PENDING' / 'FILLED' / 'CANCELLED' 等），None = 全部
        since  : 时间过滤（仅返回此时间之后的订单），None = 全部

        实现说明：
          - 应同时覆盖当日和历史订单
          - 轻量券商 API 可只实现当日订单
        """
        ...

    @abstractmethod
    def get_fills(self, since: Optional[datetime] = None) -> List[Fill]:
        """
        查询成交记录。

        Parameters
        ----------
        since : 时间过滤，None = 今日全部

        实现说明：
          - 可用于对账和 TCA 分析
          - 返回时间倒序（最新在前）
        """
        ...

    # ------------------------------------------------------------------
    # 能力声明
    # ------------------------------------------------------------------

    def supported_markets(self) -> Set[MarketType]:
        """
        声明此券商支持的市场类型（默认：A股）。

        覆盖此方法以声明更多市场：
            def supported_markets(self):
                return {MarketType.HK_STOCK, MarketType.US_STOCK}
        """
        return {MarketType.A_SHARE}

    def supports(self, market: MarketType) -> bool:
        """是否支持指定市场。"""
        return market in self.supported_markets()

    # ------------------------------------------------------------------
    # 兼容旧 BrokerAdapter 接口
    # ------------------------------------------------------------------

    def send(self, order: Order) -> Fill:
        """
        向后兼容 BrokerAdapter.send()。
        新代码请使用 submit_order()。
        """
        return self.submit_order(order)

    def cancel(self, order_id: str) -> bool:
        """向后兼容 BrokerAdapter.cancel()。"""
        return self.cancel_order(order_id)

    def quote(self, symbol: str) -> Dict:
        """向后兼容 BrokerAdapter.quote()，返回旧式字典。"""
        q = self.get_quote(symbol)
        return {
            'last': q.last,
            'bid': q.bid,
            'ask': q.ask,
            'open': q.open,
            'high': q.high,
            'low': q.low,
            'volume': q.volume,
            'change_pct': q.change_pct,
        }

    # ------------------------------------------------------------------
    # 便捷方法（基于 abstract 方法的组合，子类无需覆盖）
    # ------------------------------------------------------------------

    def get_pending_orders(self) -> List[Order]:
        """获取所有挂单中订单。"""
        return self.get_orders(status='PENDING')

    def get_filled_orders(self, since: Optional[datetime] = None) -> List[Order]:
        """获取所有成交订单。"""
        return self.get_orders(status='FILLED', since=since)

    def total_position_value(self) -> float:
        """所有持仓的总市值。"""
        return sum(p.shares * p.current_price for p in self.get_positions())

    def __repr__(self) -> str:
        connected = 'connected' if self.is_connected() else 'disconnected'
        markets = ','.join(m.value for m in self.supported_markets())
        return f'<{self.name} [{connected}] markets=[{markets}]>'
