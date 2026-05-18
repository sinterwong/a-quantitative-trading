# -*- coding: utf-8 -*-
"""
G5 — _merged_list_fetch + 归一化辅助函数单元测试。

直接绕过 _route，对 MERGE_LISTS 原语做白盒覆盖：
  - 标题归一化（_news_dedupe_key）：去前缀 / 全角空格 / 末尾标点
  - 跨源 dedupe：保留 score 高的源那条
  - 时间倒序 + 缺 ts 兜底
  - 单源 / 无源 / 全 error 路径
  - prov_dict 记录每源贡献条数
"""

from datetime import datetime

import pytest

from core.data_gateway.capabilities import (
    Capability, Market, ProviderCapability,
)
from core.data_gateway.gateway import (
    DataGateway, _news_dedupe_key, _news_has_ts, _news_ts_epoch,
)
from core.data_gateway.providers.base import Provider, ProviderError
from core.data_gateway.schemas import NewsItem


# ── 归一化辅助 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,expected", [
    ("【快讯】央行降准",  "央行降准"),
    ("[财联社] 油价跳水", "油价跳水"),
    ("央行降准。",         "央行降准"),
    ("央行降准.",          "央行降准"),
    ("央行　降准",         "央行 降准"),           # 全角空格 → 半角
    ("央行   降准",        "央行 降准"),           # 多空白折叠
    ("  油价跳水  ",       "油价跳水"),
    ("",                   ""),
    ("【太长前缀超过十二字符不应被剥】消息", "【太长前缀超过十二字符不应被剥】消息"),
])
def test_news_dedupe_key_normalization(raw, expected):
    assert _news_dedupe_key(NewsItem(title=raw)) == expected


def test_news_dedupe_key_missing_title_attr_returns_empty():
    class _NoTitle: pass
    assert _news_dedupe_key(_NoTitle()) == ""


def test_news_has_ts_and_epoch_helpers():
    ts = datetime(2026, 5, 18, 21, 30, 0)
    item_with = NewsItem(title="a", timestamp=ts)
    item_without = NewsItem(title="b")
    assert _news_has_ts(item_with) is True
    assert _news_has_ts(item_without) is False
    assert _news_ts_epoch(item_with) > 0
    assert _news_ts_epoch(item_without) == 0.0


# ── _merged_list_fetch 行为 ─────────────────────────────────────────────────


class _NewsProvider(Provider):
    """可配置的 mock news provider。"""

    def __init__(self, name, items, hint=0.5, raises=False):
        self.name = name
        self._items = items
        self._hint = hint
        self._raises = raises

    def declare(self):
        return ProviderCapability(
            capabilities=frozenset({Capability.NEWS_HEADLINES}),
            markets=frozenset({Market.GLOBAL}),
            priority_hint=self._hint,
        )

    def fetch_news_headlines(self, symbol, n=20):
        if self._raises:
            raise ProviderError(f"{self.name} mocked failure")
        return list(self._items)


@pytest.fixture
def gw():
    from core.circuit_breaker import reset_all
    reset_all()
    return DataGateway(enable_disk_cache=False)


def test_no_candidates_returns_empty(gw):
    items, prov = gw._merged_list_fetch(
        Capability.NEWS_HEADLINES, Market.GLOBAL, "fetch_news_headlines", "X",
    )
    assert items == []
    assert prov == {}


def test_single_source_passes_through(gw):
    em_items = [
        NewsItem(title="A", timestamp=datetime(2026, 5, 18, 9, 0), source="em"),
        NewsItem(title="B", timestamp=datetime(2026, 5, 18, 10, 0), source="em"),
    ]
    gw.register_provider(_NewsProvider("em", em_items, hint=0.8))
    items, prov = gw._merged_list_fetch(
        Capability.NEWS_HEADLINES, Market.GLOBAL, "fetch_news_headlines", "X",
    )
    assert [it.title for it in items] == ["B", "A"]   # 按 ts 倒序
    assert prov == {"em": "2"}


def test_dedupe_keeps_highest_score_source_entry(gw):
    """两源同标题不同写法：保留 score 高的源那条（先进 seen_keys）。"""
    ts_em = datetime(2026, 5, 18, 9, 0)
    ts_ak = datetime(2026, 5, 18, 9, 1)
    em = _NewsProvider("em", [
        NewsItem(title="【快讯】央行降准", timestamp=ts_em, source="em"),
    ], hint=0.8)
    ak = _NewsProvider("ak", [
        NewsItem(title="央行降准。", timestamp=ts_ak, source="ak"),
    ], hint=0.3)
    gw.register_provider(em)
    gw.register_provider(ak)
    items, prov = gw._merged_list_fetch(
        Capability.NEWS_HEADLINES, Market.GLOBAL, "fetch_news_headlines", "X",
    )
    assert len(items) == 1
    assert items[0].source == "em"      # score 高的源胜出
    assert items[0].title == "【快讯】央行降准"
    assert prov == {"em": "1"}            # ak 全部去重 → 0 贡献不出现


