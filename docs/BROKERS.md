# 券商

本系统不接入真实券商，所有"下单"都是虚拟模拟盘记账。

## 可用 broker

| 类 | 路径 | 用途 |
|---|---|---|
| `PaperBroker`（生产 HTTP） | `backend.services.broker.PaperBroker` | 同步直写 `state.db`，API 端点链路使用 |
| `SimulatedBroker` | `core.brokers.simulated.SimulatedBroker` | 同步语义模拟盘，回测 / 单元测试 |
| `EventDrivenPaperBroker` | `core.oms.EventDrivenPaperBroker`（别名 `core.brokers.PaperBroker`） | 事件驱动版，配合 `core.event_bus` 使用 |

生产链路：`Scheduler → IntradayMonitor → backend.services.broker.PaperBroker`，同步阻塞调用。

事件驱动栈（`EventBus / OMS / EventDrivenPaperBroker`）目前仅在 `core.paper_trade_validator` 与单测中实例化，主交易链路未消费。

## Broker 接口

`core/brokers/base.BrokerBase` 定义统一接口，子类实现：

```python
class BrokerBase:
    def connect(self) -> bool: ...
    def disconnect(self) -> None: ...
    def submit_order(self, symbol, direction, shares, price, price_type) -> OrderResult: ...
    def cancel_order(self, order_id) -> bool: ...
    def get_account_info(self) -> AccountInfo: ...
    def supported_markets(self) -> Set[MarketType]: ...
```

新增 broker 子类继承 `BrokerBase` 并实现全部 abstract 方法。

## 订单提交链路

```
HTTP POST /orders/submit
    ↓
backend.api_routes.orders.submit_order
    ↓ (Idempotency reserve)
core.use_cases.submit_order.submit_order
    ↓ (ref price + PreTrade 风控)
broker.submit_order(symbol, direction, shares, price, price_type)
    ↓
backend.services.broker.PaperBroker._fill
    ↓ (atomic SQL: 查现金 → 撮合 → 写持仓 → 写流水)
data/state.db
```

PaperBroker 内部以 `_lock`（threading.RLock）保证"查现金 → 撮合 → 写持仓"原子。

## Deprecated

下列文件 import 时打 `DeprecationWarning`，不再维护：

| 文件 | 原适配目标 |
|---|---|
| `core/brokers/futu.py` | 富途 OpenD |

如需接入真实券商，建议另起独立仓库 + 合规中间件（QMT / PTrade 等），不在本仓库做。
