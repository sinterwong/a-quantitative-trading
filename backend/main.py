"""
main.py — Backend service entry point
=====================================
Starts the HTTP API server as a persistent background process.
Runs the unified Scheduler for full-day automation:
  08:30  morning_runner    — 选股→watchlist→RSI信号→下单→早报飞书
  09:31  IntradayMonitor   — 盘中信号扫描（每5分钟 RSI 金叉/死叉）
  15:00  afternoon_report  — 收盘晚报→飞书推送
  15:10  /analysis/run     — 日终 DynamicStockSelectorV2 选股分析
  16:00  DailyOpsReporter  — 每日运营报告推送

Usage:
    python main.py                    # API server only
    python main.py --mode scheduler   # scheduler only
    python main.py --mode both        # API + scheduler + intraday monitor
"""

import os
import sys
import argparse
import logging
import threading
import time
import signal
from datetime import datetime

# Load .env before accessing environment variables
_dotenv_path = os.path.join(os.path.dirname(__file__), '..', '.env')
if os.path.exists(_dotenv_path):
    try:
        with open(_dotenv_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())
    except Exception:
        pass

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.dirname(THIS_DIR)
BACKEND_DIR = THIS_DIR

LOG_FILE = os.path.join(THIS_DIR, 'backend.log')



def setup_logging():
    from logging.handlers import RotatingFileHandler
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s — %(message)s')
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # 文件 handler：100 MB 轮转，保留 5 个备份
    fh = RotatingFileHandler(
        LOG_FILE, encoding='utf-8',
        maxBytes=100 * 1024 * 1024,  # 100 MB
        backupCount=5,
    )
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    root.addHandler(fh)
    root.addHandler(sh)
    return logging.getLogger('backend')


# Module-level monitor reference (set in main())
_monitor = None
_broker = None   # shared PaperBroker instance (same one monitor uses)

def get_monitor():
    """Return the IntradayMonitor instance, or None if not started."""
    return _monitor

def get_broker():
    """Return the shared broker instance, or None if not started."""
    return _broker


# ============================================================
# Scheduler
# ============================================================

