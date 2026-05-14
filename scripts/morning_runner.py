"""
morning_runner.py — 早盘自动化（仅选股 + watchlist 同步 + 早报推送）
=================================================================

设计原则（重构后）：
  morning_runner 不再下单。所有买卖决策统一由 IntradayMonitor（09:31 启动）
  通过 FactorPipeline + 风控链处理，避免双信号源并存。

执行步骤：
  Step 0: DynamicStockSelectorV2 选 N 只候选标的
  Step 1: 同步候选到 backend watchlist（供 IntradayMonitor 09:31 评分使用）
  Step 2: 读取市场环境（regime）用于早报上下文
  Step 3: 记录开盘 daily_meta（candidates + 现金 + 权益）
  Step 4: 生成结构化早报 + 飞书推送
"""

import os
import sys
import json
import logging
from datetime import datetime, date

# 避免代理干扰
for _k in list(os.environ.keys()):
    if 'proxy' in _k.lower():
        del os.environ[_k]

from dotenv import load_dotenv
load_dotenv(override=True)  # 强制以 .env 为准，覆盖 shell 环境变量

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
Q_DIR    = os.path.dirname(THIS_DIR)           # quant_repo/
BK_DIR   = os.path.join(Q_DIR, 'backend')     # quant_repo/backend/
sys.path.insert(0, THIS_DIR)   # for dynamic_selector
sys.path.insert(0, BK_DIR)      # for backend services

import urllib.request
import urllib.error

_log = logging.getLogger('morning_runner')

BASE_URL = 'http://127.0.0.1:5555'

# ─── Backend API 封装 ─────────────────────────────────────────────────────

def api_get(path: str) -> dict:
    try:
        req = urllib.request.Request(f'{BASE_URL}{path}')
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        _log.warning('API GET %s failed: %s', path, e)
        return {}

