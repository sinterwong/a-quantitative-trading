"""
morning_runner.py — 每日早盘自动化
=====================================
在每天 9:05 AM (Asia/Shanghai) 运行，执行以下步骤：

  1. 动态选股（DynamicStockSelectorV2）
     - 获取最新板块资金流向
     - 多维度评分选出当日标的
     - 将结果写入 watchlist（通过 Backend API）

  2. 记录日初净值（daily_meta）
     - 开盘前记录 equity 快照

  3. 生成早报推送飞书
     - 调用 morning_report.py 生成报告内容
     - 通过 Feishu 推送

使用方式（crontab 或手动）：
  python scripts/morning_runner.py

该脚本设计为无状态 — 所有状态通过 http://127.0.0.1:5555 API 管理。
"""

import os
import sys
import json
import logging
import urllib.request
import ssl
import argparse
from datetime import datetime

# ── 配置 ────────────────────────────────────────────────────────────────────

BASE_URL    = 'http://127.0.0.1:5555'
# morning_runner.py lives in quant_repo/scripts/
# WORKSPACE = C:\Users\sinte\.openclaw\workspace  (3 levels up)
SCRIPT_FILE = os.path.abspath(__file__)
WORKSPACE   = os.path.dirname(os.path.dirname(os.path.dirname(SCRIPT_FILE)))  # workspace root
BACKEND_DIR = os.path.join(WORKSPACE, 'quant_repo', 'backend')
SCRIPTS_DIR = os.path.join(WORKSPACE, 'scripts')  # workspace/scripts/
sys.path.insert(0, BACKEND_DIR)
sys.path.insert(0, SCRIPTS_DIR)
sys.path.insert(0, os.path.join(WORKSPACE, 'quant_repo', 'scripts'))  # quant_repo/scripts/

_log = logging.getLogger('morning_runner')


def api_get(path: str) -> dict:
    url = f'{BASE_URL}{path}'
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def api_post(path: str, body: dict = None) -> dict:
    url = f'{BASE_URL}{path}'
    data = json.dumps(body or {}).encode('utf-8') if body else None
    headers = {'Content-Type': 'application/json'} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers, method='POST')
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def feishu_push(text: str, to_user: str = 'user:ou_b8add658ac094464606af32933a02d0b'):
    """通过飞书推送文本消息。"""
    from utils.feishu import FeishuClient
    try:
        client = FeishuClient()
        client.send_text(to_user.split(':')[1], text)
        _log.info('Feishu push sent')
    except Exception as e:
        _log.warning('Feishu push failed: %s', e)


def get_feishu_client():
    """Load FeishuClient from utils.feishu"""
    try:
        from utils.feishu import FeishuClient
        return FeishuClient()
    except ImportError:
        from utils.feishu_webhook import FeishuWebhook
        return FeishuWebhook()


def fetch_selected_stocks(n: int = 5) -> list:
    """
    调用 DynamicStockSelectorV2 获取当日精选股票。
    返回: [{symbol, name, reason, score}, ...]
    """
    try:
        from scripts.dynamic_selector import DynamicStockSelectorV2
        sel = DynamicStockSelectorV2()

        _log.info('Fetching market news...')
        sel.fetch_market_news(30)

        _log.info('Fetching sector data...')
        sel.fetch_sectors()

        _log.info('Calculating all scores...')
        sel.calc_all_scores()

        # 获取 top N 个股
        stocks = sel.get_stock_with_context(n)
        result = []
        for s in stocks[:n]:
            result.append({
                'symbol': s.get('symbol', ''),
                'name': s.get('name', ''),
                'reason': s.get('reason', ''),
                'score': s.get('total', 0),
            })
        _log.info('Selected %d stocks: %s',
                  len(result), [s['symbol'] for s in result])
        return result
    except Exception as e:
        _log.error('DynamicSelector failed: %s', e)
        return []


def sync_watchlist(stocks: list):
    """
    将精选股票同步到 Backend watchlist（通过 API）。
    """
    try:
        # 先清空旧 watchlist
        existing = api_get('/watchlist')
        for item in existing.get('watchlist', []):
            sym = item.get('symbol')
            if sym:
                try:
                    api_post(f'/watchlist/{sym}')
                except Exception:
                    pass

        # 添加新标的（默认5%阈值）
        added = 0
        for s in stocks:
            try:
                api_post('/watchlist/add', {
                    'symbol': s['symbol'],
                    'name': s['name'],
                    'reason': s['reason'],
                    'alert_pct': 5.0,
                })
                added += 1
            except Exception as e:
                _log.warning('Failed to add %s to watchlist: %s', s['symbol'], e)
        _log.info('Watchlist synced: %d/%d stocks added', added, len(stocks))
        return added
    except Exception as e:
        _log.error('Watchlist sync failed: %s', e)
        return 0


def record_opening_equity() -> float:
    """
    开盘前记录日初净值。
    """
    try:
        summary = api_get('/portfolio/summary')
        equity = summary.get('total_equity', 0)
        cash = summary.get('cash', 0)
        _log.info('Opening equity: %.2f (cash: %.2f)', equity, cash)
        return equity
    except Exception as e:
        _log.error('Failed to record opening equity: %s', e)
        return 0.0


def build_morning_report(stocks: list, equity: float) -> str:
    """
    生成早报文本（不依赖LLM，直接用格式化的结构化文本）。
    """
    from scripts.morning_report import build_report
    # morning_report.py 生成完整早报
    report = build_report()
    return report


def push_feishu_report(report_text: str):
    """推送早报到飞书。"""
    try:
        client = get_feishu_client()
        client.send_text(
            'ou_b8add658ac094464606af32933a02d0b',
            report_text
        )
        _log.info('Morning report pushed to Feishu')
    except Exception as e:
        _log.warning('Feishu push failed: %s', e)


# ── 主流程 ────────────────────────────────────────────────────────────────

def run():
    now = datetime.now()
    _log.info('=== Morning Runner started at %s ===', now.isoformat())

    # Step 1: 动态选股
    _log.info('Step 1: Running dynamic stock selector...')
    selected = fetch_selected_stocks(n=5)
    if not selected:
        _log.warning('No stocks selected — skipping watchlist sync and push')
        return

    # Step 2: 同步 Watchlist 到 Backend
    _log.info('Step 2: Syncing watchlist...')
    sync_watchlist(selected)

    # Step 3: 记录日初净值
    _log.info('Step 3: Recording opening equity...')
    equity = record_opening_equity()

    # Step 4: 生成早报并推送
    _log.info('Step 4: Building morning report...')
    try:
        from scripts.morning_report import build_report
        report = build_report()
        _log.info('Morning report built (%d chars)', len(report))
    except Exception as e:
        _log.error('morning_report build failed: %s', e)
        report = None

    if report:
        push_feishu_report(report)

    _log.info('=== Morning Runner completed at %s ===', datetime.now().isoformat())
    return selected


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
    )
    run()
