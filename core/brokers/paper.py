"""
core/brokers/paper.py — PaperBroker
模拟撮合（复用现有 backend/services/broker.py 逻辑）。
已在 oms.py 中定义，此文件为 brokers/ 目录结构补全。
"""

from core.oms import PaperBroker as _PaperBroker

# 重导出，保持 API 兼容
PaperBroker = _PaperBroker

__all__ = ['PaperBroker']
