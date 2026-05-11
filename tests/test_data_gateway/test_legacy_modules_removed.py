# -*- coding: utf-8 -*-
"""
守门测试 — 确认旧的 source 模块已彻底删除,防止后续 PR 误添加回流。

这些模块的能力已经迁移到 core/data_gateway/providers/*。
任何 import core.quote_data_source / quote_source_manager / tencent_quote_source /
sina_quote_source / eastmoney_sector_source / hk_data_source / symbol_utils
都应当 ImportError。
"""

import importlib

import pytest


_REMOVED_MODULES = [
    "core.quote_data_source",
    "core.quote_source_manager",
    "core.tencent_quote_source",
    "core.sina_quote_source",
    "core.eastmoney_sector_source",
    "core.hk_data_source",
    "core.symbol_utils",
]


@pytest.mark.parametrize("module_name", _REMOVED_MODULES)
def test_legacy_module_not_importable(module_name):
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module(module_name)
