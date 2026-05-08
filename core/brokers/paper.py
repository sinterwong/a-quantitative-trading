"""
core/brokers/paper.py — Paper broker 别名层

事件驱动版 paper broker 定义在 core.oms.EventDrivenPaperBroker。
此文件保留 `PaperBroker` 别名以维持 brokers/ 工厂的统一接口。

注意：与生产链路使用的 backend.services.broker.PaperBroker 不是同一个类。
- core.brokers.paper.PaperBroker        → 事件驱动（OMS / EventBus 路径）
- backend.services.broker.PaperBroker   → 同步直写 PortfolioService（生产链路）
"""

from core.oms import EventDrivenPaperBroker

# 在 brokers/ 命名空间中保持 PaperBroker 名字（与 futu/tiger/ibkr 对齐）
PaperBroker = EventDrivenPaperBroker

__all__ = ['PaperBroker', 'EventDrivenPaperBroker']
