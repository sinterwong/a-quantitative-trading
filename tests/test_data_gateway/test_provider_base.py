# -*- coding: utf-8 -*-
"""
Provider ABC 单元测试 — 默认实现 / declare 必填。
"""

import pandas as pd
import pytest

from core.data_gateway.capabilities import Capability, Market, ProviderCapability
from core.data_gateway.providers.base import Provider, ProviderError


class _NoOpProvider(Provider):
    """最小实现:只声明能力,不实现任何 fetch。"""

    name = "noop"

    def declare(self) -> ProviderCapability:
        return ProviderCapability(
            capabilities=frozenset({Capability.QUOTE}),
            markets=frozenset({Market.A}),
            priority_hint=0.5,
        )


def test_provider_abstract_requires_declare():
    """没实现 declare 的子类不能实例化。"""
    with pytest.raises(TypeError):
        Provider()  # type: ignore[abstract]


def test_provider_default_fetch_methods_return_empty():
    """未覆盖的 fetch_* 方法返回 None / 空容器,不抛异常。"""
    p = _NoOpProvider()
    assert p.fetch_quote("sh600519") is None
    assert p.fetch_quotes(["sh600519"]) == {}
    assert isinstance(p.fetch_kline("sh600519"), pd.DataFrame)
    assert p.fetch_kline("sh600519").empty
    assert p.fetch_fundamentals("sh600519") is None
    assert p.fetch_sectors() == []
    assert p.fetch_sector_constituents("BK0716") == []
    assert p.fetch_north_flow() is None
    assert p.fetch_market_index("VIX") is None


def test_provider_default_field_authority_empty():
    assert _NoOpProvider().field_authority() == {}


def test_provider_declaration_round_trip():
    decl = _NoOpProvider().declare()
    assert Capability.QUOTE in decl.capabilities
    assert Market.A in decl.markets
    assert 0.0 <= decl.priority_hint <= 1.0


def test_provider_error_is_exception():
    assert issubclass(ProviderError, Exception)


def test_supports_default_checks_capabilities_and_markets():
    p = _NoOpProvider()
    assert p.supports(Capability.QUOTE, Market.A) is True
    assert p.supports(Capability.QUOTE, Market.HK) is False  # market 不在声明
    assert p.supports(Capability.KLINE_DAILY, Market.A) is False  # capability 不在声明


def test_supports_can_be_overridden():
    class _Refined(_NoOpProvider):
        def declare(self):
            return ProviderCapability(
                capabilities=frozenset({Capability.KLINE_MINUTE}),
                markets=frozenset({Market.A, Market.HK}),
                priority_hint=0.7,
            )

        def supports(self, capability, market):
            # A 股分钟 K 不支持(模拟腾讯)
            if capability == Capability.KLINE_MINUTE and market == Market.A:
                return False
            return super().supports(capability, market)

    p = _Refined()
    assert p.supports(Capability.KLINE_MINUTE, Market.HK) is True
    assert p.supports(Capability.KLINE_MINUTE, Market.A) is False
