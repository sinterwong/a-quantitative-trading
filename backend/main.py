"""
main.py — Backend service entry point
=====================================
Starts the HTTP API server as a persistent background process.
Can also run the scheduler for automated daily analysis.

Usage:
    python main.py                    # start API server
    python main.py --mode scheduler   # start scheduler only
    python main.py --mode both       # API + scheduler
"""

import os
import sys
import argparse
import logging
import threading
import time
import signal

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
    parser.add_argument('--host', default='127.0.0.1')
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


if __name__ == '__main__':
    main()
