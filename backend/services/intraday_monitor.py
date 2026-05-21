"""
intraday_monitor.py — 盘中实时监控服务（编排器）
=================================================

后台线程,交易时段持续运行:
  - 每 5 分钟检查一次持仓信号
  - 合条件时主动推送 Feishu 消息

P2-7 重构后:本文件仅保留 IntradayMonitor 类构造 + 主循环 + Mixin 组合。
具体职责分散到 backend/services/intraday/ 5 个子模块:
  - data.py        — 行情/选股/参数数据拉取
  - signaling.py   — 信号生成（调 use_case + 主循环 _check_and_push）
  - risk.py        — 仓位裁剪、ExitEngine、组合熔断
  - execution.py   — 智能路由、信号→订单转换、交易模式
  - alerts.py      — 飞书推送、LLM 终极审核、可观测性日志

使用方法不变:
  from backend.services.intraday_monitor import IntradayMonitor
  mon = IntradayMonitor(svc=portfolio_service)
  mon.start()
  mon.stop()
"""

import os
import sys
import logging
import threading
from datetime import datetime
from typing import Optional

# Resolve imports relative to backend dir(向后兼容旧代码用 services.* 形式导入）
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = THIS_DIR
sys.path.insert(0, BACKEND_DIR)

from .intraday import (
    is_market_open,
    next_market_seconds,
    CooldownTracker,
    DataMixin,
    SignalingMixin,
    RiskMixin,
    ExecutionMixin,
    AlertsMixin,
)
# 部分常量保留在本模块顶层以兼容 `from intraday_monitor import X` 形式
from .intraday.signaling import BUY_THRESHOLD_NEW, BUY_THRESHOLD_ADD  # noqa: F401
from .intraday.risk import MAX_POSITION_PCT  # noqa: F401
from .intraday.cooldown import COOLDOWN  # noqa: F401

logger = logging.getLogger('intraday_monitor')

# 全局配置
CHECK_INTERVAL = 300  # 秒(5分钟)


class IntradayMonitor(DataMixin, SignalingMixin, RiskMixin, ExecutionMixin, AlertsMixin):
    """
    盘中信号监控后台线程。
    检测到信号时:
      1. 推送飞书提醒
      2. 自动提交订单(如果 broker 已注入)

    职责通过 Mixin 组合（详见模块 docstring）。
    """

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
        self._svc = svc
        self._broker = broker
        self._interval = check_interval
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cooldown = CooldownTracker()
        self._running = False
        # 保护所有可观测性 / 缓存字段——
        # monitor 后台线程写,API 线程在 get_status() / set_strategy_runner() 读。
        # RLock 让同线程嵌套调用(如 _record_signal 嵌入 _deliver_alert)不死锁。
        self._state_lock = threading.RLock()
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
        # 新闻情绪缓存:{symbol: (sentiment, confidence, summary)}  每天刷新
        self._sentiment_cache: dict = {}
        self._sentiment_cache_date: str = ''
        # 组合熔断追踪
        self._peak_equity: float = 0.0
        self._risk_warn_fired: bool = False
        self._risk_stop_fired: bool = False
        # 市场环境缓存(LLM prompt 使用)
        self._market_regime: dict = {}
        # 组合风控参数
        self._dd_warn: float = 0.08    # 8% 回撤警告
        self._dd_stop: float = 0.12    # 12% 回撤清仓
        # Kelly 仓位
        self._kelly_pct: float = 0.10   # 默认 10%,每交易日根据历史交易更新
        self._kelly_last_updated: str = ''
        # 交易模式
        self._trading_mode: str = 'simulation'
        self._load_trading_mode()
        # 策略健康监控(每日开盘时检查一次)
        self._health_check_date: str = ''
        # 可观测性状态
        self._last_scan_symbol: str = ''
        self._last_scan_time: str = ''
        self._signal_log: list = []
        self._skip_log: list = []
        self._llm_review_log: list = []
        self._scan_count: int = 0
        self._error_count: int = 0
        self._last_error: str = ''
        # 板块资金流上一轮快照(DataMixin._check_sector_flow 用)
        self._prev_sector_flows: dict = {}

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

    def set_strategy_runner(self, runner) -> None:
        """注入 StrategyRunner 实例,用于读取 pipeline_scores。

        可在 monitor.start() 之前或之后调用,通过 _state_lock 保证可见性。
        """
        with self._state_lock:
            self._strategy_runner = runner
        logger.info('StrategyRunner injected into IntradayMonitor')

    # ── 主循环 ────────────────────────────────────────────

    def _run(self):
        logger.info('Monitor thread active, checking market hours...')
        while not self._stop_evt.is_set():
            now = datetime.now()

            if not is_market_open(now):
                # 非交易时段:sleep 到下次开盘,分段 sleep 方便快速响应 stop
                wait = next_market_seconds(now)
                logger.info('Market closed. Sleeping %ds until next open', wait)
                for _ in range(min(wait, 3600)):
                    if self._stop_evt.wait(timeout=1):
                        return
                continue

            # 交易时段:刷新持仓价格（腾讯接口，自动保护 DB 无效数据）
            try:
                refreshed = self._svc.refresh_prices()
                if refreshed:
                    logger.info('Prices refreshed: %d symbols updated', len(refreshed))
            except Exception as e:
                logger.warning('Price refresh failed: %s', e)

            # 检查信号
            try:
                self._check_and_push(now)
            except Exception as e:
                with self._state_lock:
                    self._error_count += 1
                    self._last_error = f'{now.strftime("%H:%M:%S")}: {e}'
                logger.error('Signal check error: %s', e)

            # 清理过期冷却记录
            self._cooldown.purge_old()

            # 等待下次检查
            self._stop_evt.wait(timeout=self._interval)
