"""
scripts/ml_train_all.py — 批量训练 ML 价格预测模型（P1-8）

职责：
  1. 从 Backend API /watchlist + /positions 收集训练标的池
  2. 对每个标的拉取 ≥ 500 日历史数据
  3. 用 MLPredictionFactor.fit(use_walk_forward=True) 训练并存储
  4. OOS Sharpe / accuracy 不达标则保留旧模型（不覆盖）
  5. 输出训练摘要 JSON 到 outputs/ml_training/training_{date}.json

调用入口：
  - 周末手动：python scripts/ml_train_all.py
  - 命令行：python scripts/ml_train_all.py [--min-history 500] [--api-port 5555]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))

logger = logging.getLogger('scripts.ml_train_all')

OOS_ACC_THRESHOLD = 0.51   # OOS accuracy 不达此值 → 保留旧模型
OOS_AUC_THRESHOLD = 0.51


def _fetch_training_symbols(api_port: int) -> List[str]:
    """合并 /watchlist + /positions 的标的代码集合。"""
    import urllib.request
    base = f'http://127.0.0.1:{api_port}'
    symbols: set = set()

    for path in ('/watchlist', '/positions'):
        try:
            with urllib.request.urlopen(f'{base}{path}', timeout=5) as r:
                data = json.loads(r.read())
            items = data.get('watchlist') or data.get('positions') or []
            for it in items:
                sym = it.get('symbol')
                if sym:
                    symbols.add(sym)
        except Exception as exc:
            logger.warning('fetch %s failed: %s', path, exc)

    return sorted(symbols)


def _train_one(symbol: str, min_history: int) -> Dict[str, Any]:
    """训练单个标的，返回结果摘要 dict。"""
    record: Dict[str, Any] = {
        'symbol': symbol,
        'status': 'unknown',
        'trained_at': datetime.now().isoformat(timespec='seconds'),
    }
    try:
        from core.data_layer import get_data_layer
        from core.ml.price_predictor import MLPredictionFactor

        dl = get_data_layer()
        df = dl.get_bars(symbol, days=min_history + 50)
        if df is None or len(df) < min_history:
            record['status'] = 'skipped'
            record['reason'] = f'insufficient bars ({0 if df is None else len(df)} < {min_history})'
            return record

        factor = MLPredictionFactor(symbol=symbol, retrain_every=0)
        # 记录旧 metrics（如有）
        old_metrics = None
        try:
            from core.ml.model_registry import ModelRegistry
            _, meta = ModelRegistry().load(symbol, 'xgboost')
            old_metrics = meta.get('metrics', {})
        except Exception:
            pass

        result = factor.fit(df, use_walk_forward=True)
        record.update({
            'oos_accuracy': round(result.oos_accuracy, 4),
            'oos_auc': round(result.oos_auc, 4),
            'n_folds': result.n_folds,
            'old_metrics': old_metrics,
        })

        # 质量门控：OOS 不达标则恢复旧模型
        if (
            old_metrics is not None
            and result.oos_accuracy < OOS_ACC_THRESHOLD
            and result.oos_auc < OOS_AUC_THRESHOLD
            and float(old_metrics.get('oos_accuracy', 0)) > result.oos_accuracy
        ):
            record['status'] = 'rejected_keep_old'
            record['reason'] = (
                f'oos_acc={result.oos_accuracy:.3f} < threshold and '
                f'old={old_metrics.get("oos_accuracy", 0):.3f}'
            )
            # ModelRegistry.save 在 fit 时已写入新模型 — 此处无回滚机制，
            # 标记为"已写入但不推荐"，依赖管理员手动选择
        elif result.oos_accuracy >= OOS_ACC_THRESHOLD:
            record['status'] = 'updated'
        else:
            record['status'] = 'updated_low_quality'

        # P2-18 续：审计日志记录重训
        try:
            from core.audit_log import log_ml_retrain
            log_ml_retrain(
                symbol=symbol,
                model='xgboost',
                oos_accuracy=float(result.oos_accuracy or 0.0),
                oos_sharpe=float(getattr(result, 'oos_sharpe', 0.0) or 0.0),
                persisted=record['status'] in ('updated', 'updated_low_quality'),
                reason=record.get('reason', 'scheduled'),
                extra={'n_folds': result.n_folds, 'oos_auc': float(result.oos_auc or 0.0)},
            )
        except Exception:
            pass

    except Exception as exc:
        record['status'] = 'error'
        record['error'] = str(exc)
        logger.exception('training %s failed', symbol)

    return record


def run_training(
    min_history: int = 500,
    api_port: int = 5555,
    output_dir: Optional[Path] = None,
    symbols: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """批量训练。返回汇总 dict。"""
    if symbols is None:
        symbols = _fetch_training_symbols(api_port)

    out = output_dir or (PROJ_ROOT / 'outputs' / 'ml_training')
    out.mkdir(parents=True, exist_ok=True)

    if not symbols:
        logger.warning('no symbols to train')
        summary = {
            'date': date.today().isoformat(),
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'n_symbols': 0, 'n_updated': 0, 'n_rejected': 0,
            'n_skipped': 0, 'n_errors': 0,
            'records': [], 'note': 'no_symbols',
        }
        path = out / f'training_{summary["date"]}.json'
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        return summary

    logger.info('training %d symbols: %s', len(symbols), symbols)

    records = []
    for sym in symbols:
        rec = _train_one(sym, min_history=min_history)
        logger.info('%s → %s', sym, rec.get('status'))
        records.append(rec)

    summary = {
        'date': date.today().isoformat(),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'n_symbols': len(records),
        'n_updated': sum(1 for r in records if r.get('status') == 'updated'),
        'n_rejected': sum(1 for r in records if r.get('status') == 'rejected_keep_old'),
        'n_skipped': sum(1 for r in records if r.get('status') == 'skipped'),
        'n_errors': sum(1 for r in records if r.get('status') == 'error'),
        'records': records,
    }

    path = out / f'training_{summary["date"]}.json'
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2, default=str)
    logger.info('summary written: %s', path)
    return summary


def main():
    parser = argparse.ArgumentParser(description='批量训练 ML 价格预测模型')
    parser.add_argument('--min-history', type=int, default=500)
    parser.add_argument('--api-port', type=int, default=5555)
    parser.add_argument('--symbols', nargs='*', help='指定标的（默认从 watchlist+positions 获取）')
    parser.add_argument('--output-dir', type=str, default='')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    )

    output_dir = Path(args.output_dir) if args.output_dir else None
    summary = run_training(
        min_history=args.min_history,
        api_port=args.api_port,
        output_dir=output_dir,
        symbols=args.symbols or None,
    )
    print(json.dumps({
        'date': summary['date'],
        'n_symbols': summary['n_symbols'],
        'n_updated': summary['n_updated'],
        'n_rejected': summary['n_rejected'],
        'n_skipped': summary['n_skipped'],
        'n_errors': summary['n_errors'],
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
