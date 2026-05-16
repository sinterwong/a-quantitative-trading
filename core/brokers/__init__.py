# core.brokers — 券商适配器包
"""
券商适配器层。

⚠️ 产品定位:本系统不接入真实券商,所有"下单"均为虚拟模拟盘记账。
   仅 PaperBroker / EventDrivenPaperBroker / SimulatedBroker 受支持。

历史遗留(deprecated,仅保留代码雏形,导入时打 warning):
  - futu.py    — Futu OpenD 适配(已停止维护)
  - ibkr.py    — Interactive Brokers 适配(已停止维护)
  - tiger.py   — Tiger Brokers 适配(已停止维护)

SafetyMode 默认 PAPER。LIVE 模式在当前产品定位下视为危险路径,
保留代码但不建议解锁(后续可考虑彻底移除)。
"""

from core.brokers.paper import PaperBroker
from core.brokers.facade import BrokerFactory, SafetyMode

__all__ = ['PaperBroker', 'BrokerFactory', 'SafetyMode']
