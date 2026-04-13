"""
intraday_monitor.py — 盘中实时监控服务
========================================
后台线程，交易时段持续运行：
  - 每 5 分钟检查一次持仓信号
  - 合条件时主动推送 Feishu 消息

使用方法：
  from backend.services.intraday_monitor import IntradayMonitor
  mon = IntradayMonitor(svc=portfolio_service)
  mon.start()   # 启动后台线程
  mon.stop()    # 停止
"""

import os
import sys
import time
import json
import logging
import threading
import ssl
import urllib.request
from datetime import datetime, date
from typing import Optional, List

# Resolve imports relative to backend dir
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = THIS_DIR
sys.path.insert(0, BACKEND_DIR)

from services.signals import (
    check_portfolio_signals,
    evaluate_signal,
    format_feishu_message,
    SignalAlert,
    confirm_signal_minute,
    MARKET_MORNING_START, MARKET_MORNING_END,
    MARKET_AFTERNOON_START, MARKET_AFTERNOON_END,
    TRADING_DAYS,
)

logger = logging.getLogger('intraday_monitor')

# 全局配置
CHECK_INTERVAL  = 300   # 秒（5分钟）
COOLDOWN       = 900   # 同一标的信号推送冷却时间（15分钟）


# ─── 交易时段判断 ─────────────────────────────────────────

def is_market_open(now: Optional[datetime] = None) -> bool:
    """判断当前是否为 A 股交易时段"""
    if now is None:
        now = datetime.now()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute

    def t(h_, m_):
        return h_ * 60 + m_

    cur = h * 60 + m
    morning     = t(*MARKET_MORNING_START) <= cur <= t(*MARKET_MORNING_END)
    afternoon   = t(*MARKET_AFTERNOON_START) <= cur <= t(*MARKET_AFTERNOON_END)
    return morning or afternoon


def next_market_seconds(now: Optional[datetime] = None) -> int:
    """距离下次开市还有多少秒（用于启动前 sleep）"""
    if now is None:
        now = datetime.now()
    h, m = now.hour, now.minute
    cur  = h * 60 + m

    # 今天是否还有下午时段
    afternoon_start = MARKET_AFTERNOON_START[0] * 60 + MARKET_AFTERNOON_START[1]
    if cur < afternoon_start:
        return (afternoon_start - cur) * 60

    # 检查明天开盘
    tomorrow = now.replace(hour=0, minute=0, second=0) + __import__('datetime').timedelta(days=1)
    morning_start = MARKET_MORNING_START[0] * 60 + MARKET_MORNING_START[1]
    return int((tomorrow.timestamp() - now.timestamp())) + (morning_start * 60)


# ─── 冷却追踪 ─────────────────────────────────────────────

class CooldownTracker:
    """防止同一标的信号在 COOLDOWN 秒内重复推送"""

    def __init__(self, cooldown: int = COOLDOWN):
        self._cooldown = cooldown
        self._last: dict[str, float] = {}

    def can_fire(self, symbol: str) -> bool:
        now = time.time()
        last = self._last.get(symbol, 0)
        if now - last < self._cooldown:
            return False
        self._last[symbol] = now
        return True

    def purge_old(self):
        now = time.time()
        self._last = {k: v for k, v in self._last.items() if now - v < self._cooldown}


# ─── 主监控类 ─────────────────────────────────────────────

