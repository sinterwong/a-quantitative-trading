"""
core/use_cases/intraday_signals.py — 盘中信号生成 use case (P2-3)

从 watchlist + pipeline scores 中筛选出可触发建仓的候选标的(BUY)。

这是 IntradayMonitor 调用链的"信号生成"环节,纯逻辑函数,无 IO。
后续的实时价拉取 / 分钟级确认 / 风控 / LLM 审核 / 实际下单
仍保留在 IntradayMonitor 编排层(P2-7 进一步拆分)。

设计:
- 纯函数,输入决定输出,易测试
- 不依赖 DataGateway / 配置 / 数据库
- 失败/异常上抛 UseCaseError,由 caller 决定降级行为
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


@dataclass
class IntradaySignalRequest:
    """生成 BUY 候选的输入。"""
    watched_symbols: List[str]
    pipeline_scores: Dict[str, float]
    threshold: float = 0.5
    excluded_symbols: Set[str] = field(default_factory=set)


@dataclass
class IntradaySignalCandidate:
    """单个候选标的。"""
    symbol: str
    score: float
    direction: str = 'BUY'
    reason: str = ''

    def to_dict(self) -> dict:
        return {
            'symbol': self.symbol,
            'score': round(float(self.score), 6),
            'direction': self.direction,
            'reason': self.reason,
        }


@dataclass
class IntradaySignalResponse:
    candidates: List[IntradaySignalCandidate] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)
    threshold_used: float = 0.0

    def to_dict(self) -> dict:
        return {
            'candidates': [c.to_dict() for c in self.candidates],
            'skipped': self.skipped,
            'threshold_used': self.threshold_used,
        }


def generate_intraday_signals(
    req: IntradaySignalRequest,
) -> IntradaySignalResponse:
    """从 pipeline scores 中筛选超过 threshold 的 BUY 候选。

    筛选规则:
      1. symbol 必须在 watched_symbols 中
      2. symbol 不能在 excluded_symbols(已持仓)中
      3. score 必须 > 0
      4. score 必须 > threshold

    返回的候选按 score 降序,便于 caller 取 top-N。
    """
    candidates: List[IntradaySignalCandidate] = []
    skipped: List[dict] = []

    for sym in req.watched_symbols:
        if sym in req.excluded_symbols:
            skipped.append({'symbol': sym, 'reason': 'already_held'})
            continue

        score = req.pipeline_scores.get(sym)
        if score is None:
            skipped.append({'symbol': sym, 'reason': 'no_score'})
            continue

        if score <= 0:
            skipped.append({'symbol': sym, 'reason': 'non_positive_score'})
            continue

        if score <= req.threshold:
            skipped.append({
                'symbol': sym,
                'reason': f'score={score:.3f} <= threshold={req.threshold:.3f}',
            })
            continue

        candidates.append(IntradaySignalCandidate(
            symbol=sym,
            score=float(score),
            direction='BUY',
            reason=f'Pipeline score={score:.3f} > threshold={req.threshold:.3f}',
        ))

    candidates.sort(key=lambda c: c.score, reverse=True)
    return IntradaySignalResponse(
        candidates=candidates,
        skipped=skipped,
        threshold_used=req.threshold,
    )


__all__ = [
    'IntradaySignalRequest',
    'IntradaySignalCandidate',
    'IntradaySignalResponse',
    'generate_intraday_signals',
]
