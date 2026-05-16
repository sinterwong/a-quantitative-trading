"""
ui/components — Streamlit 公共渲染组件 (P4-1 阶段二)

跨 page 共享的轻量渲染助手。每个函数只接收数据 + 返回 None(渲染到当前
streamlit 上下文),不持有状态。
"""

from .layout import regime_badge, regime_zh, broker_badge, global_css

__all__ = ['regime_badge', 'regime_zh', 'broker_badge', 'global_css']
