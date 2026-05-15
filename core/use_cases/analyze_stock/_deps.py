"""
analyze_stock._deps — 依赖解析辅助。

优先使用 ``req`` 中显式注入的实例(测试/多源场景),
否则回退到 ``core.data_gateway.get_gateway`` /
``core.data_layer.get_data_layer`` 全局 singleton。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._types import AnalysisRequest


def resolve_gateway(req: 'AnalysisRequest'):
    """req.gateway > get_gateway()。"""
    if getattr(req, 'gateway', None) is not None:
        return req.gateway
    from core.data_gateway import get_gateway
    return get_gateway()


def resolve_data_layer(req: 'AnalysisRequest'):
    """req.data_layer > get_data_layer()。"""
    if getattr(req, 'data_layer', None) is not None:
        return req.data_layer
    from core.data_layer import get_data_layer
    return get_data_layer()
