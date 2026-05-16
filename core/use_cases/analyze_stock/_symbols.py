"""
analyze_stock._symbols — A 股 / 港股代码识别与规范化。
"""

from __future__ import annotations

import re


_A_SHARE_PATTERN = re.compile(r'^\d{6}\.(SH|SZ)$', re.IGNORECASE)
_HK_PATTERNS = [
    re.compile(r'^HK:?(\d{3,5})$', re.IGNORECASE),
    re.compile(r'^hk(\d{3,5})$', re.IGNORECASE),
    re.compile(r'^(\d{3,5})\.HK$', re.IGNORECASE),
]


def detect_market(symbol: str) -> str:
    """识别股票市场:'A' / 'HK' / 'unknown'。"""
    s = symbol.strip()
    if _A_SHARE_PATTERN.match(s):
        return 'A'
    for p in _HK_PATTERNS:
        if p.match(s):
            return 'HK'
    return 'unknown'


def normalize_a_share_symbol(symbol: str) -> str:
    """统一 A 股代码大小写为 '600519.SH' 形式。"""
    s = symbol.strip().upper()
    if not _A_SHARE_PATTERN.match(s):
        raise ValueError(f'invalid A-share symbol: {symbol!r} (expected NNNNNN.SH/SZ)')
    return s


def normalize_hk_symbol(symbol: str) -> str:
    """统一港股代码为 'hkNNNNN' 形式(new sina API 用)。"""
    s = symbol.strip()
    for p in _HK_PATTERNS:
        m = p.match(s)
        if m:
            num = m.group(1).zfill(5)
            return f'hk{num}'
    raise ValueError(f'invalid HK symbol: {symbol!r} (expected HK:NNNNN / NNNNN.HK / hkNNNNN)')
