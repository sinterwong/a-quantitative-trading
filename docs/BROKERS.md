# 券商接入政策

> 产品定位:**本系统不接入真实券商**,所有"下单"均为虚拟模拟盘记账。

---

## 支持的 Broker(本次重构后唯一推荐路径)

| Broker 类 | 路径 | 用途 |
|---|---|---|
| `PaperBroker` | `core.brokers.PaperBroker` | 事件驱动模拟盘(`core.oms.EventDrivenPaperBroker` 别名) |
| `SimulatedBroker` | `core.brokers.simulated.SimulatedBroker` | 同步语义模拟盘(回测 / 单元测试) |
| 生产链路 PaperBroker | `backend.services.broker.PaperBroker` | 直写 PortfolioService(API 生产链路) |

> 注:两个 PaperBroker 实现差异:
> - `core.brokers.PaperBroker` 走 OMS + EventBus,适合策略仿真
> - `backend.services.broker.PaperBroker` 同步直写 SQLite,适合 HTTP 端点

---

## SafetyMode 政策

`core.brokers.facade.BrokerFactory` 提供 `SafetyMode`:

| Mode | 行为 | 推荐 |
|---|---|---|
| `PAPER` | 仅 PaperBroker,所有真实下单路径阻断 | ✅ 默认 |
| `SIMULATED` | 等同 PAPER,语义区分 | ✅ 回测/研究 |
| `LIVE` | 允许真实下单,需 3 步显式解锁 | ❌ **当前定位下视为危险路径,不建议解锁** |

环境变量 `QUANT_BROKER_MODE` 强制覆盖。默认 PAPER。

---

## Deprecated Broker(代码雏形保留,不再维护)

下列文件 **导入时打 DeprecationWarning**,生产/研究代码请勿引用:

| 文件 | 原适配目标 | 状态 |
|---|---|---|
| `core/brokers/futu.py` | 富途 OpenD | DEPRECATED |
| `core/brokers/ibkr.py` | Interactive Brokers | DEPRECATED (Stub) |
| `core/brokers/tiger.py` | 老虎证券 | DEPRECATED (Stub) |

后续可能彻底移除(下个清理周期),代码保留供历史参考。

---

## 为何不接入真实券商?

1. **产品定位**:研究 + 模拟盘验证,不为单租户提供实盘下单服务
2. **风险**:真实下单需要严格的合规、风控、签约、KYC,与单租户研究台的简洁性冲突
3. **数据已足够**:Gateway 已覆盖所需所有市场数据,虚拟盘对策略验证已经足够保真

如未来需要实盘:
- 不在本仓库内做,另起一个 `quant-live` 仓库独立隔离
- 引入第三方合规中间件(如 QMT、PTrade、Algolithms)