def test_time_desc_sort_across_sources(gw):
    em = _NewsProvider("em", [
        NewsItem(title="A", timestamp=datetime(2026, 5, 18, 8, 0), source="em"),
        NewsItem(title="C", timestamp=datetime(2026, 5, 18, 12, 0), source="em"),
    ], hint=0.8)
    ak = _NewsProvider("ak", [
        NewsItem(title="B", timestamp=datetime(2026, 5, 18, 10, 0), source="ak"),
        NewsItem(title="D", timestamp=datetime(2026, 5, 18, 14, 0), source="ak"),
    ], hint=0.3)
    gw.register_provider(em)
    gw.register_provider(ak)
    items, _ = gw._merged_list_fetch(
        Capability.NEWS_HEADLINES, Market.GLOBAL, "fetch_news_headlines", "X",
    )
    assert [it.title for it in items] == ["D", "C", "B", "A"]


def test_missing_ts_items_sort_after_ts_items(gw):
    """缺 timestamp 的条目排在所有有 ts 的条目之后。"""
    em = _NewsProvider("em", [
        NewsItem(title="ts_item", timestamp=datetime(2026, 5, 18, 1, 0), source="em"),
        NewsItem(title="no_ts_em", timestamp=None, source="em"),
    ], hint=0.8)
    ak = _NewsProvider("ak", [
        NewsItem(title="no_ts_ak", timestamp=None, source="ak"),
    ], hint=0.3)
    gw.register_provider(em)
    gw.register_provider(ak)
    items, _ = gw._merged_list_fetch(
        Capability.NEWS_HEADLINES, Market.GLOBAL, "fetch_news_headlines", "X",
    )
    # 有 ts 的 "ts_item" 必排第一；后两条相对顺序：em 在前（先入 merged）
    assert items[0].title == "ts_item"
    assert {it.title for it in items[1:]} == {"no_ts_em", "no_ts_ak"}


def test_provenance_counts_unique_contributions_per_source(gw):
    em = _NewsProvider("em", [
        NewsItem(title="X", source="em"),
        NewsItem(title="Y", source="em"),
    ], hint=0.8)
    ak = _NewsProvider("ak", [
        NewsItem(title="X", source="ak"),    # 与 em 重复 → 不计
        NewsItem(title="Z", source="ak"),
    ], hint=0.3)
    gw.register_provider(em)
    gw.register_provider(ak)
    items, prov = gw._merged_list_fetch(
        Capability.NEWS_HEADLINES, Market.GLOBAL, "fetch_news_headlines", "X",
    )
    assert {it.title for it in items} == {"X", "Y", "Z"}
    assert prov == {"em": "2", "ak": "1"}


def test_one_source_errors_others_still_succeed(gw):
    em_ok = _NewsProvider("em", [
        NewsItem(title="A", timestamp=datetime(2026, 5, 18, 9), source="em"),
    ], hint=0.8)
    ak_bad = _NewsProvider("ak", [], hint=0.3, raises=True)
    gw.register_provider(em_ok)
    gw.register_provider(ak_bad)
    items, prov = gw._merged_list_fetch(
        Capability.NEWS_HEADLINES, Market.GLOBAL, "fetch_news_headlines", "X",
    )
    assert [it.title for it in items] == ["A"]
    assert prov == {"em": "1"}    # ak 完全不出现


def test_all_sources_error_returns_empty(gw):
    gw.register_provider(_NewsProvider("em", [], hint=0.8, raises=True))
    gw.register_provider(_NewsProvider("ak", [], hint=0.3, raises=True))
    items, prov = gw._merged_list_fetch(
        Capability.NEWS_HEADLINES, Market.GLOBAL, "fetch_news_headlines", "X",
    )
    assert items == []
    assert prov == {}


# ── 集成层：gw.news_headlines 出口仍 List[str] ─────────────────────────────


def test_news_headlines_public_api_projects_to_titles_after_merge_lists(gw):
    """G5-1 + G5-3 联动：底层走 NewsItem，公开 API 仍 List[str]。"""
    em = _NewsProvider("em", [
        NewsItem(title="A", timestamp=datetime(2026, 5, 18, 9), source="em"),
        NewsItem(title="B", timestamp=datetime(2026, 5, 18, 10), source="em"),
    ], hint=0.8)
    ak = _NewsProvider("ak", [
        NewsItem(title="C", timestamp=datetime(2026, 5, 18, 11), source="ak"),
    ], hint=0.3)
    gw.register_provider(em)
    gw.register_provider(ak)
    out = gw.news_headlines("sh600519", n=10)
    assert isinstance(out, list)
    assert all(isinstance(s, str) for s in out)
    assert out == ["C", "B", "A"]       # 时间倒序
