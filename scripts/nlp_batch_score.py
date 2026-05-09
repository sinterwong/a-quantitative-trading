"""
scripts/nlp_batch_score.py — NLP 因子批量预计算（P1-9）

职责：
  1. 从 Backend API 收集需要的标的（默认 watchlist+positions）
  2. 对每个标的，按日期范围调用 NewsSentimentFactor._get_daily_score
     批量获取/打分（自动复用 4h 新闻缓存 + 24h 评分缓存）
  3. 输出到 outputs/nlp_sentiment/{symbol}.parquet（列：date, score）
  4. NewsSentimentFactor.evaluate 优先读取此 Parquet（无需运行时调 API）

调用入口：
  - 每日 06:00 cron / Scheduler
  - 命令行：python scripts/nlp_batch_score.py [--days 30] [--symbols A.SH B.SH]

运行前提：
  - 设置环境变量 ANTHROPIC_API_KEY（无则全零，仅生成空 parquet）
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))

logger = logging.getLogger('scripts.nlp_batch_score')


def _fetch_symbols(api_port: int) -> List[str]:
    """从 Backend 收集需要预计算情感的标的（watchlist + positions）。"""
    import urllib.request
    base = f'http://127.0.0.1:{api_port}'
    syms: set = set()
    for path in ('/watchlist', '/positions'):
        try:
            with urllib.request.urlopen(f'{base}{path}', timeout=5) as r:
                data = json.loads(r.read())
            items = data.get('watchlist') or data.get('positions') or []
            for it in items:
                sym = it.get('symbol')
                if sym:
                    syms.add(sym)
        except Exception as exc:
            logger.warning('fetch %s failed: %s', path, exc)
    return sorted(syms)


def _score_one_symbol(
    symbol: str, dates: List[str], use_api: bool,
) -> pd.Series:
    """对单个标的的多个日期跑 _get_daily_score。"""
    from core.factors.nlp import NewsSentimentFactor

    factor = NewsSentimentFactor(symbol=symbol, use_api=use_api)
    scores: Dict[pd.Timestamp, float] = {}
    for d in dates:
        try:
            score = factor._get_daily_score(d) if use_api else 0.0
        except Exception as exc:
            logger.warning('%s %s score error: %s', symbol, d, exc)
            score = 0.0
        scores[pd.Timestamp(d)] = float(score)
    return pd.Series(scores, name='score').sort_index()


def _write_parquet(symbol: str, series: pd.Series,
                    output_dir: Optional[Path] = None) -> Path:
    out = output_dir or (PROJ_ROOT / 'outputs' / 'nlp_sentiment')
    out.mkdir(parents=True, exist_ok=True)
    path = out / f'{symbol}.parquet'

    df = series.rename('score').to_frame()
    df.index.name = 'date'

    # 如果已存在 Parquet，合并历史（避免重复 API 调用浪费）
    if path.exists():
        try:
            old = pd.read_parquet(path)
            if 'date' in old.columns:
                old = old.set_index('date')
            old.index = pd.to_datetime(old.index)
            combined = pd.concat([old['score'], df['score']])
            combined = combined[~combined.index.duplicated(keep='last')]
            df = combined.sort_index().to_frame(name='score')
            df.index.name = 'date'
        except Exception as exc:
            logger.warning('merge with existing parquet failed: %s', exc)

    df.reset_index().to_parquet(path, index=False)
    return path


def run_batch(
    symbols: List[str], days: int = 30,
    use_api: bool = True,
    output_dir: Optional[Path] = None,
) -> Dict[str, str]:
    """
    批量预计算情感得分。

    Returns
    -------
    {symbol: parquet_path_str}
    """
    end = date.today()
    start = end - timedelta(days=days)
    dates = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:   # 周一-周五
            dates.append(cur.isoformat())
        cur += timedelta(days=1)

    if not symbols:
        logger.warning('no symbols to score')
        return {}

    result: Dict[str, str] = {}
    for sym in symbols:
        logger.info('scoring %s over %d dates', sym, len(dates))
        try:
            scores = _score_one_symbol(sym, dates, use_api=use_api)
            path = _write_parquet(sym, scores, output_dir=output_dir)
            result[sym] = str(path)
        except Exception as exc:
            logger.exception('symbol %s failed: %s', sym, exc)
            result[sym] = f'ERROR: {exc}'

    return result


def main():
    parser = argparse.ArgumentParser(description='NLP 情感批量预计算')
    parser.add_argument('--days', type=int, default=30,
                        help='回溯天数（默认 30 天）')
    parser.add_argument('--symbols', nargs='*', default=None,
                        help='指定标的（默认 watchlist+positions）')
    parser.add_argument('--api-port', type=int, default=5555)
    parser.add_argument('--no-api', action='store_true',
                        help='不调用 Claude API（仅复用本地缓存或写零）')
    parser.add_argument('--output-dir', type=str, default='')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    )

    symbols = args.symbols or _fetch_symbols(args.api_port)
    output_dir = Path(args.output_dir) if args.output_dir else None

    result = run_batch(
        symbols=symbols, days=args.days,
        use_api=not args.no_api, output_dir=output_dir,
    )
    print(json.dumps({
        'date': date.today().isoformat(),
        'n_symbols': len(result),
        'paths': result,
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
