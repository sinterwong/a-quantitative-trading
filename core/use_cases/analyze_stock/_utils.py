"""
analyze_stock._utils — 通用小工具。
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional


def safe_float(v) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 6)
    except (TypeError, ValueError):
        return None


def safe_json_extract(text: str) -> Optional[Dict[str, Any]]:
    """尝试从 LLM 输出中提取 JSON。LLM 经常在 JSON 周围包裹解释或代码块。"""
    if not text:
        return None
    import json
    # 优先尝试三反引号代码块
    m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    candidate = m.group(1) if m else None
    if candidate is None:
        # 否则取首个 { 到末尾 }
        i = text.find('{')
        j = text.rfind('}')
        if i >= 0 and j > i:
            candidate = text[i:j + 1]
    if not candidate:
        return None
    try:
        return json.loads(candidate)
    except Exception:
        return None
