"""
analyze_stock._recommend — 投资建议决策(combined_score × Regime × 基本面)。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def make_recommendation(combined_score: float, dominant: str,
                        regime: Optional[Dict[str, Any]],
                        fundamentals: Optional[Dict[str, Any]],
                        risk: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """规则化决策:综合得分 × Regime 阻尼 × 基本面健康度。

    基本面硬红线(任一条触发直接 SELL,不受综合得分覆盖):
      - 营收 YoY 连续为负(本期 < 0)
      - 净利 YoY 连续为负(本期 < 0)
      - 任一 YoY 跌幅超过 20%(趋势性恶化)

    返回 {action, confidence, reasoning}.
    """
    score = float(combined_score or 0.0)
    reasons: List[str] = []

    # 0) 基本面硬红线 — 任一触发则强制 SELL,不受综合得分左右
    fundamental_red = False
    if fundamentals:
        rev = fundamentals.get('revenue_yoy')
        profit = fundamentals.get('profit_yoy')
        roe = fundamentals.get('roe_ttm')

        if roe is not None and roe < 0:
            fundamental_red = True
            reasons.append(f'ROE 为负({roe:.1f}%)')

        if rev is not None:
            if rev < -20:
                fundamental_red = True
                reasons.append(f'营收同比大幅下滑({rev:+.1f}%)')
            elif rev < 0:
                fundamental_red = True
                reasons.append(f'营收同比下滑({rev:+.1f}%)')

        if profit is not None:
            if profit < -20:
                fundamental_red = True
                reasons.append(f'净利同比大幅下滑({profit:+.1f}%)')
            elif profit < 0:
                fundamental_red = True
                reasons.append(f'净利同比下滑({profit:+.1f}%)')

    # 1) Regime 调整
    multiplier = 1.0
    blocked = False
    if regime:
        mult = float(regime.get('signal_threshold_multiplier', 1.0) or 1.0)
        if mult > 1.0:
            multiplier = mult
            reasons.append(f"Regime={regime.get('regime')} 阈值×{mult:.2f}")
        if regime.get('regime') == 'BEAR' and not regime.get('allow_new_buys', True):
            blocked = True
            reasons.append('BEAR 禁止新开多仓')

    # 2) 基本面软修正(仅在未触发红线时适用)
    # 营收/净利本身的 YoY 已在红线处理;这里只处理无 YoY 风险但 ROE/营收质量偏弱的情况
    if fundamentals and not fundamental_red:
        pass  # 暂不需要软修正,当前因子已足够严格

    # 3) 风险约束
    if risk and isinstance(risk, dict):
        ann_vol = risk.get('annualized_vol')
        if ann_vol is not None and ann_vol > 0.60:
            score *= 0.85
            reasons.append(f'年化波动 {ann_vol:.1%} 偏高,置信折扣')

    # 4) 决策
    threshold = 0.5 * multiplier
    if blocked:
        action = 'HOLD'
    elif fundamental_red:
        # 基本面红线:强制 SELL,reasoning 已在上面说明具体原因
        action = 'SELL'
    elif score >= threshold:
        action = 'BUY'
    elif score <= -threshold:
        action = 'SELL'
    else:
        action = 'HOLD'

    confidence = min(1.0, abs(score) / max(1.0, threshold))
    if dominant in ('BUY', 'SELL') and dominant == action:
        confidence = min(1.0, confidence + 0.10)

    return {
        'action': action,
        'confidence': round(confidence, 4),
        'adjusted_score': round(score, 4),
        'threshold': round(threshold, 4),
        'reasoning': '; '.join(reasons) or '基于综合得分',
    }
