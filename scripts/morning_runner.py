"""
morning_runner.py — 早盘自动化 + P5 全自动 Paper Trade 闭环
=========================================================
升级内容（P5）：
  - Step 2: 对 watchlist 运行 evaluate_signal()（环境感知参数）
  - Step 3: 分钟 RSI 二次确认 → 市价 BUY 单
  - Step 4: 记录完整开盘状态
  - Step 5: 生成早报 + 推送
"""

import os
import sys
import json
import logging
from datetime import datetime, date
from typing import Optional

# 避免代理干扰
for _k in list(os.environ.keys()):
    if 'proxy' in _k.lower():
        del os.environ[_k]

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


# ─── 飞书推送 ──────────────────────────────────────────────────────────────

def feishu_push(text: str, to_user: str = 'user:ou_b8add658ac094464606af32933a02d0b'):
    app_id     = os.environ.get('FEISHU_APP_ID', '')
    app_secret = os.environ.get('FEISHU_APP_SECRET', '')
    if not app_id or not app_secret:
        _log.debug('Feishu not configured, skipping push')
        return
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # 获取 tenant_access_token
    token_url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
    token_data = json.dumps({'app_id': app_id, 'app_secret': app_secret}).encode()
    try:
        tok_req = urllib.request.Request(token_url, data=token_data,
                                          headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(tok_req, context=ctx, timeout=10) as r:
            token_result = json.loads(r.read())
        token = token_result.get('tenant_access_token', '')
        if not token:
            _log.warning('Feishu token empty')
            return
        # 发送消息
        msg_url = 'https://open.feishu.cn/open-apis/im/v1/messages'
        msg_body = {
            'receive_id': to_user.replace('user:', ''),
            'msg_type': 'text',
            'content': json.dumps({'text': text})
        }
        msg_req = urllib.request.Request(
            msg_url + f'?receive_id_type=open_id',
            data=json.dumps(msg_body).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {token}'},
            method='POST'
        )
        with urllib.request.urlopen(msg_req, context=ctx, timeout=10) as r:
            _log.info('Feishu push OK: %s', r.read()[:100])
    except Exception as e:
        _log.warning('Feishu push failed: %s', e)


# ─── Step 1: 动态选股 ───────────────────────────────────────────────────────

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


# ─── Step 2: 同步 Watchlist ────────────────────────────────────────────────

def sync_watchlist(stocks: list):
    """同步选股结果到 Backend watchlist。"""
    try:
        # 清除旧 watchlist
        existing = api_get('/watchlist')
        for item in existing.get('watchlist', []):
            sym = item.get('symbol')
            if sym:
                try:
                    api_delete(f'/watchlist/{sym}')  # DELETE
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


# ─── P5 New: 读取当前持仓 ─────────────────────────────────────────────────

def get_current_positions() -> list:
    """获取 Backend 当前持仓列表。"""
    summary = api_get('/portfolio/summary')
    return summary.get('positions', [])

def get_cash_and_equity() -> tuple[float, float]:
    """获取当前现金和总权益。"""
    summary = api_get('/portfolio/summary')
    return summary.get('cash', 0.0), summary.get('total_equity', 0.0)


# ─── P5 New: 读取 Regime + 参数 ─────────────────────────────────────────────

def get_regime_params() -> dict:
    """
    读取今日市场环境参数。
    Returns: {regime, rsi_buy, rsi_sell, atr_threshold, ...}
    """
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


# ─── P5 New: 信号评估 ──────────────────────────────────────────────────────

def evaluate_candidate(symbol: str, rsi_buy: int, atr_threshold: float) -> Optional[dict]:
    """
    对候选标的运行 evaluate_signal()。
    Returns: {signal, price, reason, pct, ...} or None（无信号）
    """
    try:
        sys.path.insert(0, BK_DIR)
        from services.signals import evaluate_signal

        alert = evaluate_signal(
            symbol=symbol,
            rsi_buy=rsi_buy,
            atr_threshold=atr_threshold,
        )
        if alert and alert.signal == 'RSI_BUY':
            return {
                'symbol':       alert.symbol,
                'signal':       alert.signal,
                'price':        alert.price,
                'pct':          alert.pct,
                'reason':       alert.reason,
                'prev_rsi':     alert.prev_rsi,
                'volume_ratio': alert.volume_ratio,
                'day_chg':      alert.day_chg,
            }
        return None
    except Exception as e:
        _log.warning('evaluate_signal(%s) failed: %s', symbol, e)
        return None


# ─── P5 New: 分钟 RSI 二次确认 ──────────────────────────────────────────────

def confirm_minute_rsi(symbol: str, direction: str = 'BUY') -> tuple[bool, Optional[float], str]:
    """
    分钟级 RSI 二次确认（防止金叉假突破）。
    逻辑与 intraday_monitor.py 一致。
    Returns: (confirmed, minute_rsi, reason)
    """
    try:
        sys.path.insert(0, BK_DIR)
        from services.signals import confirm_signal_minute
        return confirm_signal_minute(symbol, direction)
    except Exception as e:
        _log.warning('confirm_signal_minute(%s) failed: %s', symbol, e)
        return True, None, '确认函数异常，放行'


# ─── P5 New: Kelly 仓位计算 ────────────────────────────────────────────────

def calc_kelly_shares(cash: float, price: float, kelly_pct: float = 0.5) -> int:
    """
    计算 Kelly 仓位（取整至100股，保守下限100股）。
    kelly_pct: Kelly 比例，默认 0.5（半 Kelly）
    """
    if price <= 0 or cash <= 0:
        return 0
    raw = (cash * kelly_pct) / price
    shares = int(raw // 100 * 100)
    return max(100, shares)


def calc_single_position_shares(cash: float, equity: float,
                                 price: float,
                                 max_pos_pct: float = 0.25) -> int:
    """
    双重仓位限制：
      1. Kelly 半仓（kelly_pct=0.5）
      2. 单标的 <= 25% 总权益
    取两者较小值。
    """
    kelly_shares  = calc_kelly_shares(cash, price, kelly_pct=0.5)
    max_pos_shares = int((equity * max_pos_pct) / price)
    max_pos_shares = (max_pos_shares // 100) * 100
    return min(kelly_shares, max(100, max_pos_shares))


# ─── P5 New: 提交市价买单 ──────────────────────────────────────────────────

def submit_market_buy(symbol: str, shares: int, reason: str = '') -> dict:
    """通过 Backend API 提交市价买入单。"""
    try:
        result = api_post('/orders/submit', {
            'symbol':     symbol,
            'direction':  'BUY',
            'shares':     shares,
            'price_type': 'market',
        })
        status = result.get('status', 'unknown')
        filled = result.get('filled_shares', 0)
        avg_price = result.get('avg_price', 0)
        _log.info('BUY %s %d shares @ %.2f [avg]: %s (%s)',
                  symbol, filled, avg_price, status, reason[:60] if reason else '')
        return result
    except Exception as e:
        _log.error('submit_market_buy(%s) failed: %s', symbol, e)
        return {}


# ─── P5 New: 记录信号到 Backend ─────────────────────────────────────────────

def log_signal_to_backend(signal_data: dict, regime_info: dict):
    try:
        reason = '[' + regime_info['regime'] + '] ' + signal_data.get('reason', '')
        api_post("/signals", {
            'symbol': signal_data['symbol'],
            'signal': signal_data.get('signal', 'RSI_BUY'),
            'price': signal_data.get('price', 0),
            'pct': signal_data.get('pct', 0),
            'prev_rsi': signal_data.get('prev_rsi', 0),
            'volume_ratio': signal_data.get('volume_ratio', 0),
            'day_chg': signal_data.get('day_chg', 0),
            'reason': reason,
        })
    except Exception as e:
        _log.warning("log_signal failed: %s", e)

def log_opening_state(candidates: list, buy_results: list,
                      positions: list, cash: float, equity: float,
                      regime_info: dict):
    """
    记录完整开盘状态到 backend（作为 daily_meta notes）。
    便于盘中/收盘时追溯当时的信号和订单状态。
    """
    try:
        today = date.today().isoformat()
        # 格式化候选股信息
        cand_summary = '; '.join([
            f"{c['symbol']}({c['name']}) score={c['score']:.0f}" for c in candidates
        ])
        # 格式化已执行订单
        orders_summary = '; '.join([
            f"{r.get('symbol')} {r.get('filled_shares', 0)}@{r.get('avg_price', 0):.2f}"
            for r in buy_results if r.get('filled_shares', 0) > 0
        ]) or '无'

        notes = (
            f"[MorningRunner] regime={regime_info['regime']} "
            f"ATR={regime_info['atr_ratio']:.2f} "
            f"RSI({regime_info['rsi_buy']}/{regime_info['rsi_sell']}) "
            f"candidates:{len(candidates)} "
            f"orders:{orders_summary} "
            f"equity={equity:.0f} cash={cash:.0f}"
        )

        api_post('/portfolio/daily', {
            'date':        today,
            'nav':         equity / equity if equity else 1.0,
            'equity':      equity,
            'cash':        cash,
            'market_value': equity - cash,
            'notes':       notes,
        })
        _log.info('Opening state logged: %s', notes[:200])
    except Exception as e:
        _log.warning('log_opening_state failed: %s', e)


# ─── P5 New: 完整早盘自动化流程 ─────────────────────────────────────────────

def evaluate_watchlist_and_submit(candidates: list, regime_info: dict) -> list:
    """
    对候选股运行信号评估 → 分钟确认 → Kelly仓位 → 市价单。
    Returns: list of order results
    """
    cash, equity = get_cash_and_equity()
    if cash <= 0 or equity <= 0:
        _log.warning('No equity or cash, skipping order submission')
        return []

    buy_results = []
    for cand in candidates:
        sym = cand['symbol']
        rsi_buy = regime_info['rsi_buy']
        atr_thr  = regime_info['atr_threshold']

        # Step A: 评估信号
        sig_result = evaluate_candidate(sym, rsi_buy, atr_thr)
        if not sig_result:
            _log.debug('%s: no RSI_BUY signal, skipping', sym)
            continue

        price   = sig_result['price']
        sig_reason = sig_result['reason']

        # Step B: 分钟 RSI 二次确认
        confirmed, minute_rsi, confirm_reason = confirm_minute_rsi(sym, 'BUY')
        _log.info('%s minute RSI confirm: confirmed=%s rsi=%s reason=%s',
                  sym, confirmed, minute_rsi, confirm_reason)

        if not confirmed:
            _log.info('  -> %s RSI confirm rejected: %s', sym, confirm_reason)
            # 记录被拒信号
            log_signal_to_backend({**sig_result, 'reason': sig_reason + ' [min_rejected]'}, regime_info)
            continue

        # Step C: 计算仓位（双重限制）
        shares = calc_single_position_shares(cash, equity, price, max_pos_pct=0.25)
        if shares < 100:
            _log.info('  -> %s position too small (%d shares), skipping', sym, shares)
            continue

        # Step D: 记录信号
        log_signal_to_backend(sig_result, regime_info)

        # Step E: 提交市价单
        order_result = submit_market_buy(sym, shares, reason=sig_reason)
        buy_results.append(order_result)

        # 扣减预估现金（盘后以实际成交为准）
        if order_result.get('filled_shares'):
            filled = order_result['filled_shares']
            avg_p  = order_result.get('avg_price', price)
            cash -= filled * avg_p

    return buy_results


# ─── Step 3: 记录日初净值 ─────────────────────────────────────────────────

def record_opening_equity() -> float:
    """记录当前总权益。"""
    summary = api_get('/portfolio/summary')
    equity = summary.get('total_equity', 0)
    cash   = summary.get('cash', 0)
    _log.info('Opening equity: %.2f (cash: %.2f)', equity, cash)
    return equity


# ─── Step 4: 早报生成 + 推送 ───────────────────────────────────────────────

def build_and_push_morning_report(candidates: list, buy_results: list,
                                   regime_info: dict,
                                   equity: float, cash: float):
    """生成结构化早报文本并推送飞书。"""
    try:
        import morning_report
        report = morning_report.build_report()
    except Exception as e:
        _log.error('morning_report.build_report failed: %s', e)
        report = None

    # 如果 morning_report 不可用，手动构建简洁版
    if not report:
        executed = [r for r in buy_results if r.get('filled_shares', 0) > 0]
        lines = [
            f"【早报】{date.today().isoformat()}",
            f"",
            f"市场环境: [{regime_info['regime']}] {regime_info.get('regime_reason', '')}",
            f"ATR ratio: {regime_info['atr_ratio']:.3f}",
            f"RSI参数: ({regime_info['rsi_buy']}/{regime_info['rsi_sell']})",
            f"",
            f"开盘权益: {equity:.0f}  现金: {cash:.0f}",
            f"",
            f"候选标的 ({len(candidates)}只):",
        ]
        for c in candidates[:5]:
            lines.append(f"  {c['symbol']} {c['name']} score={c['score']:.0f}")
        lines.append("")
        if executed:
            lines.append(f"已执行订单 ({len(executed)}笔):")
            for r in executed:
                lines.append(f"  买入 {r['symbol']} {r['filled_shares']}股 @{r.get('avg_price', 0):.2f}")
        else:
            lines.append("已执行订单: 无")
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
        msg_url = 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id'
        msg_body = {
            'receive_id': 'ou_b8add658ac094464606af32933a02d0b',
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
        _log.warning('[Step0] No stocks selected, exiting')
        return

    # Step 1: 同步 watchlist 到 Backend
    _log.info('[Step1] Syncing watchlist...')
    sync_watchlist(candidates)

    # Step 2: 读取市场环境参数
    _log.info('[Step2] Detecting market regime...')
    regime_info = get_regime_params()
    _log.info('  Regime: [%s] %s', regime_info['regime'], regime_info.get('regime_reason', ''))

    # Step 3: 信号评估 + 分钟确认 + 提交市价单
    _log.info('[Step3] Evaluating signals and submitting orders...')
    positions = get_current_positions()
    cash, equity = get_cash_and_equity()
    _log.info('  Positions before open: %d, Equity: %.2f, Cash: %.2f',
               len(positions), equity, cash)

    buy_results = evaluate_watchlist_and_submit(candidates, regime_info)

    # Step 4: 记录开盘状态
    _log.info('[Step4] Logging opening state...')
    cash_after, equity_after = get_cash_and_equity()
    log_opening_state(candidates, buy_results,
                      get_current_positions(), cash_after, equity_after,
                      regime_info)

    # Step 5: 生成早报 + 飞书推送
    _log.info('[Step5] Building and pushing morning report...')
    build_and_push_morning_report(candidates, buy_results, regime_info,
                                   equity_after, cash_after)

    _log.info('=== Morning Runner completed at %s ===', datetime.now().isoformat())


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    run()
