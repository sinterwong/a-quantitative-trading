"""
scripts/daily_tca.py — 每日 TCA 报告（P1-12）

职责：
  1. 从 Backend API /trades 拉取近 N 日成交记录
  2. 用 TCAAnalyzer 计算 IS / 显性成本 / 按 Regime/symbol/hour 分组
  3. 输出 outputs/tca_daily/tca_{YYYY-MM-DD}.json
  4. 每月 1 日额外跑月度报告 + 反馈调整 ImpactEstimator 系数到
     outputs/tca_calibration.json（ImpactEstimator.load_from_config 优先读取）
  5. avg_is_bps 大幅偏离基线时推送 AlertManager.send_warning

调用入口：
  - Scheduler 每日 15:45 自动触发
  - 命令行：python scripts/daily_tca.py [--days 1] [--api-port 5555]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))

logger = logging.getLogger('scripts.daily_tca')


# ---------------------------------------------------------------------------
# 数据获取
# ---------------------------------------------------------------------------

def _fetch_trades(api_port: int = 5555, limit: int = 200) -> List[Dict]:
    """从 Backend /trades 拉取近期成交记录。"""
    import urllib.request
    base = f'http://127.0.0.1:{api_port}'
    try:
        with urllib.request.urlopen(f'{base}/trades?limit={limit}', timeout=5) as r:
            data = json.loads(r.read())
        return data.get('trades', []) or []
    except Exception as exc:
        logger.warning('failed to fetch /trades: %s', exc)
        return []


def _filter_trades_by_date(trades: List[Dict], target_date: date) -> List[Dict]:
    """筛选指定日期内的成交。"""
    out: List[Dict] = []
    for t in trades:
        ts = t.get('executed_at') or t.get('timestamp') or ''
        if str(ts)[:10] == target_date.isoformat():
            out.append(t)
    return out


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------

def _summarize(report) -> Dict[str, Any]:
    """把 TCAReport 转 JSON-friendly dict。"""
    return {
        'n_trades': report.n_trades,
        'avg_is_bps': report.avg_is_bps,
        'median_is_bps': report.median_is_bps,
        'p95_is_bps': report.p95_is_bps,
        'avg_total_cost_bps': report.avg_total_cost_bps,
        'recommended_slippage_bps': report.recommended_slippage_bps,
        'by_symbol': report.by_symbol,
        'by_direction': report.by_direction,
        'by_regime': report.by_regime,
        'by_hour': {str(k): v for k, v in report.by_hour.items()},
        'monthly': report.monthly,
    }


def _write_daily(summary: Dict[str, Any], target_date: date,
                  output_dir: Optional[Path] = None) -> Path:
    out = output_dir or (PROJ_ROOT / 'outputs' / 'tca_daily')
    out.mkdir(parents=True, exist_ok=True)
    path = out / f'tca_{target_date.isoformat()}.json'
    payload = {
        'date': target_date.isoformat(),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        **summary,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    return path


# ---------------------------------------------------------------------------
# 反馈闭环：根据滚动 IS 调整 ImpactEstimator 系数
# ---------------------------------------------------------------------------

def _read_calibration() -> Dict[str, Any]:
    path = PROJ_ROOT / 'outputs' / 'tca_calibration.json'
    if not path.exists():
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _write_calibration(perm_coeff: float, temp_coeff: float, source: Dict) -> Path:
    path = PROJ_ROOT / 'outputs' / 'tca_calibration.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        'updated_at': datetime.now().isoformat(timespec='seconds'),
        'impact_permanent_coeff': round(perm_coeff, 3),
        'impact_temporary_coeff': round(temp_coeff, 3),
        'source': source,
    }
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def _calibrate_impact_coefficients(
    rolling_avg_is_bps: float, baseline_avg_is_bps: float,
) -> Optional[Dict[str, float]]:
    """
    简单反馈：实际 IS / 基线 IS 比例 → 同比例缩放冲击系数。

    rolling_avg_is_bps : 滚动 20 日实际 IS（基点）
    baseline_avg_is_bps : 当前配置下 ImpactEstimator 估算的"目标 IS"

    若实际 IS 比基线高 > 50%（系数偏低），按比例放大；
    若低 > 50%（系数偏高），按比例缩小；
    其余保留不动（防止抖动）。
    """
    if baseline_avg_is_bps <= 0 or rolling_avg_is_bps <= 0:
        return None

    ratio = rolling_avg_is_bps / baseline_avg_is_bps
    if 0.67 <= ratio <= 1.5:
        return None  # 处于合理区间，不调整

    from core.execution.impact_estimator import ImpactEstimator
    new_perm = ImpactEstimator.PERMANENT_COEFF * ratio
    new_temp = ImpactEstimator.TEMPORARY_COEFF * ratio
    # clamp 到 [1.0, 50.0] 防止失控
    new_perm = max(1.0, min(50.0, new_perm))
    new_temp = max(1.0, min(50.0, new_temp))

    return {
        'permanent': new_perm,
        'temporary': new_temp,
        'rolling_is_bps': rolling_avg_is_bps,
        'baseline_is_bps': baseline_avg_is_bps,
        'scale_ratio': round(ratio, 3),
    }


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run_report(
    target_date: Optional[date] = None,
    api_port: int = 5555,
    output_dir: Optional[Path] = None,
    enable_alert: bool = True,
    enable_calibration: bool = True,
) -> Dict[str, Any]:
    """生成并保存每日 TCA 报告。返回 summary 字典。"""
    target_date = target_date or date.today()

    # 1. 拉取交易记录（多取 200 条，按日期筛选）
    raw_trades = _fetch_trades(api_port=api_port, limit=200)
    today_trades = _filter_trades_by_date(raw_trades, target_date)
    if not today_trades:
        summary = {
            'n_trades': 0,
            'avg_is_bps': 0.0,
            'recommended_slippage_bps': 5.0,
            'by_symbol': {}, 'by_direction': {},
            'by_regime': {}, 'by_hour': {}, 'monthly': {},
            'note': 'no_trades_today',
        }
        path = _write_daily(summary, target_date, output_dir)
        logger.info('no trades on %s, empty report → %s', target_date, path)
        return summary

    # 2. 跑 TCA 分析
    from core.tca import TCAAnalyzer
    analyzer = TCAAnalyzer.from_trade_dicts(today_trades)
    report = analyzer.analyze()
    summary = _summarize(report)

    # 3. 写文件
    path = _write_daily(summary, target_date, output_dir)
    logger.info('TCA daily → %s | n=%d avg_is=%.2f bps',
                path, report.n_trades, report.avg_is_bps)

    # 4. 月底反馈：每月 1 日基于过去 20 日的 daily TCA 累计调整
    if enable_calibration and target_date.day == 1:
        try:
            _maybe_calibrate(target_date, output_dir)
        except Exception as exc:
            logger.warning('calibration failed: %s', exc)

    # 5. 偏离告警
    if enable_alert and report.n_trades >= 5:
        try:
            _maybe_alert(report)
        except Exception as exc:
            logger.warning('alert failed: %s', exc)

    return summary


def _maybe_calibrate(target_date: date, output_dir: Optional[Path] = None) -> None:
    """读取最近 20 个 daily TCA 文件，做反馈调整。"""
    out = output_dir or (PROJ_ROOT / 'outputs' / 'tca_daily')
    if not out.exists():
        return
    cutoff = target_date - timedelta(days=30)
    is_history: List[float] = []
    for p in sorted(out.glob('tca_*.json')):
        try:
            d = json.loads(p.read_text(encoding='utf-8'))
            ds = d.get('date', '')
            if not ds:
                continue
            f_date = date.fromisoformat(ds)
            if f_date < cutoff:
                continue
            avg = d.get('avg_is_bps')
            n = d.get('n_trades', 0)
            if avg is not None and n >= 1:
                is_history.append(float(avg))
        except Exception:
            continue
    if len(is_history) < 5:
        logger.info('calibration skipped: only %d daily samples', len(is_history))
        return

    rolling = sum(is_history) / len(is_history)
    # 基线：以当前 ImpactEstimator 系数估算"普通中等订单"的 IS
    from core.execution.impact_estimator import ImpactEstimator
    baseline = ImpactEstimator.estimate(
        order_qty=10_000,
        market_daily_vol=1_000_000,   # 假设 1% 参与率
    )

    cal = _calibrate_impact_coefficients(rolling, baseline)
    if cal is None:
        logger.info(
            'calibration: rolling_is=%.2f baseline=%.2f within tolerance',
            rolling, baseline,
        )
        return

    _write_calibration(cal['permanent'], cal['temporary'], cal)
    logger.info(
        'calibration: scale=%.2f → perm=%.2f temp=%.2f (rolling_is=%.2f baseline=%.2f)',
        cal['scale_ratio'], cal['permanent'], cal['temporary'],
        cal['rolling_is_bps'], cal['baseline_is_bps'],
    )


def _maybe_alert(report) -> None:
    """avg_is_bps 超过 30 bps（明显异常）→ 推送 WARNING。"""
    if report.avg_is_bps <= 30:
        return
    try:
        from core.alerting import get_alert_manager
        msg = (
            f'⚠️ TCA 异常：今日成交 {report.n_trades} 笔，'
            f'平均 IS={report.avg_is_bps:.2f} bps，超过 30 bps 阈值。\n'
            f'建议 slippage 参数：{report.recommended_slippage_bps:.1f} bps\n'
            f'P95 IS：{report.p95_is_bps:.2f} bps'
        )
        get_alert_manager().send_warning(msg)
    except Exception as exc:
        logger.warning('alert send failed: %s', exc)


def main():
    parser = argparse.ArgumentParser(description='每日 TCA 报告')
    parser.add_argument('--date', type=str, default='', help='YYYY-MM-DD（默认今天）')
    parser.add_argument('--api-port', type=int, default=5555)
    parser.add_argument('--no-alert', action='store_true')
    parser.add_argument('--no-calibration', action='store_true')
    parser.add_argument('--output-dir', type=str, default='')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    )

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    output_dir = Path(args.output_dir) if args.output_dir else None
    summary = run_report(
        target_date=target_date,
        api_port=args.api_port,
        output_dir=output_dir,
        enable_alert=not args.no_alert,
        enable_calibration=not args.no_calibration,
    )
    print(json.dumps({
        'date': target_date.isoformat(),
        'n_trades': summary.get('n_trades'),
        'avg_is_bps': summary.get('avg_is_bps'),
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