def _build_trade_calendar() -> set:
    """从 AKShare 获取 A 股交易日历，返回 'YYYY-MM-DD' 字符串集合。失败时返回空集合。"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        # 返回 DataFrame，列名为 'trade_date'，类型为 datetime.date 或 str
        dates = df.iloc[:, 0]
        return {str(d)[:10] for d in dates}
    except Exception:
        return set()


# 模块级缓存：交易日集合 + 加载日期
_trade_calendar: set = set()
_trade_calendar_date: str = ''


def is_trading_day() -> bool:
    """Check if today is an A-share trading day using AKShare calendar.

    Falls back to weekday check (Mon-Fri) when AKShare is unavailable.
    Calendar is cached for the entire calendar day to avoid repeated API calls.
    """
    from datetime import datetime
    global _trade_calendar, _trade_calendar_date

    today_str = datetime.now().strftime('%Y-%m-%d')

    # 每天首次调用时刷新日历
    if _trade_calendar_date != today_str:
        cal = _build_trade_calendar()
        if cal:
            _trade_calendar = cal
            _trade_calendar_date = today_str

    if _trade_calendar:
        return today_str in _trade_calendar

    # 降级：简单周一~周五判断
    return datetime.now().weekday() < 5


def wait_until_next(target_hour=15, target_min=10):
    """Return seconds until target time (CST = UTC+8)."""
    from datetime import datetime, timedelta
    now = datetime.now()
    target = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 0)


class Scheduler:
    """
    统一调度器 — 交易日自动化核心引擎。
    每日定时任务（北京时间）：
      08:30  — 早盘自动化（选股→watchlist→RSI信号→下单→早报飞书）
      09:31  — 盘中信号监控开启（IntradayMonitor，每5分钟扫 RSI 金叉/死叉）
      15:00  — 收盘晚报（持仓快照→日收益→飞书推送）
      15:10  — 日终选股分析（DynamicStockSelectorV2 → 写入 analysis_*.json）
      16:00  — 每日运营报告（告警推送）

    非交易日（周末/节假日）全部跳过。
    """

    # 每日定时任务表：key = (hour, minute), value = 方法名
    DAILY_TASKS = [
        (8,  30, '_trigger_morning_runner'),   # 早盘自动化
        (9,  31, '_trigger_intraday_monitor'), # 盘中信号监控（仅交易时段）
        (15,  0, '_trigger_afternoon_report'),  # 收盘晚报
        (15, 10, '_trigger_analysis'),          # 日终选股分析
        (16,  0, '_trigger_daily_ops_report'),  # 每日运营报告
    ]

    def __init__(self, api_port: int = 5555):
        self.api_port = api_port
        self.logger = logging.getLogger('backend.scheduler')
        self._stop = threading.Event()

    # ── 任务触发方法 ────────────────────────────────────────────────

    def _trigger_morning_runner(self):
        """08:30 — 调用 morning_runner.run() 完整早盘流程。"""
        self.logger.info('[Scheduler] 08:30 — triggering morning_runner')
        try:
            import importlib.util, sys as _sys, os as _os
            scripts_dir = os.path.join(PROJ_DIR, 'scripts')
            _sys.path.insert(0, scripts_dir)
            # 直接加载脚本模块并调用 run()
            spec = importlib.util.spec_from_file_location(
                'morning_runner', os.path.join(scripts_dir, 'morning_runner.py'))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.run()
            self.logger.info('[Scheduler] morning_runner completed')
        except Exception as e:
            self.logger.error('[Scheduler] morning_runner failed: %s', e)

    def _trigger_intraday_monitor(self):
        """09:31 — 通知 IntradayMonitor 启动盘中信号扫描（每5分钟一次）。"""
        self.logger.info('[Scheduler] 09:31 — triggering intraday monitor')
        # 注意：不能在 Scheduler 实例方法里导入 backend.main（循环引用），
        # 所以直接引用 backend.main 模块级别定义的 _monitor 变量
        import sys as _sys
        _sys.path.insert(0, os.path.join(PROJ_DIR, 'backend'))
        try:
            import backend.main as _bm
            monitor = getattr(_bm, '_monitor', None)
            if monitor is not None:
                monitor.start()
                self.logger.info('[Scheduler] IntradayMonitor started')
            else:
                self.logger.warning('[Scheduler] IntradayMonitor not yet initialized')
        except Exception as e:
            self.logger.error('[Scheduler] IntradayMonitor start failed: %s', e)

    def _trigger_afternoon_report(self):
        """15:00 — 调用 afternoon_report.run() 收盘晚报。"""
        self.logger.info('[Scheduler] 15:00 — triggering afternoon_report')
        try:
            import importlib.util, sys as _sys
            scripts_dir = os.path.join(PROJ_DIR, 'scripts')
            _sys.path.insert(0, scripts_dir)
            spec = importlib.util.spec_from_file_location(
                'afternoon_report', os.path.join(scripts_dir, 'afternoon_report.py'))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.run()
            self.logger.info('[Scheduler] afternoon_report completed')
        except Exception as e:
            self.logger.error('[Scheduler] afternoon_report failed: %s', e)

    def _trigger_analysis(self):
        """15:10 — HTTP POST 到 /analysis/run（已有逻辑）。"""
        self.logger.info('[Scheduler] 15:10 — triggering /analysis/run')
        import urllib.request
        url = f'http://127.0.0.1:{self.api_port}/analysis/run'
        try:
            req = urllib.request.Request(url, method='POST')
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read()
            self.logger.info('[Scheduler] analysis triggered: %s',
                             body.decode('utf-8', errors='replace')[:200])
        except Exception as e:
            self.logger.error('[Scheduler] /analysis/run failed: %s', e)

        # 每周一额外触发行业轮动
        from datetime import datetime as _dt
        if _dt.now().weekday() == 0:
            self._trigger_sector_rotation()
            # 季报刷新：季度末月（3/6/9/12）25日起，或财报季首周（1/4/7/10月1-7日）
            is_quarter_end = _dt.now().month in (3, 6, 9, 12) and _dt.now().day >= 25
            is_earnings_season = _dt.now().month in (1, 4, 7, 10) and 1 <= _dt.now().day <= 7
            if is_quarter_end or is_earnings_season:
                label = 'quarter-end' if is_quarter_end else 'earnings-season'
                self.logger.info('[Scheduler] %s — refreshing fundamental data', label)
                self._refresh_fundamentals()

    def _trigger_sector_rotation(self):
        """每周一 15:10 后 — HTTP POST 到 /analysis/sector_rotation。"""
        self.logger.info('[Scheduler] Monday — triggering sector rotation')
        import urllib.request, json as _json
        url = f'http://127.0.0.1:{self.api_port}/analysis/sector_rotation'
        try:
            payload = _json.dumps({}).encode()
            req = urllib.request.Request(url, data=payload, method='POST',
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
            data = _json.loads(body)
            buy  = data.get('data', {}).get('buy', [])
            sell = data.get('data', {}).get('sell', [])
            self.logger.info('[Scheduler] 行业轮动信号 — 买入: %s  卖出: %s', buy, sell)
        except Exception as e:
            self.logger.error('[Scheduler] sector rotation failed: %s', e)

    def _refresh_fundamentals(self):
        """季报季度末 / 财报季强制刷新持仓标的基本面数据缓存。"""
        import sys as _sys, urllib.request as _req, json as _j
        try:
            url = f'http://127.0.0.1:{self.api_port}/positions'
            with _req.urlopen(url, timeout=5) as r:
                d = _j.loads(r.read())
            symbols = [p['symbol'] for p in d.get('positions', []) if p.get('shares', 0) > 0]
        except Exception:
            symbols = []

        if not symbols:
            self.logger.info('[Scheduler] Fundamental refresh: no positions, skipping')
            return

        _sys.path.insert(0, PROJ_DIR)
        try:
            from core.fundamental_data import FundamentalDataManager
            mgr = FundamentalDataManager()
            ok, fail = 0, 0
            for sym in symbols:
                try:
                    mgr.invalidate(sym)
                    df = mgr.get_fundamentals(sym)
                    ok += 1 if not df.empty else 0
                    fail += 1 if df.empty else 0
                except Exception as e:
                    fail += 1
                    self.logger.warning('Fundamental refresh error %s: %s', sym, e)
            self.logger.info('[Scheduler] Fundamental refresh done — ok=%d fail=%d', ok, fail)
        except ImportError as e:
            self.logger.error('[Scheduler] FundamentalDataManager import failed: %s', e)

    def _trigger_daily_ops_report(self):
        """16:00 — DailyOpsReporter 运营报告。"""
        self.logger.info('[Scheduler] 16:00 — triggering daily ops report')
        try:
            sys.path.insert(0, PROJ_DIR)
            from core.daily_ops_reporter import DailyOpsReporter
            reporter = DailyOpsReporter(api_port=self.api_port)
            report = reporter.run()
            n_trades = report.get('trades', {}).get('n_trades', 0)
            pnl = report.get('portfolio', {}).get('total_unrealized_pnl', 0.0)
            self.logger.info('[Scheduler] ops report done — trades=%d unrealized_pnl=%.2f', n_trades, pnl)
        except Exception as e:
            self.logger.error('[Scheduler] daily ops report failed: %s', e)

    # ── 核心循环 ────────────────────────────────────────────────────────

    def _run_loop(self):
        self.logger.info('[Scheduler] started — tasks: %s',
                         [(f'{h:02d}:{m:02d}', fn) for h, m, fn in self.DAILY_TASKS])

        while not self._stop.is_set():
            now = datetime.now()

            # ── 检查每个定时任务 ──
            for target_hour, target_min, method_name in self.DAILY_TASKS:
                target = now.replace(hour=target_hour, minute=target_min,
                                     second=0, microsecond=0)
                # 触发窗口：目标时间 ± 60 秒（防止时钟漂移丢任务）
                if abs((now - target).total_seconds()) < 60:
                    if not is_trading_day():
                        self.logger.info('[Scheduler] %02d:%02d — 非交易日，跳过 %s',
                                         target_hour, target_min, method_name)
                        continue

                    self.logger.info('[Scheduler] >>> %02d:%02d 触发 %s',
                                     target_hour, target_min, method_name)
                    handler = getattr(self, method_name, None)
                    if handler:
                        t = threading.Thread(target=handler, name=f'Scheduler-{method_name}')
                        t.start()
                        # 等待该任务完成（避免多个任务同时跑抢占资源）
                        t.join()
                    else:
                        self.logger.error('[Scheduler] 方法不存在: %s', method_name)

                    break  # 一次循环只触发一个任务，避免重复
            else:
                # 没有任务触发：休息 30 秒再检查
                time.sleep(30)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run_loop, daemon=True, name='Scheduler')
        t.start()
        return t

    def stop(self):
        self.logger.info('[Scheduler] stopping')
        self._stop.set()


# ============================================================
# API server
# ============================================================

def start_api_server(host: str, port: int, logger):
    """Start Flask app in the current process (blocking)."""
    sys.path.insert(0, THIS_DIR)
    sys.path.insert(0, PROJ_DIR)
    os.environ['FLASK_ENV'] = 'production'

    from werkzeug.serving import make_server
    import importlib.util

    spec = importlib.util.spec_from_file_location('api', os.path.join(THIS_DIR, 'api.py'))
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)

    server = make_server(host, port, api.app, threaded=True, passthrough_errors=False)
    logger.info('API running on http://%s:%s', host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info('API shutting down...')
        server.shutdown()
        server.server_close()


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Portfolio Backend Service')
    parser.add_argument('--host', default='0.0.0.0', help='绑定地址，0.0.0.0=接受局域网访问，127.0.0.1=仅本机（默认 0.0.0.0）')
    parser.add_argument('--port', type=int, default=5555)
    parser.add_argument('--mode', choices=['api', 'scheduler', 'both'], default='both')
    args = parser.parse_args()

    logger = setup_logging()
    logger.info('Backend starting in %s mode', args.mode)

    sched = Scheduler(api_port=args.port)
    sched_thread = sched.start()

    def on_shutdown():
        logger.info('Shutdown signal received...')
        sched.stop()

    signal.signal(signal.SIGINT, lambda *_: on_shutdown())
    signal.signal(signal.SIGTERM, lambda *_: on_shutdown())

    # Intraday monitor — runs during trading hours, pushes Feishu + auto-orders
    global _monitor
    monitor = None
    if args.mode in ('scheduler', 'both'):
        sys.path.insert(0, BACKEND_DIR)
        try:
            from services.intraday_monitor import IntradayMonitor
            from services.portfolio import PortfolioService
            from services.broker import PaperBroker

            # Get or create PortfolioService
            svc = None
            try:
                from api import _svc
                svc = _svc
            except ImportError:
                pass
            if svc is None:
                svc = PortfolioService()

            # Initialize PaperBroker with PortfolioService
            global _broker
            broker = PaperBroker(portfolio_service=svc)
            broker.connect()
            _broker = broker

            # Load max_position_pct from params.json if available
            import json as _json
            max_pos_pct = 0.20
            params_path = os.path.join(BACKEND_DIR, '..', 'params.json')
            if os.path.exists(params_path):
                try:
                    with open(params_path, 'r', encoding='utf-8') as f:
                        params = _json.load(f)
                    max_pos_pct = params.get('risk', {}).get('max_position_pct', 0.20)
                except Exception:
                    pass

            # Initialize LLM service for news sentiment analysis
            llm_service = None
            try:
                from services.llm.factory import create_llm_service
                llm_service = create_llm_service()
                logger.info('LLM service initialized for news sentiment')
            except Exception as e:
                logger.warning('LLM service init failed (news sentiment disabled): %s', e)

            monitor = IntradayMonitor(
                svc=svc, broker=broker,
                check_interval=300,
                max_position_pct=max_pos_pct,
                llm_service=llm_service,
            )
            _monitor = monitor
            # 注意：延迟 monitor.start()，待 StrategyRunner 注入后再启动
            logger.info('IntradayMonitor created (broker=PaperBroker, max_pos_pct=%.0f%%, llm=%s)',
                        max_pos_pct * 100, llm_service is not None)
        except Exception as e:
            logger.warning('IntradayMonitor init failed (non-fatal): %s', e)

    # API server 必须在 StrategyRunner 之前启动（runner 的 _runner_symbols 依赖 API）
    if args.mode in ('api', 'both'):
        logger.info('Starting API server thread')
        api_t = threading.Thread(
            target=start_api_server,
            args=(args.host, args.port, logger),
            daemon=True,
            name='APIServer',
        )
        api_t.start()
        logger.info('API: http://%s:%s', args.host, args.port)

    # StrategyRunner — 使用 DynamicWeightPipeline 全因子流水线
    # dry_run=True：仅记录/告警信号，执行权仍在 IntradayMonitor
    if args.mode in ('scheduler', 'both'):
        try:
            from core.pipeline_factory import build_runner

            def _runner_symbols():
                """动态获取候选标的：持仓标的 + 宽基 ETF 兜底。"""
                try:
                    import urllib.request as _req, json as _j
                    url = f'http://127.0.0.1:{args.port}/positions'
                    with _req.urlopen(url, timeout=3) as r:
                        d = _j.loads(r.read())
                    held = [p['symbol'] for p in d.get('positions', [])
                            if p.get('shares', 0) > 0]
                    if held:
                        return held
                except Exception:
                    pass
                return ['510300.SH', '159915.SZ', '512690.SH']

            runner = build_runner(
                symbols=_runner_symbols,
                dry_run=True,    # 信号仅记录日志，不重复下单
                interval=300,
                signal_threshold=0.5,
            )
            # 注入到 IntradayMonitor（在 start() 之前，避免竞态读取 None）
            if monitor is not None:
                monitor.set_strategy_runner(runner)
            runner_t = threading.Thread(
                target=runner.run_loop, daemon=True, name='StrategyRunner')
            runner_t.start()
            logger.info('StrategyRunner started (DynamicWeightPipeline, dry_run=True)')
        except Exception as exc:
            logger.warning('StrategyRunner start failed (non-fatal): %s', exc)

    # StrategyRunner 已注入 → 安全启动 IntradayMonitor
    if monitor is not None:
        try:
            monitor.start()
            logger.info('IntradayMonitor started')
        except Exception as e:
            logger.warning('IntradayMonitor start failed (non-fatal): %s', e)

    logger.info('Backend running. Press Ctrl+C to stop.')

    try:
        while sched_thread.is_alive():
            sched_thread.join(timeout=5)
    except KeyboardInterrupt:
        on_shutdown()
        if monitor:
            monitor.stop()


if __name__ == '__main__':
    main()
