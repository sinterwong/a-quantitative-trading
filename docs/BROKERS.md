# 券商

本系统不接入真实券商,所有"下单"都是虚拟模拟盘记账。

## 可用 broker

| 类 | 路径 | 用途 |
|---|---|---|
| `PaperBroker`(策略仿真) | `core.brokers.PaperBroker` | OMS + EventBus,适合策略仿真 |
| `SimulatedBroker` | `core.brokers.simulated.SimulatedBroker` | 同步语义模拟盘,回测/单元测试 |
| `PaperBroker`(生产 HTTP) | `backend.services.broker.PaperBroker` | 同步直写 SQLite,API 端点链路 |

> `core.brokers.PaperBroker` 是 `core.oms.EventDrivenPaperBroker` 的别名。
> 两个 PaperBroker 实现差异在事件驱动 vs 同步直写。

## Deprecated

下列文件 import 时会打 `DeprecationWarning`,不再维护,后续清理周期可能删除:

| 文件 | 原适配目标 |
|---|---|
| `core/brokers/futu.py` | 富途 OpenD |

（R2-2: `core/brokers/ibkr.py` / `tiger.py` / `facade.py`
[BrokerFactory + SafetyMode] 已删除——本系统不接入真实券商,SafetyMode
实际只剩 PAPER 一种状态,工厂层徒增维护负担。生产链路直接
`from backend.services.broker import PaperBroker`。）

如需真实下单,建议另起独立仓库,接合规中间件(QMT / PTrade 等),不在本仓库做。
