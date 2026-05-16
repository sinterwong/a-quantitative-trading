"""
core/use_cases/morning_workflow.py — 早盘工作流 use case (P2-4)

负责把"早盘工作流"涉及的数据装配成结构化结果(MorningReport)。
外部 IO(HTTP 调用 backend / 推送飞书) 由 caller(scripts/morning_runner.py)负责。

当前 MVP 范围:
- 装配候选 / 持仓 / 现金 / regime → MorningReport 结构
- 提供降级文本生成(fallback report,scripts/morning_report 不可用时使用)

未来扩展(等 backend 走 in-process 调用而非 HTTP):
- 整个 morning_runner.run() 可以下沉到本 use case
- 此时 scripts/morning_runner.py 退化为 ≤30 行的 CLI
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional


@dataclass
class MorningWorkflowRequest:
    """早盘工作流装配输入。"""
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    regime_info: Dict[str, Any] = field(default_factory=dict)
    positions: List[Dict[str, Any]] = field(default_factory=list)
    cash: float = 0.0
    equity: float = 0.0


@dataclass
class MorningReport:
    """早盘装配结果。"""
    date: str
    regime: str
    regime_reason: str
    atr_ratio: float
    equity: float
    cash: float
    candidates: List[Dict[str, Any]] = field(default_factory=list)
    positions_count: int = 0
    notes_for_daily_meta: str = ''
    fallback_text: str = ''   # 降级文本(structured report 不可用时使用)

    def to_dict(self) -> dict:
        return {
            'date': self.date,
            'regime': self.regime,
            'regime_reason': self.regime_reason,
            'atr_ratio': self.atr_ratio,
            'equity': self.equity,
            'cash': self.cash,
            'candidates': self.candidates,
            'positions_count': self.positions_count,
            'notes_for_daily_meta': self.notes_for_daily_meta,
            'fallback_text': self.fallback_text,
        }


def _normalize_candidates(raw: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """统一候选字段 (兼容 dynamic_selector 多种输出形态)。"""
    out = []
    for c in raw:
        out.append({
            'code': c.get('code', c.get('symbol', '')),
            'symbol': c.get('symbol', c.get('code', '')),
            'name': c.get('name', ''),
            'change_pct': c.get('pct', c.get('change_pct', 0)),
            'sector_name': c.get('sector', c.get('sector_name', '')),
            'total_score': c.get('score', c.get('total_score', c.get('total', 0))),
        })
    return out


def build_fallback_report_text(req: MorningWorkflowRequest) -> str:
    """生成降级早报文本(纯字符串渲染,无外部依赖)。"""
    today = date.today().isoformat()
    regime = req.regime_info.get('regime', 'CALM')
    reason = req.regime_info.get('regime_reason', '')
    atr = float(req.regime_info.get('atr_ratio', 0.0))

    lines = [
        f"【早报降级版】{today}",
        "",
        f"市场环境: [{regime}] {reason}",
        f"ATR ratio: {atr:.3f}",
        f"开盘权益: {req.equity:.0f}  现金: {req.cash:.0f}",
        "",
        f"今日候选 ({len(req.candidates)}只):",
    ]
    for c in req.candidates[:5]:
        sym = c.get('symbol', c.get('code', '?'))
        nm = c.get('name', '')
        score = float(c.get('score', c.get('total_score', c.get('total', 0))) or 0)
        lines.append(f"  {sym} {nm} score={score:.0f}")
    lines.append("")
    lines.append("(开仓决策由盘中 IntradayMonitor 处理)")
    return '\n'.join(lines)


def assemble_morning_report(req: MorningWorkflowRequest) -> MorningReport:
    """装配早盘报告结构(纯逻辑,无 IO)。

    输入已由 caller 准备好(选股 / regime / 持仓 / 现金 / equity),
    本函数只做数据装配 + 降级文本生成。
    """
    today = date.today().isoformat()
    regime = req.regime_info.get('regime', 'CALM')
    reason = req.regime_info.get('regime_reason', '')
    atr = float(req.regime_info.get('atr_ratio', 0.0))

    candidates_norm = _normalize_candidates(req.candidates)

    notes = (
        f"[MorningRunner] regime={regime} "
        f"ATR={atr:.2f} "
        f"candidates:{len(req.candidates)} "
        f"positions:{len(req.positions)} "
        f"equity={req.equity:.0f} cash={req.cash:.0f}"
    )

    return MorningReport(
        date=today,
        regime=regime,
        regime_reason=reason,
        atr_ratio=atr,
        equity=req.equity,
        cash=req.cash,
        candidates=candidates_norm,
        positions_count=len(req.positions),
        notes_for_daily_meta=notes,
        fallback_text=build_fallback_report_text(req),
    )


__all__ = [
    'MorningWorkflowRequest',
    'MorningReport',
    'assemble_morning_report',
    'build_fallback_report_text',
]
