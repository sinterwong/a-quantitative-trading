# -*- coding: utf-8 -*-
"""
data_gateway.merge — 字段级数据聚合

将多个 provider 返回的同类型 dataclass(Quote / Fundamentals)按字段级
互补合并。每个字段独立挑选最优来源,不再以"哪家 provider 整体优先"为内核。

挑选规则(每字段):
  score = provider_health × field_authority × freshness_factor
  - provider_health: gateway 注入,来自 HealthTracker.score()
  - field_authority: provider.field_authority() 中声明的字段权重(默认 1.0)
  - freshness_factor: 若 dataclass 有 timestamp 字段,越新越大(目前简化为常量 1.0,
    避免在毫秒级行情场景里因时钟漂移误判)

字段是否"有值"判定:同 dataclass.default 比较。这处理了大部分基本类型的
缺省情况(0.0 / "" / 0),避免误把缺省值当作真实数据。
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple


# Provenance 中 divergence 元数据的键后缀。形如 `price__divergence`，值是
# 字符串化的浮点(0.0~1.0)。下游消费者(profile primary 源、调试导出)
# 通过 `endswith(DIVERGENCE_SUFFIX)` 过滤掉这些元数据键。
DIVERGENCE_SUFFIX = "__divergence"


# ─── 默认值判定 ────────────────────────────────────────────────────────────────


def _field_default(field) -> Any:
    """从 dataclasses.field 提取默认值(若有 default_factory 则调用一次)。"""
    if field.default is not _MISSING:
        return field.default
    if field.default_factory is not _MISSING_FACTORY:  # type: ignore[attr-defined]
        try:
            return field.default_factory()
        except Exception:
            return None
    return None


# 哨兵:dataclasses.MISSING 的 typed alias
from dataclasses import MISSING as _MISSING  # noqa: E402
_MISSING_FACTORY = _MISSING


def _has_value(value: Any, default: Any) -> bool:
    """判断字段是否"实际有值"(非默认)。"""
    if value is None:
        return False
    if isinstance(value, str):
        return value != "" and value != default
    if isinstance(value, (int, float)):
        # 0 / 0.0 视为"无值"(行情字段的默认占位)
        if value == 0:
            return False
        return value != default
    # 其他对象类型(datetime / dict / list):非空即视为有值
    if hasattr(value, "__len__"):
        return len(value) > 0
    return value != default


# ─── 候选结构 ──────────────────────────────────────────────────────────────────


class Candidate:
    """单个 provider 返回的一份候选数据 + 选源元数据。"""

    __slots__ = ("provider", "obj", "health", "authority")

    def __init__(
        self,
        provider: str,
        obj: Any,
        health: float = 1.0,
        authority: Optional[Dict[str, float]] = None,
    ):
        self.provider = provider
        self.obj = obj
        self.health = max(0.0, min(1.0, health))
        self.authority = authority or {}

    def field_score(self, field_name: str) -> float:
        """该候选对某字段的总分。"""
        return self.health * self.authority.get(field_name, 1.0)


# ─── 字段级差异度 ──────────────────────────────────────────────────────────────


def _field_divergence(values: List[Any]) -> float:
    """同字段多源取值的差异度，0.0 表示一致。

    - 数值(int/float/bool)：`(max - min) / max(|v|)`，纯零返回 0.0。
    - 其他类型：值不全相等返回 1.0，否则 0.0。
    """
    if len(values) < 2:
        return 0.0
    if all(isinstance(v, bool) or isinstance(v, (int, float)) for v in values):
        nums = [float(v) for v in values]
        max_abs = max(abs(v) for v in nums)
        if max_abs == 0.0:
            return 0.0
        return (max(nums) - min(nums)) / max_abs
    first = values[0]
    return 0.0 if all(v == first for v in values[1:]) else 1.0


# ─── 字段级合并 ────────────────────────────────────────────────────────────────


def merge_field_level(
    candidates: Iterable[Candidate],
    *,
    skip_fields: Iterable[str] = (),
) -> Tuple[Optional[Any], Dict[str, str]]:
    """对一组候选 dataclass 进行字段级合并。

    Args:
        candidates: 同类型 dataclass 的候选实例列表
        skip_fields: 不参与合并的字段名(如内部标识)

    Returns:
        (merged_obj, provenance):
          merged_obj: 合并后的新实例(类型与第一个非 None 候选相同)
          provenance: {field_name: provider_name} 记录每字段最终来源
    """
    cands: List[Candidate] = [c for c in candidates if c.obj is not None]
    if not cands:
        return None, {}
    if len(cands) == 1:
        only = cands[0]
        prov = {f.name: only.provider for f in fields(only.obj)}
        return only.obj, prov

    cls = type(cands[0].obj)
    if not is_dataclass(cls):
        # 非 dataclass:无法字段级合并,取健康度最高者
        winner = max(cands, key=lambda c: c.health)
        return winner.obj, {}

    skip = set(skip_fields)
    field_defaults = {f.name: _field_default(f) for f in fields(cls)}

    merged_values: Dict[str, Any] = {}
    provenance: Dict[str, str] = {}

    for fname, default in field_defaults.items():
        if fname in skip:
            merged_values[fname] = getattr(cands[0].obj, fname)
            provenance[fname] = cands[0].provider
            continue

        best_value = default
        best_score = -1.0
        best_provider: Optional[str] = None
        has_real_value = False
        real_values: List[Any] = []

        for cand in cands:
            value = getattr(cand.obj, fname, default)
            if not _has_value(value, default):
                continue
            real_values.append(value)
            score = cand.field_score(fname)
            if score > best_score:
                best_score = score
                best_value = value
                best_provider = cand.provider
                has_real_value = True

        if not has_real_value:
            # 所有候选都是默认值 → 任挑一个(取第一家)
            best_value = getattr(cands[0].obj, fname, default)
            best_provider = cands[0].provider

        merged_values[fname] = best_value
        provenance[fname] = best_provider or "unknown"

        if len(real_values) >= 2:
            div = _field_divergence(real_values)
            if div > 0.0:
                provenance[f"{fname}{DIVERGENCE_SUFFIX}"] = f"{div:.4f}"

    return cls(**merged_values), provenance


__all__ = [
    "Candidate",
    "DIVERGENCE_SUFFIX",
    "merge_field_level",
    "_field_divergence",
]