def api_post(path: str, body: dict = None) -> dict:
    try:
        data = json.dumps(body or {}).encode('utf-8')
        req = urllib.request.Request(
            f'{BASE_URL}{path}', data=data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        _log.warning('API POST %s HTTP %d: %s', path, e.code, body[:200])
        return {}
    except Exception as e:
        _log.warning('API POST %s failed: %s', path, e)
        return {}


def api_delete(path: str) -> dict:
    try:
        req = urllib.request.Request(
            f'{BASE_URL}{path}',
            method='DELETE'
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()
        _log.warning('API DELETE %s HTTP %d: %s', path, e.code, body[:200])
        return {}
    except Exception as e:
        _log.warning('API DELETE %s failed: %s', path, e)
        return {}


# ─── Step 0: 动态选股 ───────────────────────────────────────────────────────

def fetch_selected_stocks(n: int = 5) -> list:
    """
    调用 DynamicStockSelectorV2 获取当日热门标的。
    Returns: [{symbol, name, reason, score}, ...]
    """
    try:
        import dynamic_selector
        DynamicStockSelectorV2 = dynamic_selector.DynamicStockSelectorV2
        sel = DynamicStockSelectorV2()

        _log.info('Fetching market news...')
        sel.fetch_market_news(30)

        _log.info('Fetching sector data...')
        sel.fetch_sectors()

        _log.info('Calculating all scores...')
        sel.calc_all_scores()

        stocks = sel.get_stock_with_context(n)
        result = []
        for s in stocks[:n]:
            result.append({
                'symbol': s.get('code', ''),
                'name':   s.get('name', ''),
                'reason': s.get('reason', ''),
                'score':  s.get('total', 0),
            })
        _log.info('Selected %d stocks: %s', len(result), [s['symbol'] for s in result])
        return result
    except Exception as e:
        _log.error('DynamicSelector failed: %s', e)
        return []


# ─── Step 1: 同步 Watchlist ────────────────────────────────────────────────

def sync_watchlist(stocks: list):
    """同步选股结果到 Backend watchlist。"""
    try:
        # 清除旧 watchlist
        existing = api_get('/watchlist')
        for item in existing.get('watchlist', []):
            sym = item.get('symbol')
            if sym:
                try:
                    api_delete(f'/watchlist/{sym}')
                except Exception:
                    pass

        # 添加新标的（默认5%预警阈值）
        added = 0
        for s in stocks:
            try:
                api_post('/watchlist/add', {
                    'symbol':    s['symbol'],
                    'name':      s['name'],
                    'reason':    s['reason'],
                    'alert_pct': 5.0,
                })
                added += 1
            except Exception as e:
                _log.warning('Failed to add %s: %s', s['symbol'], e)
        _log.info('Watchlist synced: %d/%d', added, len(stocks))
        return added
    except Exception as e:
        _log.error('Watchlist sync failed: %s', e)
        return 0


# ─── Step 2: 读取持仓和现金 ─────────────────────────────────────────────────

def get_current_positions() -> list:
    summary = api_get('/portfolio/summary')
    return summary.get('positions', [])

def get_cash_and_equity() -> tuple[float, float]:
    summary = api_get('/portfolio/summary')
    return summary.get('cash', 0.0), summary.get('total_equity', 0.0)


# ─── Step 3: 读取 Regime ─────────────────────────────────────────────────

def get_regime_params() -> dict:
    """读取今日市场环境参数（仅作为早报展示用，不再用于下单决策）。"""
    try:
        sys.path.insert(0, os.path.join(Q_DIR, 'scripts', 'quant'))
        from regime_detector import get_cached_regime, get_params_for_regime
        regime_result = get_cached_regime()
        regime = regime_result.get('regime', 'CALM')
        params = get_params_for_regime(regime)
        return {
            'regime':         regime,
            'rsi_buy':        params.get('rsi_buy', 25),
            'rsi_sell':       params.get('rsi_sell', 65),
            'atr_threshold':  params.get('atr_threshold', 0.85),
            'regime_reason':  regime_result.get('reason', ''),
            'atr_ratio':      regime_result.get('atr_ratio', 0),
        }
    except Exception as e:
        _log.warning('Regime detection failed, using CALM defaults: %s', e)
        return {
            'regime': 'CALM', 'rsi_buy': 25, 'rsi_sell': 65,
            'atr_threshold': 0.85, 'regime_reason': '默认CALM', 'atr_ratio': 0,
        }


# ─── Step 4: 记录开盘 daily_meta ────────────────────────────────────────────

def log_opening_state(candidates: list,
                      positions: list, cash: float, equity: float,
                      regime_info: dict):
    """记录完整开盘状态到 backend（作为 daily_meta notes）。"""
    try:
        today = date.today().isoformat()
        notes = (
            f"[MorningRunner] regime={regime_info['regime']} "
            f"ATR={regime_info['atr_ratio']:.2f} "
            f"candidates:{len(candidates)} "
            f"positions:{len(positions)} "
            f"equity={equity:.0f} cash={cash:.0f}"
        )

        api_post('/portfolio/daily', {
            'date':        today,
            'nav':         1.0,
            'equity':      equity,
            'cash':        cash,
            'market_value': equity - cash,
            'notes':       notes,
        })
        _log.info('Opening state logged: %s', notes[:200])
    except Exception as e:
        _log.warning('log_opening_state failed: %s', e)


# ─── Step 5: 早报推送 ──────────────────────────────────────────────────────

def build_and_push_morning_report(candidates: list,
                                   regime_info: dict,
                                   equity: float, cash: float):
    """
    生成结构化早报文本并推送飞书。
    早报中已不再含"今日已下单"区块，因为 morning_runner 不再下单。
    """
    try:
        import morning_report
        normalized_candidates = []
        for c in candidates:
            normalized_candidates.append({
                'code':       c.get('code', c.get('symbol', '')),
                'symbol':     c.get('symbol', c.get('code', '')),
                'name':       c.get('name', ''),
                'change_pct': c.get('pct', c.get('change_pct', 0)),
                'sector_name': c.get('sector', c.get('sector_name', '')),
                'total_score': c.get('score', c.get('total_score', c.get('total', 0))),
            })
        report = morning_report.build_report(
            prefetched_stocks=normalized_candidates,
            prefetched_regime=regime_info,
            # prefetched_orders 留空：morning_runner 不再下单
        )
    except Exception as e:
        _log.error('morning_report.build_report failed: %s', e)
        report = None

    # 降级兜底
    if not report:
        lines = [
            f"【早报降级版】{date.today().isoformat()}",
            f"",
            f"市场环境: [{regime_info['regime']}] {regime_info.get('regime_reason', '')}",
            f"ATR ratio: {regime_info['atr_ratio']:.3f}",
            f"开盘权益: {equity:.0f}  现金: {cash:.0f}",
            f"",
            f"今日候选 ({len(candidates)}只)：",
        ]
        for c in candidates[:5]:
            lines.append(f"  {c.get('symbol', c.get('code', '?'))} {c.get('name', '')} "
                         f"score={c.get('score', 0):.0f}")
        lines.append("")
        lines.append("（开仓决策由盘中 IntradayMonitor 处理）")
        report = '\n'.join(lines)

    # 飞书推送
    try:
        app_id     = os.environ.get('FEISHU_APP_ID', '')
        app_secret = os.environ.get('FEISHU_APP_SECRET', '')
        if not app_id or not app_secret:
            _log.debug('Feishu not configured, skipping push')
            return
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        token_url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
        token_data = json.dumps({'app_id': app_id, 'app_secret': app_secret}).encode()
        tok_req = urllib.request.Request(token_url, data=token_data,
                                          headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(tok_req, context=ctx, timeout=10) as r:
            token_result = json.loads(r.read())
        token = token_result.get('tenant_access_token', '')
        msg_url = 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id'
        msg_body = {
            'receive_id': os.environ.get('FEISHU_CHAT_ID', ''),
            'msg_type': 'text',
            'content': json.dumps({'text': report})
        }
        msg_req = urllib.request.Request(
            msg_url, data=json.dumps(msg_body).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'},
            method='POST'
        )
        with urllib.request.urlopen(msg_req, context=ctx, timeout=10) as r:
            _log.info('Morning report pushed: %s', r.read()[:100])
    except Exception as e:
        _log.warning('Feishu push failed: %s', e)


# ─── 主入口 ────────────────────────────────────────────────────────────────

def run():
    now = datetime.now()
    _log.info('=== Morning Runner started at %s ===', now.isoformat())

    # Step 0: 动态选股
    _log.info('[Step0] Running dynamic stock selector...')
    candidates = fetch_selected_stocks(n=5)
    if not candidates:
        _log.warning('[Step0] No stocks selected, will still send degraded report')

    # Step 1: 同步 watchlist 到 Backend
    #   IntradayMonitor 09:31 第一轮扫描会从 watchlist 取标的，
    #   通过 FactorPipeline 评分 + 全风控链决定是否开仓。
    _log.info('[Step1] Syncing watchlist...')
    sync_watchlist(candidates)

    # Step 2: 读取市场环境（仅作早报上下文）
    _log.info('[Step2] Detecting market regime...')
    regime_info = get_regime_params()
    _log.info('  Regime: [%s] %s', regime_info['regime'], regime_info.get('regime_reason', ''))

    # Step 3: 读取持仓 + 记录开盘 daily_meta
    _log.info('[Step3] Logging opening state...')
    positions = get_current_positions()
    cash, equity = get_cash_and_equity()
    _log.info('  Positions: %d, Equity: %.2f, Cash: %.2f',
               len(positions), equity, cash)
    log_opening_state(candidates, positions, cash, equity, regime_info)

    # Step 4: 生成早报 + 飞书推送
    _log.info('[Step4] Building and pushing morning report...')
    build_and_push_morning_report(candidates, regime_info, equity, cash)

    _log.info('=== Morning Runner completed at %s ===', datetime.now().isoformat())


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    run()
