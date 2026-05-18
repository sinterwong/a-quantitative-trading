"""
quant_app/main.py — 启动器入口 (P3-2)

按 mode 装配 API + Worker:
  --mode all       (默认) API + Scheduler + IntradayMonitor + StrategyRunner
  --mode api       仅 API server (用于调试/对外提供查询接口)
  --mode worker    仅 Scheduler + IntradayMonitor + StrategyRunner (不开 HTTP)
  --mode both      (别名 → all,backward compat)
  --mode scheduler (别名 → worker,backward compat)

依然跑在同一 Python 进程内,但 API/Worker 子系统通过 quant_app.serve_api
与 quant_app.run_worker 两个模块解耦,未来可一行配置切到独立进程。
"""

from __future__ import annotations

import os
import sys
import argparse
import logging
import threading
import signal
from logging.handlers import RotatingFileHandler

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.dirname(THIS_DIR)
BACKEND_DIR = os.path.join(PROJ_DIR, 'backend')
LOG_FILE = os.path.join(BACKEND_DIR, 'backend.log')

# 启动时加载 .env(在访问 os.environ 之前)
_dotenv_path = os.path.join(PROJ_DIR, '.env')
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


# ── 共享单例:供 backend/api.py 等外部模块通过 get_*() 访问 ──────

_monitor = None
_broker = None


def get_monitor():
    """Return the IntradayMonitor instance, or None if not started."""
    return _monitor


def get_broker():
    """Return the shared broker instance, or None if not started."""
    return _broker


# ── Logging ────────────────────────────────────────────────────

def setup_logging():
    fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(name)s — %(message)s')
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    fh = RotatingFileHandler(
        LOG_FILE, encoding='utf-8',
        maxBytes=100 * 1024 * 1024, backupCount=5,
    )
    fh.setFormatter(fmt)

    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)

    root.addHandler(fh)
    root.addHandler(sh)
    return logging.getLogger('backend')


# ── mode 别名规范化 ─────────────────────────────────────────────

_MODE_ALIASES = {'both': 'all', 'scheduler': 'worker'}


def _normalize_mode(mode: str) -> str:
    return _MODE_ALIASES.get(mode, mode)


# ── 入口 ────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Quant trading entry point')
    parser.add_argument('--host', default='0.0.0.0',
                        help='Bind host (default 0.0.0.0 = LAN; 127.0.0.1 = local only)')
    parser.add_argument('--port', type=int, default=5555)
    parser.add_argument(
        '--mode',
        choices=['all', 'api', 'worker', 'both', 'scheduler'],
        default='all',
        help='all (默认) | api | worker | both (=all) | scheduler (=worker)',
    )
    args = parser.parse_args()
    mode = _normalize_mode(args.mode)

    logger = setup_logging()

    # OS 级单实例锁 (P3-1)
    try:
        if PROJ_DIR not in sys.path:
            sys.path.insert(0, PROJ_DIR)
        from core.single_instance import acquire_singleton, SingletonError
        lock = acquire_singleton('quant-backend', lock_dir=BACKEND_DIR)
        logger.info('[Backend] Singleton lock acquired: %s', lock.lock_file)
    except SingletonError as e:
        logger.error('[Backend] 已有实例在运行 (PID=%d, lock=%s),退出。',
                     e.holder_pid, e.lock_file)
        sys.exit(1)

    logger.info('Backend starting in %s mode', mode)

    # 遗留 JSON 配置 deprecation 告警 (P3-3)
    try:
        from core.config import warn_legacy_configs
        warn_legacy_configs(logger=logger)
    except Exception:
        pass

    # ImpactEstimator 热加载 TCA 校准 — 之前 load_from_config 没人调,
    # daily_tca 的反馈系数永远不会进生产路径,VWAP/TWAP 一直用初值 5.0。
    try:
        from core.execution.impact_estimator import ImpactEstimator
        ok_ = ImpactEstimator.load_from_config()
        logger.info(
            '[ImpactEstimator] load_from_config: ok=%s perm=%.2f temp=%.2f',
            ok_, ImpactEstimator.PERMANENT_COEFF, ImpactEstimator.TEMPORARY_COEFF,
        )
    except Exception as e:
        logger.warning('ImpactEstimator load_from_config failed (non-fatal): %s', e)

    # ── Worker 子系统 ────────────────────────────────────────
    from quant_app.run_worker import Scheduler, build_intraday_monitor, start_strategy_runner_thread

    sched = None
    sched_thread = None
    if mode in ('all', 'worker'):
        sched = Scheduler(api_port=args.port)
        sched_thread = sched.start()

    def _on_shutdown():
        logger.info('Shutdown signal received...')
        if sched is not None:
            sched.stop()

    signal.signal(signal.SIGINT, lambda *_: _on_shutdown())
    signal.signal(signal.SIGTERM, lambda *_: _on_shutdown())

    global _monitor, _broker
    monitor = None
    if mode in ('all', 'worker'):
        try:
            monitor, broker = build_intraday_monitor(args.port, logger)
            _monitor = monitor
            _broker = broker
        except Exception as e:
            logger.warning('IntradayMonitor init failed (non-fatal): %s', e)

    # ── API 子系统(StrategyRunner 必须在 API 之后启动,runner 需要查 watchlist)──
    if mode in ('all', 'api'):
        from quant_app.serve_api import start_api_server
        logger.info('Starting API server thread')
        api_t = threading.Thread(
            target=start_api_server, args=(args.host, args.port, logger),
            daemon=True, name='APIServer',
        )
        api_t.start()
        logger.info('API: http://%s:%s', args.host, args.port)

    # ── StrategyRunner(Worker 模式才启动)─────────────────
    if mode in ('all', 'worker'):
        start_strategy_runner_thread(args.port, monitor, logger)

    # ── 启动 IntradayMonitor(StrategyRunner 注入后才安全)──
    if monitor is not None:
        try:
            monitor.start()
            logger.info('IntradayMonitor started')
        except Exception as e:
            logger.warning('IntradayMonitor start failed (non-fatal): %s', e)

    logger.info('Backend running. Press Ctrl+C to stop.')

    try:
        if sched_thread is not None:
            while sched_thread.is_alive():
                sched_thread.join(timeout=5)
        else:
            # mode=api: 阻塞等待信号
            signal.pause()
    except KeyboardInterrupt:
        _on_shutdown()
        if monitor:
            monitor.stop()


if __name__ == '__main__':
    main()
