"""
scripts/daily_risk_report.py — 每日盘后组合风险报告（P0-5）

职责：
  1. 从 Backend API 读取当前持仓与权益快照
  2. 用 DataLayer 拉取每个标的的历史日收益率序列（默认 252 日）
  3. 调用 PortfolioRiskChecker.check_cvar() 计算 CVaR(95%)
  4. 调用 MonteCarloStressTest.run() 做 10000 次蒙特卡洛模拟（21 日 horizon）
  5. 输出 JSON 到 outputs/risk_daily/risk_{YYYY-MM-DD}.json
  6. CVaR 或 ES 超限 → AlertManager.send_critical()

调用入口：
  - Scheduler 每日 15:30 自动触发
  - 命令行：python scripts/daily_risk_report.py [--n-sim 10000] [--horizon 21]

输出 JSON 字段（见 _summarize_result）：
  date, equity, positions_count, cvar, var, exp_shortfall,
  prob_loss, max_drawdown_p95, scenarios{base,bear,crash,bull}, breach
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

# 项目根路径
PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))

logger = logging.getLogger('scripts.daily_risk_report')


# ---------------------------------------------------------------------------
# 数据获取
# ---------------------------------------------------------------------------

def _fetch_portfolio_snapshot(api_port: int = 5555) -> Dict[str, Any]:
    """从 Backend API 读取持仓 + 权益。"""
    import urllib.request

    base = f'http://127.0.0.1:{api_port}'
    snapshot: Dict[str, Any] = {
        'positions': [],
        'equity': 0.0,
        'cash': 0.0,
        'position_value': 0.0,
    }

    try:
        with urllib.request.urlopen(f'{base}/portfolio/summary', timeout=5) as r:
            summary = json.loads(r.read())
        snapshot['cash'] = float(summary.get('cash', 0))
        snapshot['position_value'] = float(summary.get('position_value', 0))
        snapshot['equity'] = snapshot['cash'] + snapshot['position_value']
    except Exception as exc:
        logger.warning('failed to fetch /portfolio/summary: %s', exc)

    try:
        with urllib.request.urlopen(f'{base}/positions', timeout=5) as r:
            data = json.loads(r.read())
        snapshot['positions'] = data.get('positions', [])
    except Exception as exc:
        logger.warning('failed to fetch /positions: %s', exc)

    return snapshot


def _fetch_returns_for_symbols(
    symbols: List[str], lookback_days: int = 252,
) -> Dict[str, pd.Series]:
    """用 DataLayer 拉取每个标的的历史日收益率序列。"""
    from core.data_layer import get_data_layer

    dl = get_data_layer()
    returns: Dict[str, pd.Series] = {}
    for sym in symbols:
        try:
            df = dl.get_bars(sym, limit=lookback_days + 5)
            if df is None or len(df) < 30:
                logger.warning('symbol %s: %d bars (skipping)',
                               sym, 0 if df is None else len(df))
                continue
            returns[sym] = df['close'].pct_change().dropna()
        except Exception as exc:
            logger.warning('failed to load bars for %s: %s', sym, exc)
    return returns


def _build_portfolio_returns(
    positions: List[Dict[str, Any]],
    equity: float,
    returns_by_symbol: Dict[str, pd.Series],
) -> Optional[pd.Series]:
    """按持仓权重合成组合每日收益率序列（用于 MC 模拟的输入分布）。"""
    if not positions or equity <= 0 or not returns_by_symbol:
        return None

    weights: Dict[str, float] = {}
    for p in positions:
        sym = p.get('symbol', '')
        shares = float(p.get('shares', 0) or 0)
        price = float(p.get('current_price', 0) or 0)
        if shares <= 0 or price <= 0 or sym not in returns_by_symbol:
            continue
        weights[sym] = (shares * price) / equity

    if not weights:
        return None

    df = pd.DataFrame({s: returns_by_symbol[s] for s in weights}).dropna()
    if df.empty:
        return None

    # 归一化权重（仅对有数据的标的）
    w_sum = sum(weights[s] for s in df.columns)
    if w_sum <= 0:
        return None
    w_vec = np.array([weights[s] / w_sum for s in df.columns])
    portfolio_rets = pd.Series(df.values @ w_vec, index=df.index)
    return portfolio_rets


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------

def _summarize_result(
    snapshot: Dict[str, Any],
    cvar_result,
    mc_result,
    var_limit: float,
    cvar_limit: float,
) -> Dict[str, Any]:
    """汇总输出 JSON。"""
    breach: List[str] = []
    if cvar_result is not None and not cvar_result.passed:
        breach.append(f'CVaR_{cvar_limit*100:.0f}%')

    summary = {
        'date': date.today().isoformat(),
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'equity': round(snapshot.get('equity', 0.0), 2),
        'cash': round(snapshot.get('cash', 0.0), 2),
        'position_value': round(snapshot.get('position_value', 0.0), 2),
        'positions_count': len(snapshot.get('positions', [])),
        'limits': {
            'var_limit': var_limit,
            'cvar_limit': cvar_limit,
        },
        'cvar': None,
        'var': None,
        'monte_carlo': None,
        'breach': breach,
    }

    if cvar_result is not None:
        summary['cvar'] = {
            'level': cvar_result.level,
            'passed': cvar_result.passed,
            'reason': cvar_result.reason,
            'details': cvar_result.details,
        }

    if mc_result is not None:
        summary['monte_carlo'] = {
            'n_simulations': mc_result.n_simulations,
            'horizon_days': mc_result.horizon_days,
            'initial_equity': mc_result.initial_equity,
            'p5_final': mc_result.p5_final,
            'p25_final': mc_result.p25_final,
            'p50_final': mc_result.p50_final,
            'p75_final': mc_result.p75_final,
            'p95_final': mc_result.p95_final,
            'prob_loss': mc_result.prob_loss,
            'expected_shortfall': mc_result.expected_shortfall,
            'max_drawdown_mean': mc_result.max_drawdown_mean,
            'max_drawdown_p95': mc_result.max_drawdown_p95,
            'stress_scenarios': mc_result.stress_scenarios,
        }

    return summary


def _write_report(summary: Dict[str, Any], output_dir: Optional[Path] = None) -> Path:
    """写入 outputs/risk_daily/risk_{date}.json。"""
    output_dir = output_dir or (PROJ_ROOT / 'outputs' / 'risk_daily')
    output_dir.mkdir(parents=True, exist_ok=True)
    fname = f'risk_{summary["date"]}.json'
    path = output_dir / fname
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    logger.info('风险报告已写入 %s', path)
    return path


def _maybe_alert(summary: Dict[str, Any]) -> None:
    """有 breach 时通过 AlertManager 推送 CRITICAL。"""
    if not summary.get('breach'):
        return
    try:
        from core.alerting import get_alert_manager
        msg_lines = [
            '⚠️ 组合风险超限！',
            f'日期: {summary["date"]}',
            f'权益: {summary["equity"]:,.0f}',
            f'持仓: {summary["positions_count"]} 只',
            f'触发项: {", ".join(summary["breach"])}',
        ]
        cvar = summary.get('cvar') or {}
        if cvar.get('reason'):
            msg_lines.append(f'CVaR: {cvar["reason"]}')
        mc = summary.get('monte_carlo') or {}
        if mc:
            msg_lines.append(
                f'MC: ES(95%)={mc["expected_shortfall"]*100:.2f}%, '
                f'P95 回撤={mc["max_drawdown_p95"]*100:.1f}%'
            )
        get_alert_manager().send_critical('\n'.join(msg_lines))
    except Exception as exc:
        logger.error('AlertManager 推送失败: %s', exc)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def run_report(
    n_simulations: int = 10000,
    horizon_days: int = 21,
    lookback_days: int = 252,
    api_port: int = 5555,
    output_dir: Optional[Path] = None,
    enable_alert: bool = True,
) -> Dict[str, Any]:
    """生成并保存每日风险报告。返回 summary 字典。"""
    logger.info('开始生成每日组合风险报告 (n_sim=%d, horizon=%d)',
                n_simulations, horizon_days)

    # 1. 持仓快照
    snapshot = _fetch_portfolio_snapshot(api_port=api_port)
    positions = snapshot['positions']
    equity = snapshot['equity']

    if not positions or equity <= 0:
        logger.info('当前无持仓或权益为 0，仅写空报告')
        summary = {
            'date': date.today().isoformat(),
            'generated_at': datetime.now().isoformat(timespec='seconds'),
            'equity': equity,
            'positions_count': 0,
            'cvar': None,
            'var': None,
            'monte_carlo': None,
            'breach': [],
            'note': 'no_positions',
        }
        _write_report(summary, output_dir)
        return summary

    # 2. 历史收益率
    symbols = [p.get('symbol', '') for p in positions if p.get('symbol')]
    returns_by_symbol = _fetch_returns_for_symbols(symbols, lookback_days)

    # 3. 配置
    try:
        from core.config import load_config
        cfg = load_config()
        var_limit = float(cfg.risk.var_limit)
        cvar_limit = max(var_limit * 1.5, 0.05)  # CVaR 限制略宽于 VaR
    except Exception:
        var_limit = 0.03
        cvar_limit = 0.05

    # 4. CVaR 检查
    cvar_result = None
    try:
        from core.portfolio_risk import PortfolioRiskChecker, PortfolioSnapshot

        weights_map = {}
        for p in positions:
            sym = p.get('symbol', '')
            shares = float(p.get('shares', 0) or 0)
            price = float(p.get('current_price', 0) or 0)
            if shares > 0 and price > 0 and sym:
                weights_map[sym] = shares * price

        ps = PortfolioSnapshot(
            positions=weights_map,
            equity=equity,
            peak_equity=equity,  # 此处只关心 VaR/CVaR，回撤检查不需要峰值
            returns=returns_by_symbol,
        )
        checker = PortfolioRiskChecker(var_limit=var_limit)
        cvar_result = checker.check_cvar(ps, confidence=0.95, cvar_limit=cvar_limit)
        logger.info('CVaR 检查: level=%s reason=%s', cvar_result.level, cvar_result.reason)
    except Exception as exc:
        logger.error('CVaR 检查失败: %s', exc, exc_info=True)

    # 5. 蒙特卡洛模拟
    mc_result = None
    try:
        portfolio_returns = _build_portfolio_returns(positions, equity, returns_by_symbol)
        if portfolio_returns is None or len(portfolio_returns) < 20:
            logger.warning('组合收益率序列样本不足 (need ≥20)，跳过 MC')
        else:
            from core.portfolio_risk import MonteCarloStressTest
            mc = MonteCarloStressTest(
                n_simulations=n_simulations,
                horizon_days=horizon_days,
                method='bootstrap',
                seed=42,
            )
            mc_result = mc.run(portfolio_returns, initial_equity=equity)
            logger.info('MC 模拟完成: 亏损概率=%.2f%% ES(95%%)=%.2f%% P95回撤=%.2f%%',
                        mc_result.prob_loss * 100,
                        mc_result.expected_shortfall * 100,
                        mc_result.max_drawdown_p95 * 100)
    except Exception as exc:
        logger.error('MC 模拟失败: %s', exc, exc_info=True)

    # 6. 写报告
    summary = _summarize_result(snapshot, cvar_result, mc_result, var_limit, cvar_limit)
    _write_report(summary, output_dir)

    # 7. 推送 Prometheus
    try:
        from core.metrics import get_registry
        var_pct_val = None
        cvar_pct_val = None
        mc_p95_val = None
        if cvar_result is not None:
            details = cvar_result.details or {}
            var_pct_val = details.get('var_pct')
            cvar_pct_val = details.get('cvar_pct')
        if mc_result is not None:
            mc_p95_val = mc_result.max_drawdown_p95
        peak_eq = snapshot.get('peak_equity', equity) or equity
        dd = max(0.0, 1.0 - equity / peak_eq) if peak_eq > 0 else 0.0
        get_registry().set_risk_metrics(
            var_pct=var_pct_val,
            cvar_pct=cvar_pct_val,
            drawdown_pct=dd,
            max_drawdown_p95=mc_p95_val,
        )
    except Exception as exc:
        logger.debug('metrics push failed: %s', exc)

    # 8. 写"风险闸门"状态供 IntradayMonitor 读取(无论有无 breach 都写,
    #    解除时也要主动覆盖)
    try:
        from core.risk_state import write_risk_state
        write_risk_state(summary.get('breach', []), summary=summary)
    except Exception as exc:
        logger.warning('risk_state write failed: %s', exc)

    # 9. 告警
    if enable_alert:
        _maybe_alert(summary)

    return summary


def main():
    parser = argparse.ArgumentParser(description='每日组合风险报告（CVaR + MC）')
    parser.add_argument('--n-sim', type=int, default=10000)
    parser.add_argument('--horizon', type=int, default=21)
    parser.add_argument('--lookback', type=int, default=252)
    parser.add_argument('--api-port', type=int, default=5555)
    parser.add_argument('--no-alert', action='store_true', help='不推送 AlertManager')
    parser.add_argument('--output-dir', type=str, default='')
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    )

    output_dir = Path(args.output_dir) if args.output_dir else None
    summary = run_report(
        n_simulations=args.n_sim,
        horizon_days=args.horizon,
        lookback_days=args.lookback,
        api_port=args.api_port,
        output_dir=output_dir,
        enable_alert=not args.no_alert,
    )
    print(json.dumps({
        'date': summary.get('date'),
        'equity': summary.get('equity'),
        'positions_count': summary.get('positions_count'),
        'breach': summary.get('breach', []),
    }, ensure_ascii=False))


if __name__ == '__main__':
    main()
