"""
core/daily_diff_reporter.py — 每日回测 vs 模拟实盘信号对比（P3-D）

功能：
  - 对比 StrategyRunner 产生的实时/模拟信号（RunResult 列表）与
    BacktestEngine 在同一天的回测信号（TradeRecord 列表）
  - 检测：方向不一致、回测有 / 实盘无（bt_only）、实盘有 / 回测无（live_only）
  - 输出 JSON 报告到 reports/daily_bt_live_diff_{date}.json

用法：
    from core.daily_diff_reporter import DailyDiffReporter
    from core.strategy_runner import StrategyRunner
    from core.backtest_engine import BacktestEngine, BacktestConfig

    reporter = DailyDiffReporter()
    report = reporter.compare(
        live_results=runner.last_results,
        bt_trades=bt_result.trades,
        report_date=date.today(),
    )
    reporter.save(report)
    print(reporter.format_text(report))
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

_REPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'reports'
)
os.makedirs(_REPORTS_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class LiveSignalRecord:
    """模拟实盘单条信号记录（来自 RunResult）。"""
    symbol: str
    timestamp: str           # ISO 格式
    action: str              # 'BUY' | 'SELL' | 'NONE' | 'SKIPPED' | 'ERROR'
    combined_score: float
    reason: str


@dataclass
class BtSignalRecord:
    """回测单条信号记录（来自 TradeRecord）。"""
    symbol: str
    trade_date: str          # ISO 格式 (date)
    direction: str           # 'BUY' | 'SELL'
    price: float
    shares: int
    signal_reason: str
    signal_strength: float


@dataclass
class SignalMismatch:
    """信号不一致条目。"""
    symbol: str
    mismatch_type: str       # 'direction_mismatch' | 'bt_only' | 'live_only'
    bt_action: Optional[str]
    live_action: Optional[str]
    detail: str


@dataclass
class DailyDiffReport:
    """单日对比报告。"""
    report_date: str                     # ISO date
    generated_at: str                    # ISO datetime
    # 汇总
    n_live_signals: int                  # 模拟实盘触发信号数
    n_bt_signals: int                    # 回测当日信号数
    n_matches: int                       # 方向一致的匹配数
    n_mismatches: int                    # 不一致总数
    consistency_pct: float               # n_matches / max(n_live_signals, n_bt_signals)
    # 明细
    live_signals: List[LiveSignalRecord] = field(default_factory=list)
    bt_signals: List[BtSignalRecord]    = field(default_factory=list)
    mismatches: List[SignalMismatch]    = field(default_factory=list)
    # 诊断
    notes: List[str]                     = field(default_factory=list)

    def is_healthy(self, min_consistency: float = 0.8) -> bool:
        """一致率 >= min_consistency 且无 direction_mismatch 视为健康。"""
        has_direction_mismatch = any(
            m.mismatch_type == 'direction_mismatch' for m in self.mismatches
        )
        return self.consistency_pct >= min_consistency and not has_direction_mismatch


# ---------------------------------------------------------------------------
# DailyDiffReporter
# ---------------------------------------------------------------------------

class DailyDiffReporter:
    """
    每日回测 vs 模拟实盘信号对比器。

    Parameters
    ----------
    reports_dir:
        报告输出目录，默认 <project_root>/reports/
    """

    def __init__(self, reports_dir: str = _REPORTS_DIR) -> None:
        self.reports_dir = reports_dir
        os.makedirs(reports_dir, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compare(
        self,
        live_results: list,          # List[RunResult]
        bt_trades: list,             # List[TradeRecord]
        report_date: Optional[date] = None,
    ) -> DailyDiffReport:
        """
        对比模拟实盘信号与回测信号。

        Parameters
        ----------
        live_results:
            StrategyRunner.last_results（RunResult 列表）
        bt_trades:
            BacktestResult.trades，过滤当日记录（TradeRecord 列表）
        report_date:
            报告日期（默认今日）
        """
        if report_date is None:
            report_date = date.today()

        # 1. 整理实盘信号（只取 BUY / SELL）
        live_signals = self._extract_live_signals(live_results)
        live_acted = {
            s.symbol: s for s in live_signals if s.action in ('BUY', 'SELL')
        }

        # 2. 整理回测信号（过滤到同一天）
        bt_signals = self._extract_bt_signals(bt_trades, report_date)
        # 同一标的可能有多笔，取最后一笔作为代表
        bt_acted: Dict[str, BtSignalRecord] = {}
        for s in bt_signals:
            bt_acted[s.symbol] = s

        # 3. 比对
        all_symbols = set(live_acted) | set(bt_acted)
        mismatches: List[SignalMismatch] = []
        n_matches = 0

        for sym in sorted(all_symbols):
            live_sig = live_acted.get(sym)
            bt_sig   = bt_acted.get(sym)

            if live_sig and bt_sig:
                if live_sig.action == bt_sig.direction:
                    n_matches += 1
                else:
                    mismatches.append(SignalMismatch(
                        symbol=sym,
                        mismatch_type='direction_mismatch',
                        bt_action=bt_sig.direction,
                        live_action=live_sig.action,
                        detail=(
                            f'回测={bt_sig.direction}(strength={bt_sig.signal_strength:.3f}) '
                            f'实盘={live_sig.action}(score={live_sig.combined_score:.4f})'
                        ),
                    ))
            elif bt_sig and not live_sig:
                mismatches.append(SignalMismatch(
                    symbol=sym,
                    mismatch_type='bt_only',
                    bt_action=bt_sig.direction,
                    live_action=None,
                    detail=f'回测有信号({bt_sig.direction})，实盘未触发',
                ))
            elif live_sig and not bt_sig:
                mismatches.append(SignalMismatch(
                    symbol=sym,
                    mismatch_type='live_only',
                    bt_action=None,
                    live_action=live_sig.action,
                    detail=(
                        f'实盘有信号({live_sig.action}，score={live_sig.combined_score:.4f})，'
                        f'回测无同日交易'
                    ),
                ))

        denom = max(len(live_acted), len(bt_acted), 1)
        consistency_pct = round(n_matches / denom, 4)

        # 4. 诊断备注
        notes: List[str] = []
        if not live_acted and not bt_acted:
            notes.append('当日双方均无信号触发（可能非交易日或信号阈值过高）')
        if consistency_pct < 0.8:
            notes.append(
                f'一致率 {consistency_pct:.1%} < 80%，'
                '建议检查信号阈值、数据对齐或滑点设置'
            )
        direction_mismatches = [m for m in mismatches if m.mismatch_type == 'direction_mismatch']
        if direction_mismatches:
            syms = [m.symbol for m in direction_mismatches]
            notes.append(f'方向不一致标的: {syms}，需重点排查因子参数或时间对齐')

        return DailyDiffReport(
            report_date=report_date.isoformat(),
            generated_at=datetime.now().isoformat(timespec='seconds'),
            n_live_signals=len(live_acted),
            n_bt_signals=len(bt_acted),
            n_matches=n_matches,
            n_mismatches=len(mismatches),
            consistency_pct=consistency_pct,
            live_signals=live_signals,
            bt_signals=bt_signals,
            mismatches=mismatches,
            notes=notes,
        )

    def save(self, report: DailyDiffReport) -> str:
        """
        保存报告为 JSON。

        Returns
        -------
        保存路径
        """
        fname = f'daily_bt_live_diff_{report.report_date}.json'
        path = os.path.join(self.reports_dir, fname)

        data = {
            'report_date': report.report_date,
            'generated_at': report.generated_at,
            'summary': {
                'n_live_signals': report.n_live_signals,
                'n_bt_signals': report.n_bt_signals,
                'n_matches': report.n_matches,
                'n_mismatches': report.n_mismatches,
                'consistency_pct': report.consistency_pct,
                'is_healthy': report.is_healthy(),
            },
            'live_signals': [asdict(s) for s in report.live_signals],
            'bt_signals': [asdict(s) for s in report.bt_signals],
            'mismatches': [asdict(m) for m in report.mismatches],
            'notes': report.notes,
        }

        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        return path

    def load(self, report_date: date) -> Optional[DailyDiffReport]:
        """读取指定日期的报告（不存在返回 None）。"""
        fname = f'daily_bt_live_diff_{report_date.isoformat()}.json'
        path = os.path.join(self.reports_dir, fname)
        if not os.path.exists(path):
            return None

        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        summary = data['summary']
        return DailyDiffReport(
            report_date=data['report_date'],
            generated_at=data['generated_at'],
            n_live_signals=summary['n_live_signals'],
            n_bt_signals=summary['n_bt_signals'],
            n_matches=summary['n_matches'],
            n_mismatches=summary['n_mismatches'],
            consistency_pct=summary['consistency_pct'],
            live_signals=[LiveSignalRecord(**s) for s in data.get('live_signals', [])],
            bt_signals=[BtSignalRecord(**s) for s in data.get('bt_signals', [])],
            mismatches=[SignalMismatch(**m) for m in data.get('mismatches', [])],
            notes=data.get('notes', []),
        )

    def format_text(self, report: DailyDiffReport) -> str:
        """生成人类可读的文字摘要。"""
        lines = [
            f'=== 每日对比报告 {report.report_date} ===',
            f'生成时间：{report.generated_at}',
            f'',
            f'实盘信号数：{report.n_live_signals}',
            f'回测信号数：{report.n_bt_signals}',
            f'方向一致数：{report.n_matches}',
            f'不一致数：  {report.n_mismatches}',
            f'一致率：    {report.consistency_pct:.1%}',
            f'健康状态：  {"✓ PASS" if report.is_healthy() else "✗ FAIL"}',
        ]

        if report.mismatches:
            lines.append('')
            lines.append('--- 不一致明细 ---')
            for m in report.mismatches:
                tag = {
                    'direction_mismatch': '[方向不一致]',
                    'bt_only':            '[回测独有]',
                    'live_only':          '[实盘独有]',
                }.get(m.mismatch_type, f'[{m.mismatch_type}]')
                lines.append(f'  {tag} {m.symbol}: {m.detail}')

        if report.notes:
            lines.append('')
            lines.append('--- 诊断备注 ---')
            for note in report.notes:
                lines.append(f'  ! {note}')

        return '\n'.join(lines)

    def list_reports(self) -> List[Tuple[date, str]]:
        """
        列出所有已保存报告。

        Returns
        -------
        [(report_date, file_path), ...] 按日期升序
        """
        results = []
        for fname in os.listdir(self.reports_dir):
            if fname.startswith('daily_bt_live_diff_') and fname.endswith('.json'):
                date_str = fname[len('daily_bt_live_diff_'):-len('.json')]
                try:
                    d = date.fromisoformat(date_str)
                    results.append((d, os.path.join(self.reports_dir, fname)))
                except ValueError:
                    continue
        return sorted(results, key=lambda x: x[0])

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_live_signals(live_results: list) -> List[LiveSignalRecord]:
        """从 RunResult 列表提取 LiveSignalRecord。"""
        records = []
        for r in live_results:
            # RunResult duck-typing：不 import 避免循环依赖
            records.append(LiveSignalRecord(
                symbol=str(r.symbol),
                timestamp=r.timestamp.isoformat(timespec='seconds'),
                action=str(r.action),
                combined_score=float(
                    (r.pipeline_result.combined_score if r.pipeline_result else 0.0)
                    if hasattr(r, 'pipeline_result') else 0.0
                ),
                reason=str(r.reason),
            ))
        return records

    @staticmethod
    def _extract_bt_signals(bt_trades: list, report_date: date) -> List[BtSignalRecord]:
        """从 TradeRecord 列表提取指定日期的 BtSignalRecord。"""
        records = []
        for t in bt_trades:
            # TradeRecord duck-typing
            trade_dt = t.timestamp
            if hasattr(trade_dt, 'date'):
                trade_dt = trade_dt.date()
            if trade_dt != report_date:
                continue
            records.append(BtSignalRecord(
                symbol=str(t.symbol),
                trade_date=report_date.isoformat(),
                direction=str(t.direction),
                price=float(t.price),
                shares=int(t.shares),
                signal_reason=str(getattr(t, 'signal_reason', '')),
                signal_strength=float(getattr(t, 'signal_strength', 0.0)),
            ))
        return records
