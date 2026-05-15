"""
core/use_cases/daily_analysis.py — 每日分析 use case (P2-8 批次 4)

把 backend/api.py 中 /analysis/run 端点的内联业务逻辑下沉到本层:
DynamicStockSelector 选股 → 记录信号 → 持久化 JSON → 写 daily_meta。
"""

from __future__ import annotations

import json
import os
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class DailyAnalysisRequest:
    news_limit: int = 20
    top_sectors_n: int = 5
    news_summary_n: int = 10
    stocks_n: int = 5
    output_dir: str = ''   # 空则默认 backend/outputs/analysis


@dataclass
class DailyAnalysisResponse:
    sources: Dict[str, Any] = field(default_factory=dict)
    top_sectors: List[Dict[str, Any]] = field(default_factory=list)
    news_summary: List[Any] = field(default_factory=list)
    selected_stocks: List[Any] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'sources': self.sources,
            'top_sectors': self.top_sectors,
            'news_summary': self.news_summary,
            'selected_stocks': self.selected_stocks,
        }


def run_daily_analysis(req: DailyAnalysisRequest,
                       portfolio_svc: Optional[Any] = None) -> DailyAnalysisResponse:
    """
    跑一次每日分析:DynamicStockSelector 选股 + 记录信号到 portfolio_svc。

    持久化与 daily_meta 写入失败不会抛异常,只记录日志(降级行为)。
    """
    # scripts/ 是 package(scripts/__init__.py 存在),repo root 在 sys.path
    # 时直接可用——不必再做 sys.path 注入。
    from scripts.dynamic_selector import DynamicStockSelector

    selector = DynamicStockSelector()
    selector.fetch_market_news(req.news_limit)
    selector.calc_all_scores()
    top_bks = selector.get_top_bk_sectors(req.top_sectors_n)
    news = selector.get_news_summary(req.news_summary_n)
    stocks = selector.get_stock_with_context(req.stocks_n)

    # 记录信号
    if portfolio_svc is not None:
        for bk, info in top_bks:
            portfolio_svc.record_signal(
                bk, 'BUY', info.get('total', 0) / 100.0,
                f"板块:{info.get('name','')} 涨幅:{info.get('change_pct',0):.2f}%",
            )

    response = DailyAnalysisResponse(
        sources={
            'news': selector._last_news_source,
            'sectors': selector._last_source,
        },
        top_sectors=[
            {'bk': bk, 'name': info.get('name'), 'total': info.get('total'),
             'change_pct': info.get('change_pct')}
            for bk, info in top_bks
        ],
        news_summary=news,
        selected_stocks=stocks,
    )

    _persist(response, req.output_dir)
    if portfolio_svc is not None:
        _record_daily_meta(portfolio_svc, len(top_bks))
    return response


def _persist(response: DailyAnalysisResponse, output_dir: str) -> None:
    """落 JSON 到 outputs/analysis/。失败仅记日志。"""
    try:
        if not output_dir:
            backend_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            output_dir = os.path.join(backend_dir, 'backend', 'outputs', 'analysis')
        os.makedirs(output_dir, exist_ok=True)
        today_str = datetime.now().strftime('%Y-%m-%d')
        out_path = os.path.join(output_dir, f'analysis_{today_str}.json')
        with open(out_path, 'w', encoding='utf-8') as f:
            json.dump({
                'timestamp': datetime.now().isoformat(),
                **response.to_dict(),
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning('daily_analysis persist failed: %s', e)


def _record_daily_meta(portfolio_svc: Any, n_signals: int) -> None:
    """记录 daily_meta,供 StrategyHealthMonitor 读取。失败仅记日志。"""
    try:
        summary = portfolio_svc.get_portfolio_summary()
        trades_today = portfolio_svc.get_trades(limit=200)
        today_iso = datetime.now().strftime('%Y-%m-%d')
        n_trades = sum(1 for t in trades_today
                       if str(t.get('timestamp', ''))[:10] == today_iso)
        portfolio_svc.record_daily_meta(
            equity=float(summary.get('total_equity', 0) or 0),
            cash=float(summary.get('cash', 0) or 0),
            n_signals=n_signals,
            n_trades=n_trades,
        )
    except Exception as e:
        logger.warning('daily_meta record failed: %s', e)
