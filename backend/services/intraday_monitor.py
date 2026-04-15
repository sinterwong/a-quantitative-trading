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
                 daily_selector_refresh: bool = True,
                 llm_service=None):
        """
        broker: BrokerBase instance (e.g. PaperBroker). 如果不传，只推送不下单。
        max_position_pct: 每笔买入占总现金的比例（默认 20%）
        selector_top_n: 动态选股取前N（默认 5）
        daily_selector_refresh: 每天开盘前刷新一次选股列表（默认 True）
        llm_service: LLMService instance. 如果不传，新闻情绪检查被跳过。
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
        # WFA 参数缓存（每天刷新一次）
        self._params_cache: dict = {}
        self._params_cache_date: str = ''
        # LLM 新闻情绪服务（可空）
        self._llm = llm_service
        # 新闻情绪缓存：{symbol: (sentiment, confidence, summary, date)}  每天刷新
        self._sentiment_cache: dict = {}
        self._sentiment_cache_date: str = ''
        # 组合熔断追踪
        self._peak_equity: float = 0.0
        self._risk_warn_fired: bool = False   # 8% 熔断已触发（当天不重复推送）
        self._risk_stop_fired: bool = False   # 12% 熔断已触发
        # 组合风控参数
        self._dd_warn: float = 0.08    # 8% 回撤警告
        self._dd_stop: float = 0.12    # 12% 回撤清仓
        # Kelly 仓位
        self._kelly_pct: float = 0.10   # 默认 10%，每交易日根据历史交易更新
        self._kelly_last_updated: str = ''  # ISO date string

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
        根据 Kelly 仓位比例计算可买股数（整手 100 股）。
        使用 _kelly_pct（0.0~1.0）作为仓位比例。
        """
        try:
            cash = self._svc.get_cash()
        except Exception:
            cash = 0
        if cash <= 0 or price <= 0:
            return 0
        max_cost = cash * self._kelly_pct
        raw_shares = int(max_cost / price)
        return max(100, (raw_shares // 100) * 100)

    # ── 新闻情绪检查（Method A & B 共享）───────────────────────

    BEARISH_BLOCK_CONFIDENCE = 0.60  # 空方置信度 >此值则阻止建仓/换仓

    def _check_news_sentiment(self, symbol: str) -> tuple[bool, Optional[str], Optional[float], Optional[str]]:
        """
        检查标的的新闻情绪。

        Returns:
            (blocked, sentiment, confidence, summary)
            blocked=True  → 新闻情绪强烈看空，不应建仓/不追加
            blocked=False → 可以交易（或无法获取情绪）

        情绪缓存：每天早上刷新一次（盘中不重复请求 LLM）。
        """
        today = date.today().isoformat()
        if self._sentiment_cache_date != today:
            self._sentiment_cache = {}
            self._sentiment_cache_date = today

        # 缓存命中
        if symbol in self._sentiment_cache:
            sent, conf, summ = self._sentiment_cache[symbol]
            blocked = (sent == 'bearish' and conf >= self.BEARISH_BLOCK_CONFIDENCE)
            return blocked, sent, conf, summ

        if self._llm is None:
            return False, None, None, None

        # 构建搜索关键词：股票名称 + "板块" + "利好/利空"
        # 从持仓 params 拿股票名称（兜底用代码）
        params = self._get_params(symbol)
        name = params.get('name', symbol)

        news_text = f"{name} ({symbol}) 最新财经新闻"

        try:
            result = self._llm.analyze_news(news_text, timeout=12)
            sentiment = getattr(result, 'sentiment', 'neutral')
            confidence = getattr(result, 'confidence', 0.0)
            summary = getattr(result, 'summary', '')
            self._sentiment_cache[symbol] = (sentiment, confidence, summary)
            blocked = (sentiment == 'bearish' and confidence >= self.BEARISH_BLOCK_CONFIDENCE)
            logger.info(
                'NewsSentiment %s: sentiment=%s conf=%.2f blocked=%s',
                symbol, sentiment, confidence, blocked
            )
            return blocked, sentiment, confidence, summary
        except Exception as e:
            logger.warning('NewsSentiment %s failed: %s', symbol, e)
            self._sentiment_cache[symbol] = ('unknown', 0.0, '')
            return False, 'unknown', 0.0, ''

    # ── 每日动态选股 + 新闻过滤（Method B）───────────────────────

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
            # ── Method B：新闻情绪过滤 ──────────────────────────
            if self._llm is not None:
                filtered = []
                for sym in selected:
                    blocked, sent, conf, summ = self._check_news_sentiment(sym)
                    if blocked:
                        logger.info('DynamicSelector: %s blocked by news sentiment (%s conf=%.2f)',
                                   sym, sent, conf)
                        self._deliver_alert(
                            f'\u26d4[{sym}] 开盘前新闻情绪过滤\n'
                            f'   情绪：{sent}（置信度 {conf:.0%}）\n'
                            f'   摘要：{summ[:60] if summ else "无"}\n'
                            f'   原因：利空强烈，暂不纳入候选'
                        )
                    else:
                        filtered.append(sym)
                selected = filtered

            self._selector_cache = selected
            logger.info('DynamicSelector: loaded %d stocks (after news filter)', len(selected))
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
        from services.signals import evaluate_signal, confirm_signal_minute
        watched = self._get_watched_symbols()
        if not watched:
            return

        check_time = now.strftime('%H:%M')
        for sym in watched:
            # 冷却：每天每个标的只尝试一次（用 new_ 前缀区分）
            if not self._cooldown.can_fire(f'new_{sym}'):
                continue
            try:
                params = self._get_params(sym)
                alert = evaluate_signal(
                    sym,
                    rsi_buy=int(params.get('rsi_buy', 25)),
                    rsi_sell=int(params.get('rsi_sell', 65)),
                    atr_threshold=float(params.get('atr_threshold', 0.90)),
                    positions=existing,
                )
                if not alert:
                    continue
                if alert.signal not in ('RSI_BUY', 'WATCH_BUY', 'HOLD', 'RSI_SELL', 'WATCH_SELL'):
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


                # Method A: news sentiment check before buying (new position)
                if self._llm is not None:
                    blocked, sent, conf, summ = self._check_news_sentiment(sym)
                    if blocked:
                        self._deliver_alert(
                            f'\u26d4[{sym}] \u65b0\u95fb\u60c5\u7eea\u5229\u7a7a\uff0c\u62d2\u7edd\u5efa\u4ed3\n'
                            f'   \u60c5\u7eea\uff1a{sent}\uff08\u7f6e\u4fe1\u5ea6 {conf:.0%}\uff09\n'
                            f'   \u6458\u8981\uff1a{summ[:80] if summ else "\u65e0"}'
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

        # Method A: news sentiment check before buying (existing position)
        if self._llm is not None and direction == 'BUY':
            blocked, sent, conf, summ = self._check_news_sentiment(alert.symbol)
            if blocked:
                self._deliver_alert(
                    f'\u26d4[{alert.symbol}] \u65b0\u95fb\u60c5\u7eea\u5229\u7a7a\uff0c\u6682\u505c\u4e70\u5165\\n'
                    f'   \u60c5\u7eea\uff1a{sent}\uff08\u7f6e\u4fe1\u5ea6 {conf:.0%}\uff09\\n'
                    f'   \u6458\u8981\uff1a{summ[:80] if summ else "\u65e0"}\\n'
                )
                return None
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

    def _get_params(self, symbol: str) -> dict:
        """
        返回股票的参数集（WFA优先，fallback到params.json）。
        每天刷新一次缓存。
        """
        today = date.today().isoformat()
        if self._params_cache_date != today:
            self._params_cache = {}
            self._params_cache_date = today
            self._refresh_kelly_from_trades()
        if symbol not in self._params_cache:
            from services.signals import load_symbol_params
            self._params_cache[symbol] = load_symbol_params(symbol)
        return self._params_cache[symbol]

    def _refresh_kelly_from_trades(self):
        """
        每交易日上午 9:05（params_cache 刷新时）根据历史交易记录更新 Kelly 仓位。
        从 PortfolioService.get_trades() 获取全部历史交易，计算 P&L 后更新 _kelly_pct。
        """
        try:
            import sys, os
            for k in list(os.environ.keys()):
                if 'proxy' in k.lower(): del os.environ[k]
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from scripts.quant.position_sizer import compute_kelly_from_trades

            trades_raw = self._svc.get_trades(limit=500)
            if not trades_raw:
                return
            trades = [{'pnl': float(t.get('pnl', 0))} for t in trades_raw]
            new_kelly = compute_kelly_from_trades(trades)

            if abs(new_kelly - self._kelly_pct) > 0.005:
                logger.info('Kelly updated: %.1f%% -> %.1f%% (from %d trades)',
                           self._kelly_pct * 100, new_kelly * 100, len(trades))
            self._kelly_pct = new_kelly
            self._kelly_last_updated = date.today().isoformat()
        except Exception as e:
            logger.warning('_refresh_kelly_from_trades failed: %s', e)

    def _check_and_push(self, now: datetime):
        """获取持仓 → 检查信号 → 推送飞书 + 自动下单（使用WFA优化参数）"""

        # ── 大盘指数异动检查（每次轮询都检查，独立冷却）─────────
        try:
            self._check_market_index(now)
        except Exception as e:
            logger.warning('Market index check error: %s', e)

        # ── 自选股异动检查 ───────────────────────────────────
        try:
            self._check_watchlist(now)
        except Exception as e:
            logger.warning('Watchlist check error: %s', e)

        # ── 板块资金流向突变检查 ─────────────────────────────
        try:
            self._check_sector_flow(now)
        except Exception as e:
            logger.warning('Sector flow check error: %s', e)

        # 获取当前持仓
        try:
            positions = self._svc.get_positions()
        except Exception as e:
            logger.warning('get_positions failed: %s', e)
            return

        if not positions:
            logger.debug('No positions, skipping signal check')
            self._peak_equity = self._svc.get_portfolio_summary().get('total_equity', 0) or self._peak_equity
            self._risk_warn_fired = False
            self._risk_stop_fired = False
            return

        # ── 组合熔断检查 ─────────────────────────────────
        self._check_portfolio_risk(positions)

        # ── 行业集中度检查 ─────────────────────────────────
        try:
            self._check_sector_concentration(positions)
        except Exception as e:
            logger.warning('Sector concentration check error: %s', e)

        # 使用 WFA 优化参数逐个检查持仓信号
        from services.signals import evaluate_signal, format_feishu_message
        alerts = []
        for pos in positions:
            sym = pos.get('symbol')
            if not sym:
                continue
            params = self._get_params(sym)
            alert = evaluate_signal(
                sym,
                rsi_buy=int(params.get('rsi_buy', 25)),
                rsi_sell=int(params.get('rsi_sell', 65)),
                atr_threshold=float(params.get('atr_threshold', 0.90)),
                positions=positions,
            )
            if alert:
                alerts.append(alert)
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

        # 自动下单（使用 per-symbol 参数）
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

    def _submit_market_sell(self, sym, shares, reason=""):
        """Helper: market sell for portfolio risk exits."""
        class _F:
            def __init__(self, s, sh, r):
                self.symbol = s; self.shares = sh; self.signal = "RSI_SELL"
                self.price = 0.0; self.reason = r; self.direction = "SELL"
        self._submit_order_for_signal(_F(sym, shares, reason))


    def _check_portfolio_risk(self, positions):
        """DD cascade: 8pct warn, 12pct stop."""
        try:
            summary = self._svc.get_portfolio_summary(refresh_prices_now=True)
        except Exception as e:
            logger.warning("get_portfolio_summary failed: %s", e)
            return
        current_equity = summary.get("total_equity", 0)
        if not current_equity or current_equity <= 0:
            return
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity
            self._risk_warn_fired = False
            self._risk_stop_fired = False
            logger.debug("Portfolio peak updated: %.2f", self._peak_equity)
            return
        drawdown = (self._peak_equity - current_equity) / self._peak_equity
        now_str = datetime.now().strftime("%H:%M")

        # 8pct warning: reduce to 50pct
        if drawdown >= self._dd_warn and not self._risk_warn_fired:
            self._risk_warn_fired = True
            msg = "[WARNING] Portfolio DD warning DD: %.1f%% (threshold %.0f%%)\n" % (
                   drawdown * 100, self._dd_warn * 100)
            msg += " Equity: %.2f  Peak: %.2f  Time: %s\n" % (
                   current_equity, self._peak_equity, now_str)
            msg += " ACTION: Reduce position to 50pct"
            self._deliver_alert(msg)
            if self._broker:
                for pos in positions:
                    sym = pos.get("symbol")
                    shares = pos.get("shares", 0)
                    if shares > 0:
                        half = shares // 2
                        if half > 0:
                            self._submit_market_sell(sym, half, reason="portfolio_risk_reduce")
            # Return after warn action - stop will be checked on next poll if DD grows
            return

        # 12pct stop: full liquidation
        if drawdown >= self._dd_stop and not self._risk_stop_fired:
            self._risk_stop_fired = True
            msg = "[EMERGENCY] Portfolio cascade STOP! DD: %.1f%% (threshold %.0f%%)\n" % (
                   drawdown * 100, self._dd_stop * 100)
            msg += " Equity: %.2f  Peak: %.2f  Time: %s\n" % (
                   current_equity, self._peak_equity, now_str)
            msg += " ACTION: FULL LIQUIDATION"
            self._deliver_alert(msg)
            if self._broker:
                for pos in positions:
                    sym = pos.get("symbol")
                    shares = pos.get("shares", 0)
                    if shares > 0:
                        self._submit_market_sell(sym, shares, reason="portfolio_cascade_stop")
            return

        logger.debug("Portfolio DD=%.1f%% warn_fired=%s", drawdown * 100, self._risk_warn_fired)

    def _check_sector_concentration(self, positions: list):
        """
        检查行业集中度风险。
        单一行业 > 40% 权益 → 推送飞书警告 + 强制减仓至 40%。
        """
        try:
            from services.portfolio import check_sector_concentration
        except Exception:
            return

        violations = check_sector_concentration(positions, max_sector_pct=0.40)
        if not violations:
            return

        for v in violations:
            logger.warning('Sector concentration violation: %s=%.1f%% (max 40%%)',
                          v['sector'], v['pct'])
            msg = (
                f'[WARNING] 行业集中度风险！\n'
                f'  行业: {v["sector"]}\n'
                f'  当前占比: {v["pct"]}% (上限 40%)\n'
                f'  需减仓: {v["reduce_value"]:.0f}元 ({v["reduce_pct"]}% 仓位)\n'
                f'  时间: {datetime.now().strftime("%H:%M")}\n'
                f'  ACTION: 减仓至 40%'
            )
            self._deliver_alert(msg)

            # 自动减仓（broker 模式下）
            if self._broker:
                from services.portfolio import _load_sector_map
                sector_map = _load_sector_map()
                for pos in positions:
                    sym = pos.get('symbol', '')
                    shares = pos.get('shares', 0)
                    if shares <= 0:
                        continue
                    sym_key = sym.replace('.SH', '').replace('.SZ', '')
                    for key, name in sector_map.items():
                        if sym_key.startswith(key) or key in sym_key:
                            if name == v['sector']:
                                half = shares // 2
                                if half >= 100:
                                    self._submit_market_sell(sym, half, reason='sector_concentration')
                            break


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

            params = self._get_params(sym)
            tp_pct = params.get('take_profit', 0.25)
            atr_multiplier = params.get('atr_multiplier', 3.0)  # Chandelier Exit: 3x ATR

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
                atr_period=int(params.get('atr_period', 14)),
                atr_multiplier=atr_multiplier)
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
                label = f'ATR移动止盈({atr_multiplier}x)'
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

            params = self._get_params(sym)

            # 获取最新价
            snap = fetch_realtime(sym)
            if not snap or snap.get('price', 0) <= 0:
                continue
            current_price = snap['price']

            # 检查止损（per-symbol params，WFA 优先）
            triggered, stop_price, reason = check_position_stop_loss(
                sym, entry_price, current_price,
                atr_period=int(params.get('atr_period', 14)),
                atr_multiplier=params.get('atr_multiplier', 2.0),
                fixed_sl_pct=params.get('stop_loss', 0.08),
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

    # ── 大盘指数监控 ───────────────────────────────────────────────
    # 监控的指数及其预警阈值（涨跌幅绝对值超过此值则告警）
    INDEX_CONFIG = {
        'sh000001': {'name': '上证指数', 'alert_pct': 1.5},
        'sz399001': {'name': '深证成指', 'alert_pct': 1.5},
        'sz399006': {'name': '创业板指', 'alert_pct': 2.0},
        'sh000688': {'name': '科创50',   'alert_pct': 2.0},
        'sh000300': {'name': '沪深300', 'alert_pct': 1.5},
    }

    def _fetch_index_data(self) -> dict:
        """获取所有监控指数的当前行情"""
        from services.signals import fetch_bulk
        codes = list(self.INDEX_CONFIG.keys())
        result = fetch_bulk(codes)
        return result

    def _check_market_index(self, now: datetime):
        """
        检查大盘指数是否出现显著异动（涨跌超过阈值）。
        发现异动 → 推送飞书 + 记录 alert_history。
        冷却：每只指数 30 分钟内不重复告警。
        """
        data = self._fetch_index_data()
        if not data:
            logger.debug('Index data fetch failed')
            return

        for code, cfg in self.INDEX_CONFIG.items():
            sym_key = code  # fetch_bulk returns with original code as key
            # find matching key
            row = None
            for k, v in data.items():
                if k.upper().replace('.SH', '').replace('.SZ', '').replace('SH', '').replace('SZ', '') == code.replace('sh', '').replace('sz', '').upper():
                    row = v
                    break
            if not row:
                continue

            pct = row.get('pct', 0)
            abs_pct = abs(pct)
            threshold = cfg['alert_pct']
            if abs_pct < threshold:
                continue

            # 冷却检查
            cooldown_key = f'idx_{code}'
            if not self._cooldown.can_fire(cooldown_key):
                continue

            direction = '暴涨' if pct > 0 else '暴跌'
            emoji = '🚀' if pct > 0 else ('🚨' if pct < -2 else '⚠️')
            name = cfg['name']
            price = row.get('price', 0)
            msg = (
                f'{emoji}【大盘异动】{name}{direction}\n'
                f'   当前: {price} ({pct:+.2f}%)\n'
                f'   阈值: ±{threshold}% | 时间: {now.strftime("%H:%M")}'
            )
            self._deliver_alert(msg)
            from services.alert_history import record_alert
            record_alert('INDEX', msg, symbol=code, price=price, pct_change=pct)
            logger.info('Market index alert: %s %+.2f%%', name, pct)

    # ── 自选股监控 ───────────────────────────────────────────────

    def _check_watchlist(self, now: datetime):
        """
        检查自选股列表中的股票是否出现异动（涨跌幅超过各股阈值）。
        阈值默认 5%，可在 watchlist 表中逐股配置。
        只做预警推送，不自动交易。
        """
        from services.watchlist import get_watchlist, get_stock_alert_pct
        from services.signals import fetch_bulk

        watchlist = get_watchlist()
        if not watchlist:
            return

        codes = [w['symbol'] for w in watchlist]
        data = fetch_bulk(codes)
        if not data:
            return

        for w in watchlist:
            sym = w['symbol']
            # 找到匹配的行
            row = None
            for k, v in data.items():
                if k.upper().replace('.SH', '').replace('.SZ', '').replace('SH', '').replace('SZ', '') == sym.replace('.SH', '').replace('.SZ', '').upper():
                    row = v
                    break
            if not row:
                continue

            pct = row.get('pct', 0)
            threshold = w.get('alert_pct', 5.0)
            if abs(pct) < threshold:
                continue

            # 冷却
            cooldown_key = f'wl_{sym}'
            if not self._cooldown.can_fire(cooldown_key):
                continue

            price = row.get('price', 0)
            emoji = '🔺' if pct > 0 else '🔻'
            direction = '大涨' if pct > 0 else '大跌'
            name = w.get('name', sym)
            alert_reason = w.get('reason', '')
            reason_str = f' | 自选理由: {alert_reason}' if alert_reason else ''

            msg = (
                f'{emoji}【自选股异动】{name}({sym}) {direction}\n'
                f'   当前: {price} ({pct:+.2f}%)\n'
                f'   预警阈值: ±{threshold}%{reason_str}\n'
                f'   时间: {now.strftime("%H:%M")}'
            )
            self._deliver_alert(msg)
            from services.alert_history import record_alert
            record_alert('WATCHLIST', msg, symbol=sym, price=price, pct_change=pct)
            logger.info('Watchlist alert: %s %+.2f%%', sym, pct)

    # ── 板块资金流向监控 ───────────────────────────────────────────

    def _load_sector_flows(self):
        """加载今日板块资金流向数据（从 dynamic_selector）"""
        try:
            import sys as _sys
            PROJ_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            SCRIPTS_DIR = os.path.join(PROJ_DIR, 'scripts')
            if SCRIPTS_DIR not in _sys.path:
                _sys.path.insert(0, SCRIPTS_DIR)
            from dynamic_selector import DynamicStockSelectorV2
            sel = DynamicStockSelectorV2()
            sel.fetch_sectors()
            return sel.sector_scores  # {bk_code: {name, flow, ...}}
        except Exception as e:
            logger.debug('Sector flow load failed: %s', e)
            return {}

    def _check_sector_flow(self, now: datetime):
        """
        检查板块资金流向是否出现异常突变。
        资金流入评分从上一轮监控到这一轮出现显著提升（flow ↑>20分）→ 预警。
        每只板块 30 分钟冷却。
        """
        if not hasattr(self, '_prev_sector_flows'):
            self._prev_sector_flows = {}

        current_flows = self._load_sector_flows()
        if not current_flows:
            return

        for bk, info in current_flows.items():
            prev = self._prev_sector_flows.get(bk, {})
            prev_flow = prev.get('flow', 0)
            curr_flow = info.get('flow', 0)

            # 检测突变：资金流入评分跃升 > 20 分
            if prev_flow > 0 and (curr_flow - prev_flow) > 20:
                cooldown_key = f'sf_{bk}'
                if not self._cooldown.can_fire(cooldown_key):
                    continue

                name = info.get('name', bk)
                chg = info.get('change_pct', 0)
                chg_emoji = '🔺' if chg > 0 else '➖'
                msg = (
                    f'💰【资金异动】{name} 资金大幅流入\n'
                    f'   板块涨幅: {chg_emoji}{chg:+.2f}%\n'
                    f'   资金评分: {prev_flow:.0f} → {curr_flow:.0f} (+{curr_flow - prev_flow:.0f})\n'
                    f'   可能受消息面驱动，关注持续性\n'
                    f'   时间: {now.strftime("%H:%M")}'
                )
                self._deliver_alert(msg)
                from services.alert_history import record_alert
                record_alert('SECTOR_FLOW', msg, symbol=bk, pct_change=chg)
                logger.info('Sector flow alert: %s flow %d→%d', name, prev_flow, curr_flow)

        # 更新缓存
        self._prev_sector_flows = current_flows

    def _record_position_alert(self, alert_type: str, symbol: str, message: str,
                                price: float = None, pct: float = None):
        """记录持仓相关预警到历史"""
        try:
            from services.alert_history import record_alert
            record_alert(alert_type, message, symbol=symbol, price=price, pct_change=pct)
        except Exception as e:
            logger.debug('record_position_alert failed: %s', e)

    def _deliver_alert(self, text: str, alert_type: str = 'POSITION',
                       symbol: str = '', price: float = None, pct: float = None):
        """通过飞书 IM API 推送文本消息给用户，并记录到历史。"""
        app_id = os.environ.get('FEISHU_APP_ID', '')
        app_secret = os.environ.get('FEISHU_APP_SECRET', '')
        user_open_id = os.environ.get('FEISHU_USER_OPEN_ID', '')

        if not app_id or not app_secret or not user_open_id:
            logger.debug('Feishu not configured (FEISHU_APP_ID/SECRET/USER_OPEN_ID), skipping push')
            return

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

        # 3. 记录到预警历史
        try:
            from services.alert_history import record_alert
            record_alert(alert_type, text, symbol=symbol or '',
                          price=price, pct_change=pct)
        except Exception:
            pass  # 不因记录失败影响推送