class IntradayMonitor:
    """
    盘中信号监控后台线程。
    检测到信号时：
      1. 推送飞书提醒
      2. 自动提交订单（如果 broker 已注入）
    """

    # 信号 → 订单方向映射（涨跌停类不交易）
    SIGNAL_TO_ORDER = {
        'RSI_BUY':     'BUY',
        'WATCH_BUY':   'BUY',
        'RSI_SELL':    'SELL',
        'WATCH_SELL':  'SELL',
    }
    # 涨跌停类信号不交易（无法以合理价格买入/卖出）
    NO_TRADE_SIGNALS = {
        'LIMIT_UP', 'LIMIT_DOWN',
        'LIMIT_RISK_UP', 'LIMIT_RISK_DOWN',
        'WATCH_LIMIT_UP', 'WATCH_LIMIT_DOWN',
        'VOLATILE',
    }

    def __init__(self, svc, broker=None, check_interval: int = CHECK_INTERVAL,
                 max_position_pct: float = 0.20,
                 selector_top_n: int = 5,
                 daily_selector_refresh: bool = True):
        """
        broker: BrokerBase instance (e.g. PaperBroker). 如果不传，只推送不下单。
        max_position_pct: 每笔买入占总现金的比例（默认 20%）
        selector_top_n: 动态选股取前N（默认 5）
        daily_selector_refresh: 每天开盘前刷新一次选股列表（默认 True）
        """
        self._svc       = svc
        self._broker    = broker
        self._interval  = check_interval
        self._stop_evt  = threading.Event()
        self._thread:   Optional[threading.Thread] = None
        self._cooldown  = CooldownTracker()
        self._running   = False
        self._max_pos_pct = max_position_pct
        self._selector_top_n = selector_top_n
        self._daily_refresh = daily_selector_refresh
        self._selector_cache: list = []
        self._selector_loaded_date: str = ''

    # ── Public API ────────────────────────────────────────

    def start(self):
        if self._running:
            logger.warning('Monitor already running')
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name='IntradayMonitor', daemon=True)
        self._thread.start()
        self._running = True
        logger.info('IntradayMonitor started (interval=%ds)', self._interval)

    def stop(self):
        self._stop_evt.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._running = False
        logger.info('IntradayMonitor stopped')

    @property
    def is_running(self) -> bool:
        return self._running

    # ── Internal ───────────────────────────────────────────

    def _run(self):
        logger.info('Monitor thread active, checking market hours...')

        while not self._stop_evt.is_set():
            now = datetime.now()

            if not is_market_open(now):
                # 非交易时段：sleep 到下次开盘
                wait = next_market_seconds(now)
                logger.info('Market closed. Sleeping %ds until next open', wait)
                # 分段 sleep，方便快速响应 stop
                for _ in range(min(wait, 3600)):  # 最多等1小时再检查
                    if self._stop_evt.wait(timeout=1):
                        return
                continue

            # 交易时段：检查信号
            try:
                self._check_and_push(now)
            except Exception as e:
                logger.error('Signal check error: %s', e)

            # 清理过期冷却记录
            self._cooldown.purge_old()

            # 等待下次检查
            self._stop_evt.wait(timeout=self._interval)

    def _calc_shares(self, symbol: str, price: float) -> int:
        """
        根据可用现金和 max_position_pct 计算可买股数。
        A 股最小买入单位：100 股（1 手）。
        """
        try:
            cash = self._svc.get_cash()
        except Exception:
            cash = 0
        if cash <= 0 or price <= 0:
            return 0
        max_cost = cash * self._max_pos_pct
        raw_shares = int(max_cost / price)
        # 向下取整到100股的整数倍
        return (raw_shares // 100) * 100

    def _load_selector_once(self):
        """每天开盘前只加载一次动态选股结果。"""
        today = date.today().isoformat()
        if self._selector_loaded_date == today and self._selector_cache:
            return  # 已刷新，跳过
        self._selector_loaded_date = today
        self._selector_cache = []
        if not self._broker:
            return
        try:
            import sys as _sys
            PROJ_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            if PROJ_DIR not in _sys.path:
                _sys.path.insert(0, PROJ_DIR)
            from scripts.dynamic_selector import DynamicStockSelectorV2
            sel = DynamicStockSelectorV2()
            sel.fetch_market_news(30)
            sel.fetch_sectors()
            sel.calc_all_scores()
            selected = sel.select_stocks(self._selector_top_n)
            self._selector_cache = selected
            logger.info('DynamicSelector: loaded %d stocks for today', len(selected))
        except Exception as e:
            logger.warning('DynamicSelector: failed to load: %s', e)
            self._selector_cache = []

    def _get_watched_symbols(self) -> set:
        """返回今日动态选股列表（仅未持仓的标的）。"""
        self._load_selector_once()
        existing = {p.get('symbol') for p in self._svc.get_positions() if p.get('symbol')}
        return {s for s in self._selector_cache if s not in existing}

    def _check_new_positions(self, now: datetime):
        """
        检查动态选股列表中的标的，如有买入信号则自动建仓。
        """
        watched = self._get_watched_symbols()
        if not watched:
            return

        check_time = now.strftime('%H:%M')
        for sym in watched:
            # 冷却：每天每个标的只尝试一次（用 new_ 前缀区分）
            if not self._cooldown.can_fire(f'new_{sym}'):
                continue
            try:
                alert = evaluate_signal(sym, rsi_buy=35, rsi_sell=70)
                if not alert:
                    continue
                if alert.signal not in ('RSI_BUY', 'WATCH_BUY'):
                    continue
                # 分钟确认
                confirmed, m_rsi, reason = confirm_signal_minute(sym, 'BUY')
                logger.info('DynamicSelector %s @ %.2f: minute_rsi=%s → %s',
                           sym, alert.price,
                           f'{m_rsi:.0f}' if m_rsi else 'N/A', reason)
                if not confirmed:
                    self._deliver_alert(
                        f'🚫 [{sym}] 动态选股触发但分钟RSI拒绝建仓\n'
                        f'   现价：{alert.price:.2f} | {reason}'
                    )
                    continue

                shares = self._calc_shares(sym, alert.price)
                if shares < 100:
                    continue
                result = self._broker.submit_order(
                    symbol=sym, direction='BUY',
                    shares=shares, price=alert.price, price_type='market',
                )
                status_str = '✅ 成交' if result.status == 'filled' else f'❌ {result.status}'
                self._deliver_alert(
                    f'🆕[{sym}] 自动建仓（动态选股→分钟确认）\n'
                    f'   {status_str} {shares}股 @ {result.avg_price:.2f}\n'
                    f'   原因: {alert.reason} | {reason}'
                )
                logger.info('DynamicSelector auto BUY %s %d @ %.2f => %s',
                           sym, shares, result.avg_price, result.status)
            except Exception as e:
                logger.error('DynamicSelector check %s error: %s', sym, e)

    def _submit_order_for_signal(self, alert: SignalAlert):
        """将信号转换为订单并提交（含分钟级二次确认）。"""
        signal = alert.signal

        # 涨跌停等不交易
        if signal in self.NO_TRADE_SIGNALS:
            logger.debug('Skipping order for signal %s (no-trade signal)', signal)
            return None

        direction = self.SIGNAL_TO_ORDER.get(signal)
        if not direction:
            logger.debug('No order mapping for signal %s', signal)
            return None

        # 分钟确认（仅对 BUY 信号）
        if direction == 'BUY':
            confirmed, m_rsi, reason = confirm_signal_minute(alert.symbol, 'BUY')
            logger.info('Minute confirm %s %s: %s', alert.symbol, alert.signal, reason)
            if not confirmed:
                self._deliver_alert(
                    f'⚠️ [{alert.symbol}] 持仓信号触发但分钟RSI拒绝追高\n'
                    f'   现价：{alert.price:.2f} | {reason}'
                )
                return None

        # 计算股数
        shares = self._calc_shares(alert.symbol, alert.price)
        if shares < 100:
            logger.warning('Insufficient cash for %s: calculated %d shares (min 100)',
                           alert.symbol, shares)
            return None

        # 卖出时用全部持仓
        if direction == 'SELL':
            pos = self._svc.get_position(alert.symbol)
            if not pos or pos.get('shares', 0) == 0:
                logger.debug('No position to sell for %s', alert.symbol)
                return None
            shares = (pos['shares'] // 100) * 100  # 整手

        # 提交订单
        try:
            result = self._broker.submit_order(
                symbol=alert.symbol,
                direction=direction,
                shares=shares,
                price=alert.price,
                price_type='market',
            )
            logger.info('Auto-order: %s %s %d @ %.2f => %s',
                        direction, alert.symbol, shares, alert.price, result.status)
            return result
        except Exception as e:
            logger.error('Order submission failed for %s: %s', alert.symbol, e)
            return None

    def _check_and_push(self, now: datetime):
        """获取持仓 → 检查信号 → 推送飞书 + 自动下单"""
        # 获取当前持仓
        try:
            positions = self._svc.get_positions()
        except Exception as e:
            logger.warning('get_positions failed: %s', e)
            return

        if not positions:
            logger.debug('No positions, skipping signal check')
            return

        # 只保留有 symbol 的持仓
        pos_list = [
            {'symbol': p.get('symbol'), 'shares': p.get('shares', 0),
             'rsi_buy': 35, 'rsi_sell': 70}
            for p in positions
            if p.get('symbol')
        ]
        if not pos_list:
            return

        # 检查信号
        from services.signals import check_portfolio_signals
        alerts = check_portfolio_signals(pos_list)
        if not alerts:
            logger.debug('No signals at %s', now.strftime('%H:%M'))
            return

        # 过滤冷却期内标的
        actionable = [a for a in alerts if self._cooldown.can_fire(a.symbol)]
        if not actionable:
            logger.debug('All alerts in cooldown at %s', now.strftime('%H:%M'))
            return

        # 推送飞书
        check_time = now.strftime('%H:%M')
        msg = format_feishu_message(actionable, check_time)
        if msg:
            self._deliver_alert(msg)
            logger.info('Pushed %d alerts to Feishu at %s', len(actionable), check_time)

        # 自动下单（如果有 broker）
        if self._broker:
            for alert in actionable:
                self._submit_order_for_signal(alert)

        # ── 动态选股：主动建仓检查 ─────────────────────────
        if self._broker and self._daily_refresh:
            self._check_new_positions(now)

        # ── ATR 止损检查（每次轮询都检查）──────────────────
        if self._broker:
            self._check_stop_losses(positions, now)

        # ── 止盈检查（ATR移动止盈 + 固定止盈）──────────────
        if self._broker:
            self._check_take_profits(positions, now)

    def _check_take_profits(self, positions, now: datetime):
        """
        对持仓检查止盈条件（优先用 params.json 配置）：
        1. ATR 移动止盈（Chandelier Exit）：峰值回撤超过 2×ATR 时触发
        2. 固定止盈：涨幅达到 take_profit_pct 时触发
        触发 → 市价卖出 → 推送飞书。
        """
        from services.signals import (
            fetch_realtime,
            check_fixed_take_profit,
            check_atr_trailing_stop,
        )
        # 读取 params.json 中的止盈配置
        tp_pct = 0.25  # 默认25%
        try:
            import json as _json
            params_path = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                '..', 'params.json'
            )
            if os.path.exists(params_path):
                with open(params_path, 'r', encoding='utf-8') as f:
                    params = _json.load(f)
                tp_pct = params.get('strategies', {}).get('RSI', {}).get(
                    'params', {}).get('take_profit', 0.25)
        except Exception:
            pass

        for pos in positions:
            sym = pos.get('symbol')
            if not sym:
                continue
            shares = pos.get('shares', 0)
            if shares <= 0:
                continue
            entry_price = pos.get('entry_price', 0)
            peak_price = pos.get('peak_price', 0) or entry_price
            if entry_price <= 0:
                continue

            snap = fetch_realtime(sym)
            if not snap or snap.get('price', 0) <= 0:
                continue
            current_price = snap['price']

            # 同时更新持仓峰值（内存层面）
            if current_price > peak_price:
                peak_price = current_price

            # 止盈冷却 key
            tp_key = f'tp_{sym}'

            # 1. ATR 移动止盈（优先，让利润奔跑）
            atr_triggered, atr_stop, atr_reason = check_atr_trailing_stop(
                sym, peak_price, entry_price, current_price,
                atr_period=14, atr_multiplier=2.0)
            logger.debug('TakeProfit ATR %s @ %.2f: %s', sym, current_price, atr_reason)

            # 2. 固定止盈
            fixed_triggered, fixed_target, fixed_reason = check_fixed_take_profit(
                entry_price, current_price, tp_pct=tp_pct)
            logger.debug('TakeProfit fixed %s @ %.2f: %s', sym, current_price, fixed_reason)

            # 哪个先触发用哪个（取更早的信号）
            triggered = atr_triggered or fixed_triggered
            if not triggered:
                continue

            # 优先报告 ATR 移动止盈（更智能）
            if atr_triggered:
                reason = atr_reason
                label = 'ATR移动止盈'
            else:
                reason = fixed_reason
                label = f'固定止盈{tp_pct*100:.0f}%'

            # 冷却检查
            if not self._cooldown.can_fire(tp_key):
                continue

            sell_shares = (shares // 100) * 100
            try:
                result = self._broker.submit_order(
                    symbol=sym, direction='SELL',
                    shares=sell_shares, price=current_price, price_type='market',
                )
                status_str = '✅ 成交' if result.status == 'filled' else f'❌ {result.status}'
                self._deliver_alert(
                    f'🎯[{sym}] {label}触发（自动止盈）\n'
                    f'   {status_str} {sell_shares}股 @ {result.avg_price:.2f}\n'
                    f'   原因: {reason}'
                )
                logger.info('TakeProfit SELL %s %d @ %.2f => %s',
                           sym, sell_shares, result.avg_price, result.status)
            except Exception as e:
                logger.error('TakeProfit order failed for %s: %s', sym, e)

    def _check_stop_losses(self, positions, now: datetime):
        """
        对持仓逐个检查 ATR 动态止损。
        触发止损 → 市价卖出 → 推送飞书。
        """
        from services.signals import (
            fetch_realtime, check_position_stop_loss,
        )
        check_time = now.strftime('%H:%M')

        for pos in positions:
            sym = pos.get('symbol')
            if not sym:
                continue

            shares = pos.get('shares', 0)
            if shares <= 0:
                continue

            entry_price = pos.get('entry_price', 0)
            if entry_price <= 0:
                continue

            # 获取最新价
            snap = fetch_realtime(sym)
            if not snap or snap.get('price', 0) <= 0:
                continue
            current_price = snap['price']

            # 检查止损
            triggered, stop_price, reason = check_position_stop_loss(
                sym, entry_price, current_price,
                atr_period=14, atr_multiplier=2.0, fixed_sl_pct=0.08,
            )
            logger.debug('StopLoss check %s @ %.2f (entry %.2f): %s',
                        sym, current_price, entry_price, reason)

            if not triggered:
                continue

            # 冷却检查（止损触发后 15 分钟内不重复）
            sl_key = f'sl_{sym}'
            if not self._cooldown.can_fire(sl_key):
                continue

            # 执行止损卖出（全部清仓）
            sell_shares = (shares // 100) * 100
            try:
                result = self._broker.submit_order(
                    symbol=sym,
                    direction='SELL',
                    shares=sell_shares,
                    price=current_price,
                    price_type='market',
                )
                status_str = '✅ 成交' if result.status == 'filled' else f'❌ {result.status}'
                self._deliver_alert(
                    f'🛑[{sym}] ATR止损触发（自动平仓）\n'
                    f'   {status_str} {sell_shares}股 @ {result.avg_price:.2f}\n'
                    f'   止损价：{stop_price:.2f} | 当前价：{current_price:.2f}\n'
                    f'   原因: {reason}'
                )
                logger.info('StopLoss SELL %s %d @ %.2f => %s',
                           sym, sell_shares, result.avg_price, result.status)
            except Exception as e:
                logger.error('StopLoss order failed for %s: %s', sym, e)

    def _deliver_alert(self, text: str):
        """通过飞书 IM API 推送文本消息给用户。"""
        app_id = 'cli_a9217a3f3f389cc2'
        app_secret = '5kOAKAmFzhySMYQB9nV5ndInIlWS43mt'
        user_open_id = 'ou_b8add658ac094464606af32933a02d0b'

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # 1. 获取 tenant_access_token
        try:
            token_url = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
            payload = json.dumps({'app_id': app_id, 'app_secret': app_secret}).encode()
            req = urllib.request.Request(token_url, data=payload,
                                        headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
                token_result = json.loads(resp.read())
            token = token_result.get('tenant_access_token', '')
            if not token:
                logger.warning('Feishu: no tenant_access_token returned: %s', token_result)
                return
        except Exception as e:
            logger.error('Feishu token request failed: %s', e)
            return

        # 2. 发送消息
        try:
            send_url = 'https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id'
            headers = {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}
            msg_payload = json.dumps({
                'receive_id': user_open_id,
                'msg_type': 'text',
                'content': json.dumps({'text': text})
            }).encode()
            req2 = urllib.request.Request(send_url, data=msg_payload, headers=headers, method='POST')
            with urllib.request.urlopen(req2, timeout=8, context=ctx) as resp2:
                result = json.loads(resp2.read())
                code = result.get('code', -1)
                if code == 0:
                    logger.info('Feishu push succeeded: msg_id=%s', result.get('data', {}).get('message_id'))
                else:
                    logger.warning('Feishu push code=%s: %s', code, result.get('msg'))
        except Exception as e:
            logger.error('Feishu send failed: %s', e)
