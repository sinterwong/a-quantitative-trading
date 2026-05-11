# -*- coding: utf-8 -*-
"""
merge.py 单元测试 — 字段级聚合。
"""

from core.data_gateway.merge import Candidate, merge_field_level
from core.data_gateway.schemas import Fundamentals, Quote


# ── 基础 ─────────────────────────────────────────────────────────────────────


def test_empty_candidates():
    obj, prov = merge_field_level([])
    assert obj is None
    assert prov == {}


def test_single_candidate_passes_through():
    q = Quote(symbol="sh600519", price=100.0)
    obj, prov = merge_field_level([Candidate("tencent", q, health=0.5)])
    assert obj is q
    # 所有字段都来自该唯一 provider
    assert prov["symbol"] == "tencent"
    assert prov["price"] == "tencent"


# ── 字段级互补 ───────────────────────────────────────────────────────────────


def test_field_complement_takes_non_default():
    """A 有 price 无 pe,B 有 pe 无 price → 合并取两家之长。"""
    a = Quote(symbol="sh600519", price=100.0, pe_ttm=0.0)
    b = Quote(symbol="sh600519", price=0.0, pe_ttm=25.0)
    obj, prov = merge_field_level([
        Candidate("A", a, health=1.0),
        Candidate("B", b, health=1.0),
    ])
    assert obj.price == 100.0
    assert obj.pe_ttm == 25.0
    assert prov["price"] == "A"
    assert prov["pe_ttm"] == "B"


# ── 健康度排序 ───────────────────────────────────────────────────────────────


def test_healthier_provider_wins_on_overlap():
    """两家都有 price,健康度高的胜出。"""
    a = Quote(symbol="x", price=100.0)
    b = Quote(symbol="x", price=101.0)
    obj, prov = merge_field_level([
        Candidate("A", a, health=0.3),
        Candidate("B", b, health=0.9),
    ])
    assert obj.price == 101.0
    assert prov["price"] == "B"


# ── 字段权威表 ───────────────────────────────────────────────────────────────


def test_field_authority_overrides_health():
    """A 健康度低但对 pe_ttm 声明高权威,胜出。"""
    a = Quote(price=100, pe_ttm=20.0)
    b = Quote(price=100, pe_ttm=25.0)
    obj, prov = merge_field_level([
        Candidate("A", a, health=0.6, authority={"pe_ttm": 2.0}),  # 0.6*2.0=1.2
        Candidate("B", b, health=0.9, authority={"pe_ttm": 1.0}),  # 0.9*1.0=0.9
    ])
    assert obj.pe_ttm == 20.0
    assert prov["pe_ttm"] == "A"


# ── 默认值处理 ───────────────────────────────────────────────────────────────


def test_all_default_keeps_first():
    """所有候选某字段都是默认值 → 不报错,保留第一家。"""
    a = Quote(symbol="x")
    b = Quote(symbol="x")
    obj, prov = merge_field_level([
        Candidate("A", a, health=0.5),
        Candidate("B", b, health=0.5),
    ])
    assert obj.price == 0.0
    assert prov["price"] == "A"


def test_zero_is_treated_as_no_value_for_float():
    """0.0 视为占位,有非零值时取非零。"""
    a = Quote(symbol="x", pe_ttm=0.0)
    b = Quote(symbol="x", pe_ttm=15.0)
    obj, _ = merge_field_level([
        Candidate("A", a, health=0.9),
        Candidate("B", b, health=0.5),
    ])
    assert obj.pe_ttm == 15.0  # 即使 A 健康度更高,0.0 也被忽略


def test_empty_string_treated_as_no_value():
    a = Quote(symbol="", name="")
    b = Quote(symbol="sh600519", name="贵州茅台")
    obj, _ = merge_field_level([
        Candidate("A", a, health=0.9),
        Candidate("B", b, health=0.3),
    ])
    assert obj.symbol == "sh600519"
    assert obj.name == "贵州茅台"


# ── skip_fields ──────────────────────────────────────────────────────────────


def test_skip_fields_keep_first_provider():
    a = Quote(symbol="sh600519", price=100)
    b = Quote(symbol="sh600519_alt", price=200)
    obj, prov = merge_field_level(
        [Candidate("A", a, health=0.5), Candidate("B", b, health=0.9)],
        skip_fields=["symbol"],
    )
    # symbol 跳过合并,固定取第一家
    assert obj.symbol == "sh600519"
    assert prov["symbol"] == "A"
    # price 仍正常合并
    assert obj.price == 200


# ── Fundamentals 同样适用 ────────────────────────────────────────────────────


def test_fundamentals_merge():
    a = Fundamentals(symbol="x", pe_ttm=20, roe_ttm=0.0, industry="")
    b = Fundamentals(symbol="x", pe_ttm=0.0, roe_ttm=15.0, industry="白酒")
    obj, prov = merge_field_level([
        Candidate("A", a, health=0.7),
        Candidate("B", b, health=0.7),
    ])
    assert obj.pe_ttm == 20
    assert obj.roe_ttm == 15.0
    assert obj.industry == "白酒"
    assert prov["pe_ttm"] == "A"
    assert prov["roe_ttm"] == "B"


# ── None 候选过滤 ────────────────────────────────────────────────────────────


def test_none_obj_candidates_filtered():
    obj, prov = merge_field_level([
        Candidate("A", None, health=0.9),
        Candidate("B", Quote(price=100), health=0.5),
    ])
    assert obj.price == 100
    assert prov["price"] == "B"
