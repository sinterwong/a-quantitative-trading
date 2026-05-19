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

## SafetyMode

`core.brokers.facade.BrokerFactory` 的 `SafetyMode`:

| Mode | 行为 |
|---|---|
| `PAPER`(默认) | 仅 PaperBroker,真实下单路径阻断 |
| `SIMULATED` | 同 PAPER,语义区分 |
| `LIVE` | 需 3 步显式解锁,默认禁用 |

`QUANT_BROKER_MODE` 环境变量强制覆盖。

## Deprecated

下列文件 import 时会打 `DeprecationWarning`,不再维护,后续清理周期可能删除:

| 文件 | 原适配目标 |
|---|---|
| `core/brokers/futu.py` | 富途 OpenD |

（R2-2: `core/brokers/ibkr.py` 和 `core/brokers/tiger.py` 已删除——
本系统不接入真实券商，stub 形态毫无价值，徒增维护负担。）

如需真实下单,建议另起独立仓库,接合规中间件(QMT / PTrade 等),不在本仓库做。
