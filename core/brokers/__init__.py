# core.brokers — 券商适配器包
"""
券商适配器层。

⚠️ 产品定位:本系统不接入真实券商,所有"下单"均为虚拟模拟盘记账。
   仅 PaperBroker / EventDrivenPaperBroker / SimulatedBroker 受支持。

历史遗留(deprecated,仅保留代码雏形,导入时打 warning):
  - futu.py    — Futu OpenD 适配(已停止维护)

R2-2: ibkr.py / tiger.py / facade.py (BrokerFactory + SafetyMode) 已删除
——本系统不接入真实券商,生产链路直接 `from backend.services.broker import
PaperBroker`,不需要工厂层。SafetyMode 历史上是为"LIVE 模式 3 步解锁"
预留的,但产品定位明确不上 LIVE,SafetyMode 实际只剩 PAPER 一种状态,
保留它徒增维护负担。如未来需接入,请重新实现,不要恢复 stub。
"""

from core.brokers.paper import PaperBroker

__all__ = ['PaperBroker']
