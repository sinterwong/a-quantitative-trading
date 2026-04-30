"""
core/daily_ops_reporter.py — 每日运营报告生成器

功能：
  - 汇聚因子 IC 快照、策略健康度、组合 P&L、告警摘要
  - 输出 JSON 到 outputs/daily_ops/ops_{date}.json
  - 通过 AlertManager 发送 Markdown 格式日报（支持企业微信/钉钉/邮件）

触发方式：
  - backend/main.py Scheduler 每日 16:00（收盘后）自动触发
  - 也可独立调用：DailyOpsReporter().run(api_port=5555)

数据来源（均为本地 API，无需外部平台）：
  - 组合 P&L     → GET /positions, GET /trades
  - 策略健康度   → core/strategy_health.StrategyHealthMonitor（已有）
  - 告警摘要     → core/alerting.AlertManager.history（已有）
  - 因子 IC 快照 → outputs/factor_ic_report_*.json（若存在）

用法：
    reporter = DailyOpsReporter(api_port=5555)
    report = reporter.run()
    # report 已保存到 outputs/daily_ops/ops_{date}.json 并推送告警
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import date, datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger('core.daily_ops_reporter')

_PROJ_DIR = os.path.dirname(os.path.dirname(__file__))
_OUTPUT_DIR = os.path.join(_PROJ_DIR, 'outputs', 'daily_ops')
os.makedirs(_OUTPUT_DIR, exist_ok=True)


# ---------------------------------------------------------------------------
# DailyOpsReporter
# ---------------------------------------------------------------------------

class DailyOpsReporter:
    """
    每日运营报告生成器。

    Parameters
    ----------
    api_port : int
        backend API 端口（默认 5555）
    timeout  : int
        HTTP 请求超时秒数
    """

    def __init__(self, api_port: int = 5555, timeout: int = 10):
        self.api_port = api_port
        self.timeout = timeout

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(self, report_date: Optional[date] = None) -> Dict[str, Any]:
        """
        生成并保存今日运营报告。

        Returns
        -------
        dict — 完整报告结构（已写入 JSON 文件）
        """
        today = report_date or date.today()
        today_str = today.isoformat()

        report: Dict[str, Any] = {
            'date': today_str,
            'generated_at': datetime.now().isoformat(),
            'portfolio': self._fetch_portfolio(),
            'trades': self._fetch_trades_summary(today_str),
            'health': self._fetch_health(),
            'alerts': self._fetch_alert_summary(today_str),
            'factor_ic': self._fetch_factor_ic_snapshot(),
        }

        self._save(report, today_str)
        self._send(report)
        return report

    # ------------------------------------------------------------------
    # 数据收集
    # ------------------------------------------------------------------

    def _api_get(self, path: str) -> Optional[Dict]:
        """对本地 API 发 GET 请求，失败返回 None。"""
        url = f'http://127.0.0.1:{self.api_port}{path}'
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return json.loads(r.read())
        except Exception as e:
            logger.warning('DailyOpsReporter API GET %s failed: %s', path, e)
            return None

    def _fetch_portfolio(self) -> Dict[str, Any]:
        """从 /positions 获取持仓 P&L 汇总。"""
        data = self._api_get('/positions')
        if data is None:
            return {'error': 'API unavailable', 'total_value': 0.0, 'positions': []}

        positions = data.get('positions', [])
        total_pnl = sum(float(p.get('unrealized_pnl', 0.0)) for p in positions)
        total_value = sum(
            float(p.get('shares', 0)) * float(p.get('current_price', 0.0))
            for p in positions
        )
        return {
            'total_value': round(total_value, 2),
            'total_unrealized_pnl': round(total_pnl, 2),
            'n_positions': len([p for p in positions if float(p.get('shares', 0)) > 0]),
            'positions': [
                {
                    'symbol': p.get('symbol', ''),
                    'shares': p.get('shares', 0),
                    'unrealized_pnl': round(float(p.get('unrealized_pnl', 0.0)), 2),
                    'pnl_pct': round(float(p.get('pnl_pct', 0.0)), 4),
                }
                for p in positions if float(p.get('shares', 0)) > 0
            ],
        }

    def _fetch_trades_summary(self, today_str: str) -> Dict[str, Any]:
        """从 /trades 获取当日成交汇总。"""
        data = self._api_get('/trades')
        if data is None:
            return {'error': 'API unavailable', 'n_trades': 0, 'realized_pnl': 0.0}

        trades = data.get('trades', [])
        today_trades = [t for t in trades if str(t.get('date', ''))[:10] == today_str]
        realized_pnl = sum(float(t.get('pnl', 0.0)) for t in today_trades)
        return {
            'n_trades': len(today_trades),
            'realized_pnl': round(realized_pnl, 2),
            'buy_count': sum(1 for t in today_trades if t.get('side', '').upper() == 'BUY'),
            'sell_count': sum(1 for t in today_trades if t.get('side', '').upper() == 'SELL'),
        }

    def _fetch_health(self) -> Dict[str, Any]:
        """从 StrategyHealthMonitor 获取策略健康摘要。"""
        try:
            data = self._api_get('/analysis/health')
            if data and data.get('status') == 'ok':
                return data.get('data', {})
        except Exception:
            pass

        # 降级：返回基础占位符
        return {
            'status': 'unknown',
            'note': 'Health API not available; run /analysis/run to generate',
        }

    def _fetch_alert_summary(self, today_str: str) -> Dict[str, Any]:
        """从 AlertManager 历史中统计当日告警条数。"""
        try:
            from core.alerting import get_alert_manager
            mgr = get_alert_manager()
            history = mgr.history
            today_alerts = [a for a in history if str(a.get('timestamp', ''))[:10] == today_str]
            by_level: Dict[str, int] = {}
            for a in today_alerts:
                lvl = a.get('level', 'INFO')
                by_level[lvl] = by_level.get(lvl, 0) + 1
            return {
                'total': len(today_alerts),
                'by_level': by_level,
                'last_critical': next(
                    (a.get('message', '')[:80] for a in reversed(today_alerts)
                     if a.get('level') == 'CRITICAL'), None
                ),
            }
        except Exception as e:
            logger.warning('Alert summary failed: %s', e)
            return {'total': 0, 'by_level': {}, 'error': str(e)}

    def _fetch_factor_ic_snapshot(self) -> Dict[str, Any]:
        """读取最新因子 IC 报告文件（若存在）。"""
        ic_path = os.path.join(_PROJ_DIR, 'outputs', 'factor_ic_report_2026.json')
        if not os.path.exists(ic_path):
            return {'available': False, 'note': 'Run factor IC analysis to generate report'}
        try:
            with open(ic_path, 'r', encoding='utf-8') as f:
                ic_data = json.load(f)
            # 只取 IC 均值摘要，避免报告过大
            summary = {}
            factors = ic_data.get('factors', ic_data)
            if isinstance(factors, dict):
                for name, stats in list(factors.items())[:10]:
                    if isinstance(stats, dict):
                        summary[name] = {
                            'ic_mean': round(float(stats.get('ic_mean', 0.0)), 4),
                            'ic_ir': round(float(stats.get('ic_ir', 0.0)), 4),
                        }
            return {'available': True, 'factors': summary}
        except Exception as e:
            return {'available': False, 'error': str(e)}

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------

    def _save(self, report: Dict[str, Any], today_str: str) -> None:
        """保存报告 JSON 到 outputs/daily_ops/。"""
        path = os.path.join(_OUTPUT_DIR, f'ops_{today_str}.json')
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
            logger.info('Daily ops report saved: %s', path)
        except Exception as e:
            logger.error('Daily ops report save failed: %s', e)

    def _send(self, report: Dict[str, Any]) -> None:
        """通过 AlertManager 推送日报。"""
        try:
            from core.alerting import get_alert_manager
            mgr = get_alert_manager()

            portfolio = report.get('portfolio', {})
            trades = report.get('trades', {})
            health = report.get('health', {})
            alerts_summary = report.get('alerts', {})

            pnl = portfolio.get('total_unrealized_pnl', 0.0)
            realized = trades.get('realized_pnl', 0.0)
            n_positions = portfolio.get('n_positions', 0)
            n_trades = trades.get('n_trades', 0)
            health_status = health.get('status', 'unknown')
            n_alerts = alerts_summary.get('total', 0)
            critical_msg = alerts_summary.get('last_critical')

            mgr.send_daily_report({
                'date': report['date'],
                'total_pnl': pnl,
                'pnl_pct': pnl / max(portfolio.get('total_value', 1.0), 1.0),
                'n_trades': n_trades,
                'extra': {
                    '已实现盈亏': f'{realized:+.2f} 元',
                    '持仓数量': n_positions,
                    '策略健康': health_status,
                    '当日告警': n_alerts,
                    **({'最新CRITICAL': critical_msg[:60]} if critical_msg else {}),
                },
            })
        except Exception as e:
            logger.warning('Daily ops report send failed: %s', e)
