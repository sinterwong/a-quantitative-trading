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


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s — %(message)s',
        handlers=[
            logging.FileHandler(LOG_FILE, encoding='utf-8'),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger('backend')


# ============================================================
# Scheduler
# ============================================================

def is_trading_day():
    """Check if today is a weekday (simplified — no holiday check)."""
    from datetime import datetime
    wd = datetime.now().weekday()
    return wd < 5   # 0=Mon, 4=Fri


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
