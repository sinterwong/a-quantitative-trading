"""
core/level2_quality.py — Level 2 数据完整性验证框架（P2-B）

功能：
  - 采集并存储 5 档盘口快照（复用 core/level2.py 的 Level2DataSource）
  - 按字段逐一检验完整率（目标 > 95%）
  - 生成 Markdown 质量报告 outputs/level2_quality_report.md

两种使用场景：
  1. 在线采集模式（生产）：
       collector = Level2QualityCollector(['600519.SH', '000858.SZ'])
       collector.start(interval=30)    # 每 30 秒采集一次
       # 运行 5 个交易日后：
       collector.stop()
       reporter = Level2QualityReporter(collector.db_path)
       report = reporter.generate()
       report.save()

  2. 离线分析模式（已有采集数据）：
       reporter = Level2QualityReporter('data/level2_snapshots.db')
       report = reporter.generate(days=5)
       report.save()
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'data'
)
_OUTPUTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), 'outputs'
)
os.makedirs(_DATA_DIR, exist_ok=True)
os.makedirs(_OUTPUTS_DIR, exist_ok=True)

_DEFAULT_DB = os.path.join(_DATA_DIR, 'level2_snapshots.db')

# 需要检验完整率的字段
_REQUIRED_FIELDS = [
    'bid_price_1', 'bid_vol_1', 'ask_price_1', 'ask_vol_1',
    'bid_price_2', 'bid_vol_2', 'ask_price_2', 'ask_vol_2',
    'bid_price_3', 'bid_vol_3', 'ask_price_3', 'ask_vol_3',
    'bid_price_4', 'bid_vol_4', 'ask_price_4', 'ask_vol_4',
    'bid_price_5', 'bid_vol_5', 'ask_price_5', 'ask_vol_5',
    'last_price', 'volume', 'amount',
]


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class FieldQuality:
    """单字段完整率统计。"""
    field_name: str
    total: int
    valid: int             # 非 NULL 且非 0
    completeness: float    # valid / total
    passed: bool           # completeness >= threshold


@dataclass
class SymbolQuality:
    """单标的质量统计。"""
    symbol: str
    n_snapshots: int
    trading_days: int
    avg_interval_sec: float       # 平均采集间隔
    field_stats: List[FieldQuality] = field(default_factory=list)
    overall_completeness: float = 0.0
    passed: bool = False


@dataclass
class Level2QualityReport:
    """Level 2 数据质量报告。"""
    generated_at: str
    db_path: str
    days_analyzed: int
    threshold: float               # 完整率阈值（默认 0.95）
    symbols: List[SymbolQuality] = field(default_factory=list)
    overall_completeness: float = 0.0
    passed: bool = False
    notes: List[str] = field(default_factory=list)

    def save(self, md_path: Optional[str] = None, json_path: Optional[str] = None) -> str:
        """保存 Markdown 报告（+ 可选 JSON）。"""
        if md_path is None:
            md_path = os.path.join(_OUTPUTS_DIR, 'level2_quality_report.md')

        lines = [
            f'# Level 2 数据质量报告',
            f'',
            f'生成时间：{self.generated_at}',
            f'数据源：{self.db_path}',
            f'分析天数：{self.days_analyzed}',
            f'完整率阈值：{self.threshold:.0%}',
            f'',
            f'## 总体结论',
            f'',
            f'整体完整率：**{self.overall_completeness:.1%}** '
            f'{"✓ PASS" if self.passed else "✗ FAIL"}',
            f'',
        ]

        for s in self.symbols:
            lines += [
                f'## {s.symbol}',
                f'',
                f'- 快照数：{s.n_snapshots}',
                f'- 交易日数：{s.trading_days}',
                f'- 平均间隔：{s.avg_interval_sec:.1f} 秒',
                f'- 整体完整率：{s.overall_completeness:.1%} '
                f'{"✓" if s.passed else "✗"}',
                f'',
                f'| 字段 | 总数 | 有效 | 完整率 | 状态 |',
                f'|------|------|------|--------|------|',
            ]
            for fq in s.field_stats:
                status = '✓' if fq.passed else '✗'
                lines.append(
                    f'| {fq.field_name} | {fq.total} | {fq.valid} '
                    f'| {fq.completeness:.1%} | {status} |'
                )
            lines.append('')

        if self.notes:
            lines += ['## 诊断备注', '']
            for note in self.notes:
                lines.append(f'- {note}')

        with open(md_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))

        if json_path:
            with open(json_path, 'w', encoding='utf-8') as f:
                json.dump({
                    'generated_at': self.generated_at,
                    'overall_completeness': self.overall_completeness,
                    'passed': self.passed,
                    'symbols': [
                        {
                            'symbol': s.symbol,
                            'n_snapshots': s.n_snapshots,
                            'overall_completeness': s.overall_completeness,
                            'passed': s.passed,
                        }
                        for s in self.symbols
                    ],
                }, f, ensure_ascii=False, indent=2)

        return md_path

    def print_summary(self) -> None:
        status = 'PASS' if self.passed else 'FAIL'
        print(f'=== Level2 数据质量报告 [{status}] ===')
        print(f'整体完整率：{self.overall_completeness:.1%} (阈值 {self.threshold:.0%})')
        print(f'分析天数：{self.days_analyzed} | 标的数：{len(self.symbols)}')
        print()
        print(f'{"标的":<14} {"快照数":>8} {"完整率":>8} {"状态":>6}')
        print('-' * 42)
        for s in self.symbols:
            print(f'{s.symbol:<14} {s.n_snapshots:>8} '
                  f'{s.overall_completeness:>7.1%} {"✓" if s.passed else "✗":>6}')
        if self.notes:
            print()
            for note in self.notes:
                print(f'  * {note}')


# ---------------------------------------------------------------------------
# Level2QualityCollector — 在线采集
# ---------------------------------------------------------------------------

class Level2QualityCollector:
    """
    Level 2 盘口快照采集器。

    将 Level2DataSource 的快照持久化到 SQLite，
    供 Level2QualityReporter 离线分析。

    用法（生产模式）：
        collector = Level2QualityCollector(['600519.SH', '000858.SZ'])
        collector.start(interval=30)
        # ... 运行若干交易日后 ...
        collector.stop()
    """

    def __init__(
        self,
        symbols: List[str],
        db_path: str = _DEFAULT_DB,
    ) -> None:
        self.symbols = symbols
        self.db_path = db_path
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._init_db()

    def start(self, interval: int = 30) -> None:
        """启动后台采集线程，每 interval 秒采集一次。"""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop, args=(interval,), daemon=True
        )
        self._thread.start()
        logger.info('[Level2QualityCollector] Started: %d symbols, interval=%ds',
                    len(self.symbols), interval)

    def stop(self) -> None:
        """停止采集。"""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info('[Level2QualityCollector] Stopped.')

    def collect_once(self) -> int:
        """手动触发一次采集，返回成功采集的标的数。"""
        count = 0
        for symbol in self.symbols:
            try:
                snapshot = self._fetch_snapshot(symbol)
                if snapshot:
                    self._save_snapshot(snapshot)
                    count += 1
            except Exception as e:
                logger.debug('[Level2QualityCollector] %s error: %s', symbol, e)
        return count

    @property
    def n_snapshots(self) -> Dict[str, int]:
        """返回各标的已采集快照数。"""
        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.cursor()
            cur.execute(
                'SELECT symbol, COUNT(*) FROM l2_snapshots GROUP BY symbol'
            )
            return dict(cur.fetchall())
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _loop(self, interval: int) -> None:
        while not self._stop_event.is_set():
            try:
                self.collect_once()
            except Exception as e:
                logger.error('[Level2QualityCollector] loop error: %s', e)
            self._stop_event.wait(interval)

    def _init_db(self) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS l2_snapshots (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol      TEXT    NOT NULL,
                    ts          TEXT    NOT NULL,
                    last_price  REAL,
                    volume      REAL,
                    amount      REAL,
                    bid_price_1 REAL, bid_vol_1 INTEGER,
                    ask_price_1 REAL, ask_vol_1 INTEGER,
                    bid_price_2 REAL, bid_vol_2 INTEGER,
                    ask_price_2 REAL, ask_vol_2 INTEGER,
                    bid_price_3 REAL, bid_vol_3 INTEGER,
                    ask_price_3 REAL, ask_vol_3 INTEGER,
                    bid_price_4 REAL, bid_vol_4 INTEGER,
                    ask_price_4 REAL, ask_vol_4 INTEGER,
                    bid_price_5 REAL, bid_vol_5 INTEGER,
                    ask_price_5 REAL, ask_vol_5 INTEGER,
                    raw_json    TEXT
                )
            ''')
            conn.execute(
                'CREATE INDEX IF NOT EXISTS idx_l2_sym_ts ON l2_snapshots (symbol, ts)'
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _fetch_snapshot(symbol: str) -> Optional[Dict]:
        """从 Level2DataSource 获取快照。"""
        try:
            from core.level2 import Level2DataSource
            src = Level2DataSource(symbol)
            ob = src.fetch()
            if ob is None:
                return None

            result = {
                'symbol': symbol,
                'ts': datetime.now().isoformat(timespec='seconds'),
                'last_price': ob.last_price,
                'volume': ob.volume,
                'amount': ob.amount,
            }
            for i, (price, vol) in enumerate(ob.bids[:5], 1):
                result[f'bid_price_{i}'] = price
                result[f'bid_vol_{i}'] = vol
            for i, (price, vol) in enumerate(ob.asks[:5], 1):
                result[f'ask_price_{i}'] = price
                result[f'ask_vol_{i}'] = vol

            return result
        except Exception:
            return None

    def _save_snapshot(self, snap: Dict) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute('''
                INSERT INTO l2_snapshots
                (symbol, ts, last_price, volume, amount,
                 bid_price_1, bid_vol_1, ask_price_1, ask_vol_1,
                 bid_price_2, bid_vol_2, ask_price_2, ask_vol_2,
                 bid_price_3, bid_vol_3, ask_price_3, ask_vol_3,
                 bid_price_4, bid_vol_4, ask_price_4, ask_vol_4,
                 bid_price_5, bid_vol_5, ask_price_5, ask_vol_5,
                 raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                snap['symbol'], snap['ts'],
                snap.get('last_price'), snap.get('volume'), snap.get('amount'),
                snap.get('bid_price_1'), snap.get('bid_vol_1'),
                snap.get('ask_price_1'), snap.get('ask_vol_1'),
                snap.get('bid_price_2'), snap.get('bid_vol_2'),
                snap.get('ask_price_2'), snap.get('ask_vol_2'),
                snap.get('bid_price_3'), snap.get('bid_vol_3'),
                snap.get('ask_price_3'), snap.get('ask_vol_3'),
                snap.get('bid_price_4'), snap.get('bid_vol_4'),
                snap.get('ask_price_4'), snap.get('ask_vol_4'),
                snap.get('bid_price_5'), snap.get('bid_vol_5'),
                snap.get('ask_price_5'), snap.get('ask_vol_5'),
                json.dumps(snap, ensure_ascii=False),
            ))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Level2QualityReporter — 离线分析
# ---------------------------------------------------------------------------

class Level2QualityReporter:
    """
    Level 2 快照质量分析器。

    从 SQLite 数据库读取已采集快照，按字段统计完整率。

    用法：
        reporter = Level2QualityReporter('data/level2_snapshots.db')
        report = reporter.generate(days=5, threshold=0.95)
        report.save()
        report.print_summary()
    """

    def __init__(self, db_path: str = _DEFAULT_DB) -> None:
        self.db_path = db_path

    def generate(
        self,
        days: int = 5,
        threshold: float = 0.95,
        symbols: Optional[List[str]] = None,
    ) -> Level2QualityReport:
        """
        生成质量报告。

        Parameters
        ----------
        days      : 分析最近 N 天的数据
        threshold : 字段完整率合格阈值
        symbols   : 限定标的列表（None = 全部）
        """
        if not os.path.exists(self.db_path):
            return Level2QualityReport(
                generated_at=datetime.now().isoformat(timespec='seconds'),
                db_path=self.db_path,
                days_analyzed=days,
                threshold=threshold,
                passed=False,
                notes=[f'数据库不存在：{self.db_path}，请先运行 Level2QualityCollector 采集数据'],
            )

        since = (datetime.now() - timedelta(days=days)).isoformat()

        conn = sqlite3.connect(self.db_path)
        try:
            # 获取标的列表
            cur = conn.cursor()
            if symbols:
                placeholders = ','.join('?' * len(symbols))
                cur.execute(
                    f"SELECT DISTINCT symbol FROM l2_snapshots "
                    f"WHERE ts >= ? AND symbol IN ({placeholders})",
                    [since] + symbols,
                )
            else:
                cur.execute(
                    'SELECT DISTINCT symbol FROM l2_snapshots WHERE ts >= ?', (since,)
                )
            all_symbols = [row[0] for row in cur.fetchall()]

            if not all_symbols:
                return Level2QualityReport(
                    generated_at=datetime.now().isoformat(timespec='seconds'),
                    db_path=self.db_path,
                    days_analyzed=days,
                    threshold=threshold,
                    passed=False,
                    notes=[f'最近 {days} 天内无数据，请检查采集是否正常运行'],
                )

            symbol_results: List[SymbolQuality] = []
            for sym in all_symbols:
                sq = self._analyze_symbol(conn, sym, since, threshold)
                symbol_results.append(sq)

        finally:
            conn.close()

        # 汇总
        all_compl = [s.overall_completeness for s in symbol_results if s.n_snapshots > 0]
        overall = sum(all_compl) / len(all_compl) if all_compl else 0.0
        passed = overall >= threshold

        notes: List[str] = []
        if not passed:
            notes.append(f'整体完整率 {overall:.1%} < {threshold:.0%}，需检查数据源稳定性')
        failed_syms = [s.symbol for s in symbol_results if not s.passed]
        if failed_syms:
            notes.append(f'以下标的完整率不达标：{failed_syms}')

        total_days = {s.trading_days for s in symbol_results if s.n_snapshots > 0}
        if total_days and max(total_days) < 5:
            notes.append(f'数据仅覆盖 {max(total_days)} 个交易日（推荐至少 5 个）')

        return Level2QualityReport(
            generated_at=datetime.now().isoformat(timespec='seconds'),
            db_path=self.db_path,
            days_analyzed=days,
            threshold=threshold,
            symbols=symbol_results,
            overall_completeness=round(overall, 4),
            passed=passed,
            notes=notes,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _analyze_symbol(
        conn: sqlite3.Connection,
        symbol: str,
        since: str,
        threshold: float,
    ) -> SymbolQuality:
        """对单个标的分析快照质量。"""
        cur = conn.cursor()
        cur.execute(
            'SELECT ts FROM l2_snapshots WHERE symbol=? AND ts>=? ORDER BY ts',
            (symbol, since),
        )
        timestamps = [row[0] for row in cur.fetchall()]
        n = len(timestamps)

        if n == 0:
            return SymbolQuality(
                symbol=symbol, n_snapshots=0, trading_days=0,
                avg_interval_sec=0, passed=False,
            )

        # 计算平均间隔
        if n > 1:
            from datetime import datetime as dt
            times = [dt.fromisoformat(t) for t in timestamps]
            intervals = [(times[i + 1] - times[i]).total_seconds() for i in range(n - 1)]
            avg_interval = sum(intervals) / len(intervals)
        else:
            avg_interval = 0.0

        # 交易日数
        trading_days = len({t[:10] for t in timestamps})

        # 各字段完整率
        field_stats: List[FieldQuality] = []
        completeness_vals: List[float] = []

        for fname in _REQUIRED_FIELDS:
            try:
                cur.execute(
                    f'SELECT COUNT(*) FROM l2_snapshots WHERE symbol=? AND ts>=?',
                    (symbol, since),
                )
                total = cur.fetchone()[0]
                cur.execute(
                    f'SELECT COUNT(*) FROM l2_snapshots '
                    f'WHERE symbol=? AND ts>=? AND {fname} IS NOT NULL AND {fname} != 0',
                    (symbol, since),
                )
                valid = cur.fetchone()[0]
                compl = valid / total if total > 0 else 0.0
                fq = FieldQuality(
                    field_name=fname,
                    total=total,
                    valid=valid,
                    completeness=round(compl, 4),
                    passed=compl >= threshold,
                )
                field_stats.append(fq)
                completeness_vals.append(compl)
            except Exception:
                pass

        overall_compl = (
            sum(completeness_vals) / len(completeness_vals)
            if completeness_vals else 0.0
        )

        return SymbolQuality(
            symbol=symbol,
            n_snapshots=n,
            trading_days=trading_days,
            avg_interval_sec=round(avg_interval, 1),
            field_stats=field_stats,
            overall_completeness=round(overall_compl, 4),
            passed=overall_compl >= threshold,
        )
