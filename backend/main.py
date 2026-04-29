"""
main.py — Backend service entry point
=====================================
Starts the HTTP API server as a persistent background process.
Runs the Scheduler for automated daily analysis (15:10 CST).
Runs the IntradayMonitor for intraday signal detection (every 5 min during trading hours).

Usage:
    python main.py                    # start API server
    python main.py --mode scheduler   # start scheduler only
    python main.py --mode both       # API + scheduler + intraday monitor
"""

import os
import sys
import argparse
import logging
import threading
import time
import signal

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

# StrategyRunner 实例（由 main() 启动后赋值，供 IntradayMonitor 读取 pipeline_scores）
_strategy_runner = None


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
    Background thread that triggers /analysis/run at 15:10 CST daily.
    """

    def __init__(self, api_port: int = 5555):
        self.api_port = api_port
        self.logger = logging.getLogger('backend.scheduler')
        self._stop = threading.Event()

    def _trigger_analysis(self):
        """HTTP POST to /analysis/run on the local API."""
        import urllib.request
        url = f'http://127.0.0.1:{self.api_port}/analysis/run'
        try:
            req = urllib.request.Request(url, method='POST')
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read()
            self.logger.info('Analysis triggered: %s', body.decode('utf-8', errors='replace')[:200])
        except Exception as e:
            self.logger.error('Analysis trigger failed: %s', e)

    def _trigger_sector_rotation(self):
        """HTTP POST to /analysis/sector_rotation — 每周一收盘后触发行业轮动换仓信号。"""
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
            self.logger.info('行业轮动信号 — 买入: %s  卖出: %s', buy, sell)
        except Exception as e:
            self.logger.error('Sector rotation trigger failed: %s', e)

    def _refresh_fundamentals(self):
        """季报季度末 / 财报季强制刷新持仓标的基本面数据缓存。

        触发时机（由 _run_loop 判断）：
          • 每季度末月（3/6/9/12）25 日起 — 季报发布前预热
          • 每财报季首周（1/4/7/10 月 1-7 日） — 新季报落地后强制更新
        """
        import sys as _sys, urllib.request as _req, json as _j
        try:
            # 1. 获取当前持仓标的
            url = f'http://127.0.0.1:{self.api_port}/positions'
            with _req.urlopen(url, timeout=5) as r:
                d = _j.loads(r.read())
            symbols = [p['symbol'] for p in d.get('positions', []) if p.get('shares', 0) > 0]
        except Exception:
            symbols = []

        if not symbols:
            self.logger.info('Fundamental refresh: no held positions, skipping')
            return

        # 2. 使用 FundamentalDataManager 强制失效并重新拉取
        _sys.path.insert(0, PROJ_DIR)
        try:
            from core.fundamental_data import FundamentalDataManager
            mgr = FundamentalDataManager()
            ok, fail = 0, 0
            for sym in symbols:
                try:
                    mgr.invalidate(sym)
                    df = mgr.get_fundamentals(sym)
                    if not df.empty:
                        ok += 1
                        self.logger.debug('Fundamental refreshed: %s (%d rows)', sym, len(df))
                    else:
                        fail += 1
                        self.logger.warning('Fundamental refresh empty: %s', sym)
                except Exception as e:
                    fail += 1
                    self.logger.warning('Fundamental refresh error %s: %s', sym, e)
            self.logger.info('Fundamental refresh done — ok=%d fail=%d symbols=%s', ok, fail, symbols)
        except ImportError as e:
            self.logger.error('Fundamental refresh import failed: %s', e)

    def _run_loop(self):
        self.logger.info('Scheduler started')
        while not self._stop.is_set():
            seconds = wait_until_next(15, 10)
            self.logger.info('Next run in %.0f seconds (%s)', seconds,
                            'skipping non-trading day' if not is_trading_day() else 'will trigger')
            # Wait in 60-second chunks so stop signal is responsive
            waited = 0
            while waited < seconds and not self._stop.is_set():
                chunk = min(60, seconds - waited)
                time.sleep(chunk)
                waited += chunk
            if self._stop.is_set():
                break
            if is_trading_day():
                self.logger.info('Trading day — triggering analysis')
                self._trigger_analysis()
                from datetime import datetime as _dt
                today = _dt.now()
                # 每周一额外触发行业轮动（weekday() == 0）
                if today.weekday() == 0:
                    self.logger.info('Monday — triggering sector rotation')
                    self._trigger_sector_rotation()
                # 季报刷新：季度末月（3/6/9/12）25 日起，或财报季首周（1/4/7/10 月 1-7 日）
                is_quarter_end = today.month in (3, 6, 9, 12) and today.day >= 25
                is_earnings_season = today.month in (1, 4, 7, 10) and 1 <= today.day <= 7
                if is_quarter_end or is_earnings_season:
                    label = 'quarter-end' if is_quarter_end else 'earnings-season'
                    self.logger.info('%s — refreshing fundamental data cache', label)
                    self._refresh_fundamentals()
            else:
                self.logger.info('Non-trading day — skipping')

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run_loop, daemon=True, name='Scheduler')
        t.start()
        return t

    def stop(self):
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
            broker = PaperBroker(portfolio_service=svc)
            broker.connect()

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
            monitor.start()
            logger.info('IntradayMonitor started (broker=PaperBroker, max_pos_pct=%.0f%%, llm=%s)',
                        max_pos_pct * 100, llm_service is not None)
        except Exception as e:
            logger.warning('IntradayMonitor start failed (non-fatal): %s', e)

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
            # 暴露为模块级变量，供 IntradayMonitor._run_exit_engine() 读取 pipeline_scores
            import backend.main as _self_module
            _self_module._strategy_runner = runner
            runner_t = threading.Thread(
                target=runner.run_loop, daemon=True, name='StrategyRunner')
            runner_t.start()
            logger.info('StrategyRunner started (DynamicWeightPipeline, dry_run=True)')
        except Exception as exc:
            logger.warning('StrategyRunner start failed (non-fatal): %s', exc)

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
