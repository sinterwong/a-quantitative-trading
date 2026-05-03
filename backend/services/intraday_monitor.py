"""
intraday_monitor.py - 盘中实时监控服务
========================================
后台线程,交易时段持续运行:
  - 每 5 分钟检查一次持仓信号
  - 合条件时主动推送 Feishu 消息

使用方法:
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

from .signals import (
    format_feishu_message,
    SignalAlert,
    confirm_signal_minute,
    MARKET_MORNING_START, MARKET_MORNING_END,
    MARKET_AFTERNOON_START, MARKET_AFTERNOON_END,
    TRADING_DAYS,
)

logger = logging.getLogger('intraday_monitor')

# 全局配置
CHECK_INTERVAL  = 300   # 秒(5分钟)
COOLDOWN       = 900   # 同一标的信号推送冷却时间(15分钟)


# ─── 交易日判断（与 main.py Scheduler 共享同一套逻辑） ───────

_trade_calendar: set = set()
_trade_calendar_date: str = ''


def _is_trading_day(now: datetime) -> bool:
    """判断是否为 A 股交易日（复用 Scheduler 的 AKShare 日历逻辑）。"""
    global _trade_calendar, _trade_calendar_date
    today_str = now.strftime('%Y-%m-%d')

    if _trade_calendar_date != today_str:
        try:
            import akshare as ak
            df = ak.tool_trade_date_hist_sina()
            dates = df.iloc[:, 0]
            _trade_calendar = {str(d)[:10] for d in dates}
            _trade_calendar_date = today_str
        except Exception:
            _trade_calendar = set()

    if _trade_calendar:
        return today_str in _trade_calendar

    # AKShare 不可用时降级为周一~周五判断
    return now.weekday() < 5


# ─── 交易时段判断 ─────────────────────────────────────────

def is_market_open(now: Optional[datetime] = None) -> bool:
    """判断当前是否为 A 股交易时段（节假日 + 交易时间双重判断）。"""
    if now is None:
        now = datetime.now()
    if now.weekday() >= 5 or not _is_trading_day(now):
        return False
    h, m = now.hour, now.minute

    def t(h_, m_):
        return h_ * 60 + m_

    cur = h * 60 + m
    morning     = t(*MARKET_MORNING_START) <= cur <= t(*MARKET_MORNING_END)
    afternoon   = t(*MARKET_AFTERNOON_START) <= cur <= t(*MARKET_AFTERNOON_END)
    return morning or afternoon


def next_market_seconds(now: Optional[datetime] = None) -> int:
    """距离下次开市还有多少秒(用于启动前 sleep)"""
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
    检测到信号时:
      1. 推送飞书提醒
      2. 自动提交订单(如果 broker 已注入)
    """

    # 信号 → 订单方向映射(涨跌停类不交易)
    SIGNAL_TO_ORDER = {
        'RSI_BUY':     'BUY',
        'WATCH_BUY':   'BUY',
        'RSI_SELL':    'SELL',
        'WATCH_SELL':  'SELL',
    }
    # 涨跌停类信号不交易(无法以合理价格买入/卖出)
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
                 llm_service=None,
                 strategy_runner=None):
        """
        broker: BrokerBase instance (e.g. PaperBroker). 如果不传,只推送不下单。
        max_position_pct: 每笔买入占总现金的比例(默认 20%)
        selector_top_n: 动态选股取前N(默认 5)
        daily_selector_refresh: 每天开盘前刷新一次选股列表(默认 True)
        llm_service: LLMService instance. 如果不传,新闻情绪检查被跳过。
        strategy_runner: StrategyRunner instance. 注入后可从 last_results 读取
            pipeline_scores,用于 ExitEngine 的 FACTOR_REVERSAL 检查。
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
        # StrategyRunner 引用(可在启动后通过 set_strategy_runner() 注入)
        self._strategy_runner = strategy_runner
        # WFA 参数缓存(每天刷新一次)
        self._params_cache: dict = {}
        self._params_cache_date: str = ''
        # LLM 新闻情绪服务(可空)
        self._llm = llm_service
        # 新闻情绪缓存:{symbol: (sentiment, confidence, summary, date)}  每天刷新
        self._sentiment_cache: dict = {}
        self._sentiment_cache_date: str = ''
        # 组合熔断追踪
        self._peak_equity: float = 0.0
        self._risk_warn_fired: bool = False   # 8% 熔断已触发(当天不重复推送)
        self._risk_stop_fired: bool = False   # 12% 熔断已触发
        # 市场环境缓存(LLM prompt 使用,BUG-4 fix: 必须在 __init__ 初始化)
        self._market_regime: dict = {}
        # 组合风控参数
        self._dd_warn: float = 0.08    # 8% 回撤警告
        self._dd_stop: float = 0.12    # 12% 回撤清仓
        # Kelly 仓位
        self._kelly_pct: float = 0.10   # 默认 10%,每交易日根据历史交易更新
        self._kelly_last_updated: str = ''  # ISO date string
        # 交易模式:'simulation' (默认,不执行Broker订单) | 'live' (执行Broker订单)
        self._trading_mode: str = 'simulation'
        self._load_trading_mode()
        # 策略健康监控(每日开盘时检查一次)
        self._health_check_date: str = ''

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

    @property
    def trading_mode(self) -> str:
        return self._trading_mode

    def set_trading_mode(self, mode: str):
        """动态切换交易模式:'simulation' | 'live'"""
        old = self._trading_mode
        self._trading_mode = mode
        self._save_trading_mode()
        logger.info('Trading mode changed: %s → %s', old, mode)

    def set_strategy_runner(self, runner) -> None:
        """注入 StrategyRunner 实例,用于读取 pipeline_scores。

        可在 monitor.start() 之前或之后调用;线程安全(GIL 保护的单次赋值)。
        """
        self._strategy_runner = runner
        logger.info('StrategyRunner injected into IntradayMonitor')

    def _load_trading_mode(self):
        mode_file = os.path.join(BACKEND_DIR, 'trading_mode.json')
        if os.path.exists(mode_file):
            try:
                import json
                with open(mode_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self._trading_mode = data.get('mode', 'simulation')
                logger.info('Loaded trading mode: %s', self._trading_mode)
            except Exception as e:
                logger.warning('Failed to load trading_mode.json: %s', e)
                self._trading_mode = 'simulation'
        else:
            self._trading_mode = 'simulation'

    def _save_trading_mode(self):
        mode_file = os.path.join(BACKEND_DIR, 'trading_mode.json')
        try:
            import json
            with open(mode_file, 'w', encoding='utf-8') as f:
                json.dump({'mode': self._trading_mode, 'updated_at': datetime.now().isoformat()},
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning('Failed to save trading_mode.json: %s', e)

    def _can_trade(self) -> bool:
        """检查是否允许执行实单(Broker下单)。simulation模式返回False。"""
        return self._trading_mode == 'live'

    # ── Internal ───────────────────────────────────────────

    def _run(self):
        logger.info('Monitor thread active, checking market hours...')

        while not self._stop_evt.is_set():
            now = datetime.now()

            if not is_market_open(now):
                # 非交易时段:sleep 到下次开盘
                wait = next_market_seconds(now)
                logger.info('Market closed. Sleeping %ds until next open', wait)
                # 分段 sleep,方便快速响应 stop
                for _ in range(min(wait, 3600)):  # 最多等1小时再检查
                    if self._stop_evt.wait(timeout=1):
                        return
                continue

            # 交易时段:检查信号
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
        根据 Kelly 仓位比例计算可买股数(整手 100 股)。
        使用 _kelly_pct(0.0~1.0)作为仓位比例。
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

    # ── 新闻情绪检查(Method A & B 共享)───────────────────────

    BEARISH_BLOCK_CONFIDENCE = 0.60  # 空方置信度 >此值则阻止建仓/换仓

    def _check_news_sentiment(self, symbol: str) -> tuple[bool, Optional[str], Optional[float], Optional[str]]:
        """
        检查标的的新闻情绪。

        Returns:
            (blocked, sentiment, confidence, summary)
            blocked=True  → 新闻情绪强烈看空,不应建仓/不追加
            blocked=False → 可以交易(或无法获取情绪)

        情绪缓存:每天早上刷新一次(盘中不重复请求 LLM)。
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

        # 构建搜索关键词:股票名称 + "板块" + "利好/利空"
        # 从持仓 params 拿股票名称(兜底用代码)
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

    # ── 每日动态选股 + 新闻过滤(Method B)───────────────────────

    def _load_selector_once(self):
        """每天开盘前只加载一次动态选股结果。"""
        today = date.today().isoformat()
        if self._selector_loaded_date == today and self._selector_cache:
            return  # 已刷新,跳过
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
            selected = sel.select_stocks(top_n=self._selector_top_n)  # 获取选股结果
            # ── Method B:新闻情绪过滤 ──────────────────────────
            if self._llm is not None:
                filtered = []
                for sym in selected:
                    blocked, sent, conf, summ = self._check_news_sentiment(sym)
                    if blocked:
                        logger.info('DynamicSelector: %s blocked by news sentiment (%s conf=%.2f)',
                                   sym, sent, conf)
                        self._deliver_alert(
                            f'\u26d4[{sym}] 开盘前新闻情绪过滤\n'
                            f'   情绪:{sent}(置信度 {conf:.0%})\n'
                            f'   摘要:{summ[:60] if summ else "无"}\n'
                            f'   原因:利空强烈,暂不纳入候选'
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
        """返回今日动态选股列表(仅未持仓的标的)。"""
        self._load_selector_once()
        existing = {p.get('symbol') for p in self._svc.get_positions() if p.get('symbol')}
        return {s for s in self._selector_cache if s not in existing}

    def _check_new_positions(self, now: datetime):
        """
        检查动态选股列表中的标的，如有买入信号则自动建仓。

        唯一信号来源：StrategyRunner.last_scores（FactorPipeline 动态 IC 加权）。
        evaluate_signal() 降级分支已删除（消除双信号并行架构隐患）。
        """
        from services.signals import confirm_signal_minute, fetch_realtime
        watched = self._get_watched_symbols()
        if not watched:
            return

        # 获取 pipeline scores（无 StrategyRunner 时直接跳过，不降级到 RSI 硬编码）
        pipeline_scores: dict = {}
        if self._strategy_runner is not None:
            try:
                pipeline_scores = self._strategy_runner.last_scores
            except Exception:
                pass

        if not pipeline_scores:
            logger.warning(
                '_check_new_positions: no pipeline scores (runner=%s), skipping all symbols. '
                'Check that StrategyRunner is running and FactorPipeline is producing output.',
                self._strategy_runner is not None
            )
            return

        for sym in watched:
            # 冷却：每天每个标的只尝试一次（用 new_ 前缀区分）
            if not self._cooldown.can_fire(f'new_{sym}'):
                continue
            try:
                score = pipeline_scores.get(sym)
                if score is None or score <= 0:
                    continue

                # ── FactorPipeline 信号 ──────────────────────────
                threshold = getattr(self._strategy_runner.config, 'signal_threshold', 0.5)
                if score <= threshold:
                    continue

                # 获取实时价格
                try:
                    rt = fetch_realtime(sym)
                    price = float(rt.get('price', 0)) if rt else 0
                except Exception:
                    price = 0
                if price <= 0:
                    continue

                signal_reason = f'Pipeline score={score:.3f} > threshold={threshold:.3f}'
                logger.info('Pipeline %s score=%.3f > threshold=%.3f', sym, score, threshold)

                # ── 公共安全层 ────────────────────────────────

                # 分钟确认
                confirmed, m_rsi, reason = confirm_signal_minute(sym, 'BUY')
                logger.info('DynamicSelector %s @ %.2f: minute_rsi=%s → %s',
                           sym, price,
                           f'{m_rsi:.0f}' if m_rsi else 'N/A', reason)
                if not confirmed:
                    self._deliver_alert(
                        f'🚫 [{sym}] 动态选股触发但分钟RSI拒绝建仓\n'
                        f'   现价：{price:.2f} | {reason}'
                    )
                    continue

                # 新闻情绪检查
                if self._llm is not None:
                    blocked, sent, conf, summ = self._check_news_sentiment(sym)
                    if blocked:
                        self._deliver_alert(
                            f'\u26d4[{sym}] \u65b0\u95fb\u60c5\u7eea\u5229\u7a7a\uff0c\u62d2\u7edd\u5efa\u4ed3\n'
                            f'   \u60c5\u7eea\uff1a{sent}\uff08\u7f6e\u4fe1\u5ea6 {conf:.0%}\uff09\n'
                            f'   \u6458\u8981\uff1a{summ[:80] if summ else "无"}'
                        )
                        continue

                shares = self._calc_shares(sym, price)

                # LLM 终极审核（构造兼容 alert-like 对象）
                class _PipelineAlert:
                    pass
                _pa = _PipelineAlert()
                _pa.symbol = sym
                _pa.price = price
                _pa.reason = signal_reason
                _pa.signal = 'BUY'
                llm_approved, llm_reason, llm_conf, size_rec = self._llm_review_signal(_pa, 'BUY')
                if not llm_approved:
                    self._deliver_alert(
                        f'\u274c [{sym}] LLM 审核否决新仓买入\n'
                        f'   \u7406\u7531\uff1a{llm_reason}\n'
                        f'   \u7f6e\u4fe1\u5ea6\uff1a{llm_conf:.0%}'
                    )
                    logger.info('LLM rejected new BUY %s: %s', sym, llm_reason)
                    continue
                logger.info('LLM approved new BUY %s: %s (conf=%.0f%%)', sym, llm_reason, llm_conf * 100)
                if size_rec == 'half':
                    shares = shares // 2
                if shares < 100:
                    continue

                # PreTrade 风控检查
                if self._strategy_runner is not None and self._strategy_runner.risk_engine is not None:
                    try:
                        from core.factors.base import Signal as _Sig
                        _dummy = _Sig(
                            timestamp=now, symbol=sym, direction='BUY',
                            strength=1.0, factor_name='DynamicSelector', price=price,
                        )
                        rr = self._strategy_runner.risk_engine.check(_dummy)
                        if not rr.passed:
                            logger.info('RiskEngine rejected new BUY %s: %s', sym, rr.reason)
                            continue
                    except Exception as e:
                        logger.warning('RiskEngine check failed for %s: %s', sym, e)

                if not self._can_trade():
                    self._deliver_alert(
                        f'📋 [{sym}] 模拟模式：信号触发但跳过执行\n'
                        f'   方向：BUY | 股数：{shares} | 价：{price:.2f}\n'
                        f'   原因：{signal_reason}（切换到“实盘”模式后生效）'
                    )
                    logger.info('Simulation mode: skipped BUY %s %d @ %.2f', sym, shares, price)
                    continue

                result = self._broker.submit_order(
                    symbol=sym, direction='BUY',
                    shares=shares, price=price, price_type='market',
                )
                status_str = '✅ 成交' if result.status == 'filled' else f'❌ {result.status}'
                source_tag = 'Pipeline'
                self._deliver_alert(
                    f'🆕[{sym}] 自动建仓（Pipeline→分钟确认）\n'
                    f'   {status_str} {shares}股 @ {result.avg_price:.2f}\n'
                    f'   原因: {signal_reason} | {reason}'
                )
                logger.info('DynamicSelector auto BUY %s %d @ %.2f => %s [source=%s]',
                           sym, shares, result.avg_price, result.status, source_tag)
            except Exception as e:
                logger.error('DynamicSelector check %s error: %s', sym, e)

    def _submit_order_for_signal(self, alert: SignalAlert):
        """将信号转换为订单并提交(含分钟级二次确认)。"""
        signal = alert.signal

        # 涨跌停等不交易
        if signal in self.NO_TRADE_SIGNALS:
            logger.debug('Skipping order for signal %s (no-trade signal)', signal)
            return None

        direction = self.SIGNAL_TO_ORDER.get(signal)
        if not direction:
            logger.debug('No order mapping for signal %s', signal)
            return None

        # 分钟确认(仅对 BUY 信号)
        if direction == 'BUY':
            confirmed, m_rsi, reason = confirm_signal_minute(alert.symbol, 'BUY')
            logger.info('Minute confirm %s %s: %s', alert.symbol, alert.signal, reason)
            if not confirmed:
                self._deliver_alert(
                    f'⚠️ [{alert.symbol}] 持仓信号触发但分钟RSI拒绝追高\n'
                    f'   现价:{alert.price:.2f} | {reason}'
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
                    f'   \u6458\u8981\uff1a{summ[:80] if summ else "无"}\\n'
                )
                return None

        # ══ LLM 终极审核 ══════════════════════════════════════
        if direction in ('BUY', 'SELL'):
            llm_approved, llm_reason, llm_conf, size_rec = self._llm_review_signal(alert, direction)
            if not llm_approved:
                self._deliver_alert(
                    f'\u274c [{alert.symbol}] LLM 审核否决 \u24d2{direction}\n'
                    f'   \u7406\u7531\uff1a{llm_reason}\n'
                    f'   \u7f6e\u4fe1\u5ea6\uff1a{llm_conf:.0%}'
                )
                logger.info('LLM rejected %s %s: %s (conf=%.0f)',
                           direction, alert.symbol, llm_reason, llm_conf)
                return None
            logger.info('LLM approved %s %s: %s (conf=%.0f%%)',
                       direction, alert.symbol, llm_reason, llm_conf * 100)

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
            # LLM size_rec 处理(SELL 时可能建议半仓或持有)
            if size_rec == 'hold':
                logger.info('LLM SELL hold recommended for %s: %s', alert.symbol, llm_reason)
                return None
            if size_rec == 'half':
                shares = max(100, shares // 2)

        # 提交订单
        try:
            if not self._can_trade():
                self._deliver_alert(
                    f'📋 [{alert.symbol}] 模拟模式:持仓信号跳过执行\n'
                    f'   方向:{direction} | 股数:{shares} | 价:{alert.price:.2f}\n'
                    f'   信号:{signal}(切换到"实盘"模式后生效)'
                )
                logger.info('Simulation mode: skipped %s %s %d @ %.2f', direction, alert.symbol, shares, alert.price)
                return None
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

    def _llm_review_signal(self, alert: SignalAlert, direction: str):
        """
        LLM 终极审核:收集全部上下文,让大模型决定是否执行交易。
        返回 (approved: bool, reason: str, confidence: float, size_rec: str)
        """
        if self._llm is None:
            # 无 LLM,降级为直接放行
            return True, 'LLM unavailable, auto-approve', 0.5, 'full'

        try:
            # ── 收集完整上下文 ──
            sym = alert.symbol
            params = self._get_params(sym)
            cash = self._svc.get_cash()
            positions = self._svc.get_positions()
            pos = self._svc.get_position(sym)
            recent_trades = self._svc.get_recent_trades(sym, limit=5) if hasattr(self._svc, 'get_recent_trades') else []

            # 板块/大盘信息
            try:
                from services.signals import get_market_brief
                mb = get_market_brief()
            except Exception:
                mb = {}

            # 新闻情绪(已有缓存)
            sent_key = sym
            sentiment_info = ''
            if sent_key in self._sentiment_cache:
                sent, conf_s, summ = self._sentiment_cache[sent_key]
                sentiment_info = f'情绪={sent}(置信度{conf_s:.0%}),摘要:{summ[:60]}'

            # 构建持仓摘要(提前计算避免 f-string 反斜杠问题)
            if pos:
                _pos_label = f"是({pos.get('shares', 0)}股,成本{'{:.2f}'.format(pos.get('entry_price', 0))})"
            else:
                _pos_label = "否(可建仓)"

            pos_summary = []
            for p in (positions or []):
                if p.get('shares', 0) > 0:
                    pos_summary.append(
                        f"{p['symbol']}: {p['shares']}股,成本{p.get('entry_price', 0):.2f}"
                    )

            # 近期交易摘要
            trade_summary = []
            for t in (recent_trades or []):
                trade_summary.append(
                    f"{t.get('direction','')} {t.get('symbol','')} "
                    f"{t.get('shares',0)}@{t.get('price',0):.2f} "
                    f"pnl={t.get('pnl', 0):+.0f}"
                )

            # ── 构建 LLM Prompt ──
            if direction == 'BUY':
                system_prompt = (
                    "你是一个严格的A股量化交易员。每笔买入都需要通过你的最终审核。\n"
                    "你极其重视:\n"
                    "1. 当前市场环境是否适合建仓(不要在熊市/高波动环境重仓)\n"
                    "2. RSI 是否真的处于低位(是否有足够的安全边际)\n"
                    "3. ATR 波动率是否在合理范围(排除极度高波动标的)\n"
                    "4. 板块是否处于强势(避免逆势买入)\n"
                    "5. 资金管理是否合理(单只仓位不超过25%,Kelly半仓原则)\n\n"
                    "输出严格JSON格式:\n"
                    "{\"decision\": \"approve\"或\"reject\"或\"delay\"(仅当充分理由时delay,否则reject), "
                    "\"confidence\": 0.0~1.0, "
                    "\"reason\": \"简短理由(20字内)\", "
                    "\"risk_note\": \"风险提示(如有)\", "
                    "\"size_rec\": \"full\"(按Kelly满仓)或\"half\"(半仓)或\"skip\"(跳过)\"\n"
                    "}"
                )
                user_prompt = (
                    f"【买入信号审核】\n"
                    f"标的:{sym}(名称:{params.get('name', sym)})\n"
                    f"信号类型:{alert.signal}\n"
                    f"当前价:{alert.price:.2f}(今日涨幅:{getattr(alert, 'pct', 0):+.2f}%)\n"
                    f"触发原因:{alert.reason}\n"
                    f"RSI 参数:买入阈值={params.get('rsi_buy', 25)},当前RSI≈{alert.prev_rsi:.0f if alert.prev_rsi is not None else 'N/A'}\n"
                    f"ATR 阈值:{params.get('atr_threshold', 0.85)}(当前ATR ratio={getattr(alert, 'atr_ratio', 'N/A')})\n"
                    f"市场环境:{self._market_regime.get('regime', 'UNKNOWN')}(ATR ratio={self._market_regime.get('atr_ratio', 0):.3f})\n"
                    f"大盘状态:{mb.get('趋势', '未知')} | 情绪:{mb.get('情绪', '未知')}\n"
                    f"可用现金:¥{cash:,.0f}(总权益:¥{self._svc.get_equity():,.0f})\n"
                    f"该股已有持仓:{_pos_label}\n"
                    f"当前持仓:{' | '.join(pos_summary) if pos_summary else '空仓'}\n"
                    f"近期交易:{' | '.join(trade_summary) if trade_summary else '无'}\n"
                    f"新闻情绪:{sentiment_info if sentiment_info else '无情绪数据(自动放行)'}"
                )
            else:  # SELL
                system_prompt = (
                    "你是一个纪律严明的A股交易员,专注于精准止盈止损。\n"
                    "卖出决策依据:\n"
                    "1. 止盈:是否达到预设目标(TakeProfit),趋势是否已衰竭\n"
                    "2. 止损:是否触发 ATR 止损线(Chandelier Exit),还是假突破\n"
                    "3. 仓位管理:是否需要减仓还是清仓\n"
                    "4. 相对大盘:标的是否跑输大盘(弱势股优先清仓)\n\n"
                    "输出严格JSON格式:\n"
                    "{\"decision\": \"approve\"或\"reject\"或\"hold\"(持有不卖), "
                    "\"confidence\": 0.0~1.0, "
                    "\"reason\": \"简短理由(20字内)\", "
                    "\"risk_note\": \"风险提示(如有)\", "
                    "\"size_rec\": \"full\"(清仓)或\"half\"(半仓)或\"hold\"(持有)\"\n"
                    "}"
                )
                user_prompt = (
                    f"【卖出信号审核】\n"
                    f"标的:{sym}(名称:{params.get('name', sym)})\n"
                    f"信号类型:{alert.signal}\n"
                    f"当前价:{alert.price:.2f}(持仓成本:{pos.get('entry_price', 0):.2f},浮动盈亏:{((alert.price - pos.get('entry_price', 0)) / pos.get('entry_price', 1) * 100):+.1f}%)\n"
                    f"触发原因:{alert.reason}\n"
                    f"RSI 参数:卖出阈值={params.get('rsi_sell', 65)}\n"
                    f"止盈目标:{params.get('take_profit', 0.20):.0%},止损线:{params.get('stop_loss', 0.05):.0%}\n"
                    f"市场环境:{self._market_regime.get('regime', 'UNKNOWN')}(ATR ratio={self._market_regime.get('atr_ratio', 0):.3f})\n"
                    f"持仓数量:{pos.get('shares', 0)}股(整手:{(pos.get('shares', 0) // 100) * 100}股)\n"
                    f"当前持仓:{' | '.join(pos_summary) if pos_summary else '空仓'}\n"
                    f"近期交易:{' | '.join(trade_summary) if trade_summary else '无'}\n"
                )

            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

            # 调用 LLM(通过 provider.chat)
            resp = self._llm.provider.chat(messages, max_tokens=512, temperature=0.3)
            content = resp.content.strip()

            # 解析 JSON
            import re as _re
            json_match = _re.search(r'\{[^{}]*\}', content, _re.DOTALL)
            if json_match:
                parsed = json.loads(json_match.group())
                decision = parsed.get('decision', 'reject').lower()
                confidence = float(parsed.get('confidence', 0.5))
                reason = parsed.get('reason', 'LLM review')
                size_rec = parsed.get('size_rec', 'full' if decision == 'approve' else 'skip')
                approved = decision in ('approve', 'yes')
                logger.info('LLM review %s %s: decision=%s conf=%.0f reason=%s',
                           direction, sym, decision, confidence, reason)
                return approved, reason, confidence, size_rec
            else:
                logger.warning('LLM response parse failed: %s', content[:200])
                return True, f'LLM parse failed({content[:50]}),自动放行', 0.0, 'full'

        except Exception as e:
            logger.error('LLM review error for %s: %s', alert.symbol, e)
            return True, f'LLM异常({str(e)[:30]}),自动放行', 0.0, 'full'

    def _get_params(self, symbol: str) -> dict:
        """
        返回股票的参数集(WFA优先,fallback到params.json)。
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
        每交易日上午 9:05(params_cache 刷新时)根据历史交易记录更新 Kelly 仓位。
        从 PortfolioService.get_trades() 获取全部历史交易,计算 P&L 后更新 _kelly_pct。
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

    def _sync_market_regime(self):
        """从 StrategyRunner 同步最新市场环境到 _market_regime(供 LLM prompt 使用)。"""
        if self._strategy_runner is not None:
            try:
                r = self._strategy_runner.current_regime
                if r is not None:
                    self._market_regime = {
                        'regime': r.regime,
                        'reason': r.reason,
                        'atr_ratio': getattr(r, 'atr_ratio', 0.0),
                    }
                    return
            except Exception:
                pass
        # 降级:直接调用 get_regime()
        try:
            from core.regime import get_regime
            r = get_regime()
            self._market_regime = {
                'regime': r.regime,
                'reason': r.reason,
                'atr_ratio': getattr(r, 'atr_ratio', 0.0),
            }
        except Exception:
            pass  # 保持上次缓存或空字典

    def _run_daily_health_check(self):
        """每日开盘时运行一次 StrategyHealthMonitor,检查策略健康度并推送告警。"""
        today = date.today().isoformat()
        if self._health_check_date == today:
            return
        self._health_check_date = today
        try:
            from core.strategy_health import StrategyHealthMonitor
            raw = self._svc.get_daily_metas(limit=60)
            if not raw or len(raw) < 2:
                return
            # daily_meta 表有 trade_date/n_trades/equity,需补算 daily_return
            raw.sort(key=lambda r: r.get('trade_date', ''))
            stats = []
            for i, row in enumerate(raw):
                equity = float(row.get('equity', 0) or 0)
                prev_eq = float(raw[i - 1].get('equity', 0) or 0) if i > 0 else equity
                daily_ret = (equity - prev_eq) / prev_eq if prev_eq > 0 else 0.0
                stats.append({
                    'date': row.get('trade_date', today),
                    'daily_return': daily_ret,
                    'n_trades': int(row.get('n_trades', 0) or 0),
                    'equity': equity,
                })
            monitor = StrategyHealthMonitor(notify=True)
            report = monitor.check(stats)
            if report.has_warn():
                logger.warning('StrategyHealth: %s', report.worst_level())
                self._deliver_alert(report.to_feishu_text())
            else:
                logger.info('StrategyHealth: OK (sharpe_20d=%.3f)', report.rolling_sharpe_20d)
        except Exception as e:
            logger.warning('Daily health check failed: %s', e)

    def _check_and_push(self, now: datetime):
        """获取持仓 → 检查信号 → 推送飞书 + 自动下单(使用WFA优化参数)"""

        # ── 每日策略健康度检查(开盘时运行一次)────────────────────
        self._run_daily_health_check()

        # ── 同步市场环境(供 LLM 审核使用)────────────────────────
        self._sync_market_regime()

        # ── 驱动 StrategyRunner 刷新 pipeline scores ────────────
        if self._strategy_runner is not None:
            try:
                self._strategy_runner.run_once()
            except Exception as e:
                logger.warning('StrategyRunner.run_once() failed (will fallback): %s', e)

        # ── 大盘指数异动检查(每次轮询都检查,独立冷却)─────────
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

        # 刷新持仓价格并获取持仓
        try:
            self._svc.refresh_prices()
        except Exception as e:
            logger.warning('refresh_prices failed: %s', e)
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

        # ── 行业集中度检查 ─────────────────────────────────
        try:
            self._check_sector_concentration(positions)
        except Exception as e:
            logger.warning('Sector concentration check error: %s', e)

        # ── 持仓追加买入：Pipeline combined_score 驱动 ────────────
        #   旧 evaluate_signal 降级分支已删除（RSI 硬编码与 Pipeline 双信号并行有隐患）
        #   信号来源：FactorPipeline scores（经 WFA 优化，动态 IC 加权）
        #   触发阈值：combined_score > 0.30 视为买入积累信号
        from services.signals import SignalAlert, format_feishu_message, fetch_realtime
        from datetime import datetime as dt

        pipeline_scores: dict = {}
        if self._strategy_runner is not None:
            try:
                pipeline_scores = self._strategy_runner.last_scores
            except Exception:
                pass

        BUY_THRESHOLD = 0.30   # combined_score > 此值视为持仓加仓信号
        alerts = []
        for pos in positions:
            sym = pos.get('symbol')
            if not sym:
                continue
            score = pipeline_scores.get(sym, 0.0)
            if score <= BUY_THRESHOLD:
                continue

            # 获取实时行情补充 Alert 字段
            quote = fetch_realtime(sym)
            price = quote.get('close', 0) if quote else pos.get('current_price', 0)
            pct = quote.get('pct', 0) if quote else 0.0
            day_chg = quote.get('day_chg', 0) if quote else 0.0
            reason = f'Pipeline score={score:.4f} > {BUY_THRESHOLD}，持仓加仓信号'

            alert = SignalAlert(
                symbol=sym,
                signal='BUY',
                price=price,
                pct=pct,
                prev_rsi=None,
                volume_ratio=quote.get('volume_ratio') if quote else None,
                day_chg=day_chg,
                reason=reason,
                emitted_at=dt.now().strftime('%H:%M:%S'),
            )
            alerts.append(alert)
            logger.debug('Position add signal: %s score=%.4f', sym, score)

        # 过滤冷却期内标的
        actionable = [a for a in alerts if self._cooldown.can_fire(a.symbol)]

        # 推送飞书(有信号时)
        if actionable:
            check_time = now.strftime('%H:%M')
            msg = format_feishu_message(actionable, check_time)
            if msg:
                self._deliver_alert(msg)
                logger.info('Pushed %d alerts to Feishu at %s', len(actionable), check_time)

            # 自动下单(使用 per-symbol 参数)
            if self._broker:
                for alert in actionable:
                    self._submit_order_for_signal(alert)
        else:
            logger.debug('No buy/sell alerts at %s', now.strftime('%H:%M'))

        # ── 动态选股:主动建仓检查 ─────────────────────────
        if self._broker and self._daily_refresh:
            self._check_new_positions(now)

        # ── 统一退出引擎:止损 + 止盈 + 组合熔断(替代三个分散方法)──
        try:
            self._run_exit_engine(positions, now)
        except Exception as e:
            logger.error('ExitEngine error: %s', e, exc_info=True)

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

        # BUG-1 fix: 先检查 12% 熔断,再检查 8% 警告,避免 early return 屏蔽高优先级熔断
        # 12pct stop: full liquidation(优先执行,不受 8% 分支 return 影响)
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

        # 8pct warning: reduce to 50pct(在 12% 检查之后,避免被 early return 屏蔽)
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
                        half = max(100, (shares // 2 // 100) * 100)  # BUG-3 fix: 整手且不低于100
                        self._submit_market_sell(sym, half, reason="portfolio_risk_reduce")
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
                f'[WARNING] 行业集中度风险!\n'
                f'  行业: {v["sector"]}\n'
                f'  当前占比: {v["pct"]}% (上限 40%)\n'
                f'  需减仓: {v["reduce_value"]:.0f}元 ({v["reduce_pct"]}% 仓位)\n'
                f'  时间: {datetime.now().strftime("%H:%M")}\n'
                f'  ACTION: 减仓至 40%'
            )
            self._deliver_alert(msg)

            # 自动减仓(broker 模式下)
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


    def _run_exit_engine(self, positions: list, now: datetime):
        """
        统一卖出信号引擎集成层。
        替代分散的 _check_stop_losses() + _check_take_profits() + _check_portfolio_risk(),
        使用 ExitEngine 生成优先级排序的退出信号并统一执行。
        """
        try:
            from core.exit_engine import ExitEngine
        except ImportError as e:
            logger.warning('ExitEngine import failed, falling back to legacy checks: %s', e)
            self._check_portfolio_risk(positions)
            self._check_stop_losses(positions, now)
            self._check_take_profits(positions, now)
            return

        # ── 准备 price_bars(ATR/RSI 所需的 OHLCV 数据)─────────────────
        price_bars: dict = {}
        try:
            from core.data_layer import get_data_layer
            dl = get_data_layer()
            for pos in positions:
                sym = pos.get('symbol')
                if not sym:
                    continue
                try:
                    bars = dl.get_bars(sym, days=60)
                    if bars is not None and len(bars) >= 20:
                        price_bars[sym] = bars
                except Exception:
                    pass
        except Exception as e:
            logger.debug('price_bars fetch failed: %s', e)

        # ── 准备 per-symbol 参数(WFA 优化参数优先)────────────────────
        params_map = {
            pos['symbol']: self._get_params(pos['symbol'])
            for pos in positions if pos.get('symbol')
        }

        # ── 补全 current_price(ExitEngine 需要)───────────────────────
        enriched: list = []
        from services.signals import fetch_realtime
        for pos in positions:
            p = dict(pos)
            if not p.get('current_price') or p['current_price'] <= 0:
                try:
                    snap = fetch_realtime(p.get('symbol', ''))
                    if snap and snap.get('price', 0) > 0:
                        p['current_price'] = snap['price']
                        # 持久化到 DB(确保 latest_price 字段被更新)
                        self._svc.update_position_price(p['symbol'], snap['price'])
                        # 同步更新 peak_price
                        if p['current_price'] > float(p.get('peak_price', 0) or 0):
                            p['peak_price'] = p['current_price']
                except Exception:
                    pass
            enriched.append(p)

        # ── 获取当前总权益并更新历史峰值 ──────────────────────────────
        current_equity = 0.0
        try:
            summary = self._svc.get_portfolio_summary(refresh_prices_now=False)
            current_equity = float(summary.get('total_equity', 0) or 0)
        except Exception:
            pass
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity
            # 新高时重置熔断标志
            self._risk_warn_fired = False
            self._risk_stop_fired = False

        # ── 从 StrategyRunner 获取因子评分(可选,失败不影响运行)──────
        pipeline_scores: dict = {}
        if self._strategy_runner is not None:
            try:
                for rr in self._strategy_runner.last_results:
                    if rr.pipeline_result is not None:
                        pipeline_scores[rr.symbol] = rr.pipeline_result.combined_score
            except Exception:
                pass  # pipeline_scores 为空时 ExitEngine 跳过 FACTOR_REVERSAL 检查

        # ── 调用 ExitEngine 生成信号 ───────────────────────────────────
        engine = ExitEngine(
            dd_warn=self._dd_warn,
            dd_stop=self._dd_stop,
        )
        signals = engine.generate(
            positions=enriched,
            equity_peak=self._peak_equity,
            current_equity=current_equity,
            pipeline_scores=pipeline_scores or None,
            price_bars=price_bars or None,
            params_map=params_map or None,
        )

        if not signals:
            return

        logger.info('ExitEngine: %d signals generated at %s', len(signals), now.strftime('%H:%M'))

        _EMOJI = {0: '🚨', 1: '⚠️', 2: '🛑', 3: '📉',
                  4: '↩️', 5: '⚡', 6: '🎯', 7: '🏆', 8: '📊', 9: '⏰'}

        for sig in signals:
            sym = sig.symbol
            is_portfolio_level = sig.priority.value <= 1  # P0=EMERGENCY, P1=PORTFOLIO_REDUCE

            # 组合级别信号:使用 _risk_*_fired 防重入(而非 cooldown)
            if is_portfolio_level:
                if sig.priority.value == 0 and self._risk_stop_fired:
                    continue
                if sig.priority.value == 1 and self._risk_warn_fired:
                    continue
                if sig.priority.value == 0:
                    self._risk_stop_fired = True
                if sig.priority.value == 1:
                    self._risk_warn_fired = True
            else:
                # 个股级别:冷却检查(紧急信号 P2 降低冷却要求)
                cooldown_key = f'exit_{sym}'
                if not self._cooldown.can_fire(cooldown_key):
                    continue

            # 计算实际卖出股数
            pos_dict = next((p for p in enriched if p.get('symbol') == sym), None)
            if not pos_dict:
                continue
            shares = pos_dict.get('shares', 0)
            if shares <= 0:
                continue
            sell_shares = sig.shares_to_sell(shares)
            current_price = sig.current_price or float(pos_dict.get('current_price', 0) or 0)
            if current_price <= 0:
                continue

            emoji = _EMOJI.get(sig.priority.value, '📤')
            label = sig.priority.name.replace('_', ' ').title()
            pnl_str = f'{sig.unrealized_pct * 100:+.1f}%'

            if not self._can_trade():
                self._deliver_alert(
                    f'📋 [{sym}] 模拟模式:退出信号跳过执行\n'
                    f'   {label} | 卖出: {sell_shares}股 ({sig.exit_pct*100:.0f}%仓) | 价: {current_price:.2f}\n'
                    f'   浮盈: {pnl_str} | 原因: {sig.reason}\n'
                    f'   (切换"实盘"模式后生效)'
                )
                logger.info('Simulation: skipped ExitEngine %s %s %d @ %.2f',
                            sig.priority.name, sym, sell_shares, current_price)
                continue

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
                    f'{emoji}[{sym}] {label}(ExitEngine 自动平仓)\n'
                    f'   {status_str} {sell_shares}股 @ {result.avg_price:.2f} | 浮盈: {pnl_str}\n'
                    f'   原因: {sig.reason}'
                )
                logger.info('ExitEngine SELL %s %s %d @ %.2f => %s',
                            sig.priority.name, sym, sell_shares, result.avg_price, result.status)
            except Exception as e:
                logger.error('ExitEngine order failed %s %s: %s', sig.priority.name, sym, e)

    def _check_take_profits(self, positions, now: datetime):
        """
        对持仓检查止盈条件(优先用 params.json 配置):
        1. ATR 移动止盈(Chandelier Exit):峰值回撤超过 2×ATR 时触发
        2. 固定止盈:涨幅达到 take_profit_pct 时触发
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

            # 同时更新持仓峰值(内存层面)
            if current_price > peak_price:
                peak_price = current_price

            # 止盈冷却 key
            tp_key = f'tp_{sym}'

            # 1. ATR 移动止盈(优先,让利润奔跑)
            atr_triggered, atr_stop, atr_reason = check_atr_trailing_stop(
                sym, peak_price, entry_price, current_price,
                atr_period=int(params.get('atr_period', 14)),
                atr_multiplier=atr_multiplier)
            logger.debug('TakeProfit ATR %s @ %.2f: %s', sym, current_price, atr_reason)

            # 2. 固定止盈
            fixed_triggered, fixed_target, fixed_reason = check_fixed_take_profit(
                entry_price, current_price, tp_pct=tp_pct)
            logger.debug('TakeProfit fixed %s @ %.2f: %s', sym, current_price, fixed_reason)

            # 哪个先触发用哪个(取更早的信号)
            triggered = atr_triggered or fixed_triggered
            if not triggered:
                continue

            # 优先报告 ATR 移动止盈(更智能)
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
                if not self._can_trade():
                    self._deliver_alert(
                        f'📋 [{sym}] 模拟模式:止盈跳过执行\n'
                        f'   止盈:{label} | 卖出:{sell_shares}股 | 价:{current_price:.2f}\n'
                        f'   原因: {reason}(切换"实盘"后生效)'
                    )
                    logger.info('Simulation: skipped TakeProfit SELL %s %d', sym, sell_shares)
                    continue
                result = self._broker.submit_order(
                    symbol=sym, direction='SELL',
                    shares=sell_shares, price=current_price, price_type='market',
                )
                status_str = '✅ 成交' if result.status == 'filled' else f'❌ {result.status}'
                self._deliver_alert(
                    f'🎯[{sym}] {label}触发(自动止盈)\n'
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

            # 检查止损(per-symbol params,WFA 优先)
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

            # 冷却检查(止损触发后 15 分钟内不重复)
            sl_key = f'sl_{sym}'
            if not self._cooldown.can_fire(sl_key):
                continue

            # 执行止损卖出(全部清仓)
            sell_shares = (shares // 100) * 100
            try:
                if not self._can_trade():
                    self._deliver_alert(
                        f'📋 [{sym}] 模拟模式:止损跳过执行\n'
                        f'   止损触发 | 卖出:{sell_shares}股 | 价:{current_price:.2f}\n'
                        f'   止损价:{stop_price:.2f} | 原因: {reason}(切换"实盘"后生效)'
                    )
                    logger.info('Simulation: skipped StopLoss SELL %s %d', sym, sell_shares)
                    continue
                result = self._broker.submit_order(
                    symbol=sym,
                    direction='SELL',
                    shares=sell_shares,
                    price=current_price,
                    price_type='market',
                )
                status_str = '✅ 成交' if result.status == 'filled' else f'❌ {result.status}'
                self._deliver_alert(
                    f'🛑[{sym}] ATR止损触发(自动平仓)\n'
                    f'   {status_str} {sell_shares}股 @ {result.avg_price:.2f}\n'
                    f'   止损价:{stop_price:.2f} | 当前价:{current_price:.2f}\n'
                    f'   原因: {reason}'
                )
                logger.info('StopLoss SELL %s %d @ %.2f => %s',
                           sym, sell_shares, result.avg_price, result.status)
            except Exception as e:
                logger.error('StopLoss order failed for %s: %s', sym, e)

    # ── 大盘指数监控 ───────────────────────────────────────────────
    # 监控的指数及其预警阈值(涨跌幅绝对值超过此值则告警)
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
        检查大盘指数是否出现显著异动(涨跌超过阈值)。
        发现异动 → 推送飞书 + 记录 alert_history。
        冷却:每只指数 30 分钟内不重复告警。
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
        检查自选股列表中的股票是否出现异动(涨跌幅超过各股阈值)。
        阈值默认 5%,可在 watchlist 表中逐股配置。
        只做预警推送,不自动交易。
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
        """加载今日板块资金流向数据(从 dynamic_selector)"""
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
        资金流入评分从上一轮监控到这一轮出现显著提升(flow ↑>20分)→ 预警。
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

            # 检测突变:资金流入评分跃升 > 20 分
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
                    f'   可能受消息面驱动,关注持续性\n'
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
        """通过飞书 IM API 推送文本消息给用户,并记录到历史。"""
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
