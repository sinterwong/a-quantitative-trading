"""
quant_app/run_worker.py — Scheduler + IntradayMonitor + StrategyRunner (P3-2)

仅 Worker 子系统:
  - Scheduler 类
  - 交易日历 / 交易日判断 / PID 锁 / next-time 计算等纯工具
  - build_intraday_monitor / start_strategy_runner_thread 装配函数

不依赖 API 进程,可独立测试。
"""

from __future__ import annotations

import json
import os
import sys
import time
import threading
import logging
import fcntl as _fcntl
from datetime import datetime, timedelta
from typing import Optional

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJ_DIR = os.path.dirname(THIS_DIR)
BACKEND_DIR = os.path.join(PROJ_DIR, 'backend')


# ── 交易日历 ──────────────────────────────────────────────────────

_trade_calendar: set = set()
_trade_calendar_date: str = ''

_TRADE_CAL_CACHE = os.path.join(PROJ_DIR, 'data', 'trade_calendar.json')
# 缓存超过这么久就视为"过期",仍可作为 last-resort 降级使用,但会同时打 warning
_TRADE_CAL_CACHE_STALE_DAYS = 30


def _save_trade_calendar_cache(dates: set) -> None:
    """成功从 AKShare 拿到日历时,把结果写到 data/trade_calendar.json。
    后续 AKShare 不可用时优先用这份缓存,而不是退化成"周一到周五"。"""
    try:
        os.makedirs(os.path.dirname(_TRADE_CAL_CACHE), exist_ok=True)
        payload = {
            'fetched_at': datetime.now().isoformat(timespec='seconds'),
            'dates': sorted(dates),
        }
        tmp = _TRADE_CAL_CACHE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, _TRADE_CAL_CACHE)
    except Exception as exc:
        logging.getLogger('backend.scheduler').warning(
            'trade_calendar cache save failed: %s', exc,
        )


def _load_trade_calendar_cache():
    """读缓存。返回 (dates_set, fetched_at_datetime) 或 (None, None)。"""
    if not os.path.exists(_TRADE_CAL_CACHE):
        return None, None
    try:
        with open(_TRADE_CAL_CACHE, encoding='utf-8') as f:
            data = json.load(f)
        dates = set(data.get('dates') or [])
        fetched_at = data.get('fetched_at')
        fetched_dt = datetime.fromisoformat(fetched_at) if fetched_at else None
        return dates, fetched_dt
    except Exception:
        return None, None


def _build_trade_calendar() -> set:
    """从 AKShare 拉 A 股交易日历;成功 → 写缓存;失败 → 读缓存兜底。
    缓存也没有时返回空集合(由 is_trading_day 再降级为工作日判断)。"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        dates = {str(d)[:10] for d in df.iloc[:, 0]}
        if dates:
            _save_trade_calendar_cache(dates)
            return dates
    except Exception as exc:
        logging.getLogger('backend.scheduler').warning(
            'AKShare trade_calendar fetch failed (%s),尝试本地缓存', exc,
        )

    cached, fetched_at = _load_trade_calendar_cache()
    if cached:
        if fetched_at is not None:
            age = (datetime.now() - fetched_at).days
            if age > _TRADE_CAL_CACHE_STALE_DAYS:
                logging.getLogger('backend.scheduler').warning(
                    'trade_calendar 缓存已 %d 天未刷新,可能漏新公布的节假日', age,
                )
        return cached
    return set()


def is_trading_day() -> bool:
    """是否为 A 股交易日。

    优先级:
      1. AKShare 拉到的最新日历(并落地缓存)
      2. 本地 data/trade_calendar.json 缓存(网络挂掉时兜底)
      3. 都没有 → 工作日(Mon-Fri)
         注意第 3 层会在节假日误判,只能作为最坏情况的回退。
    """
    global _trade_calendar, _trade_calendar_date
    today_str = datetime.now().strftime('%Y-%m-%d')

    if _trade_calendar_date != today_str:
        cal = _build_trade_calendar()
        if cal:
            _trade_calendar = cal
            _trade_calendar_date = today_str

    if _trade_calendar:
        return today_str in _trade_calendar

    logging.getLogger('backend.scheduler').warning(
        'trade_calendar 不可用(AKShare 失败 + 无本地缓存),退化为工作日判断,'
        '可能在法定节假日误触发任务',
    )
    return datetime.now().weekday() < 5


def wait_until_next(target_hour: int = 15, target_min: int = 10) -> float:
    """返回距离下一个目标时间的秒数(CST = UTC+8)。"""
    now = datetime.now()
    target = now.replace(hour=target_hour, minute=target_min, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return max((target - now).total_seconds(), 0)


# ── PID 锁(沿用 P3-1 之前的原子实现)──────────────────────────────

def _acquire_pid_lock(pid_file: str) -> bool:
    """原子获取 PID 文件锁。True=拿到锁,False=已有实例。"""
    import errno as _errno

    pid_dir = os.path.dirname(pid_file) or '.'
    os.makedirs(pid_dir, exist_ok=True)

    # 第一关:O_CREAT|O_EXCL 原子创建
    try:
        fd = os.open(pid_file, os.O_CREAT | os.O_EXCL | os.O_RDWR)
    except OSError as exc:
        if exc.errno == _errno.EEXIST:
            pass  # 锁文件存在,走第二关
        else:
            return False
    else:
        try:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
            os.write(fd, b'%d' % os.getpid())
            os.fsync(fd)
            return True
        finally:
            pass

    # 第二关:验证旧 PID 是否还活着
    try:
        pf = open(pid_file, 'r+')
    except (IOError, OSError):
        try:
            pf = open(pid_file, 'r+')
        except (IOError, OSError):
            return False

    try:
        _fcntl.flock(pf.fileno(), _fcntl.LOCK_EX)
        old_pid_str = pf.read().strip()
        if old_pid_str:
            try:
                old_pid = int(old_pid_str)
                if old_pid != os.getpid():
                    os.kill(old_pid, 0)
                    return False
            except (ValueError, OSError):
                pass
        pf.seek(0)
        pf.truncate()
        pf.write(f'{os.getpid()}')
        pf.flush()
        os.fsync(pf.fileno())
        return True
    finally:
        pf.close()


def _release_pid_lock(pid_file: str) -> None:
    """退出时释放 PID 文件锁。"""
    try:
        os.unlink(pid_file)
    except Exception:
        pass


# ── Scheduler ─────────────────────────────────────────────────────

class Scheduler:
    """
    统一调度器 — 交易日自动化核心引擎。

    每日定时任务(北京时间):
      09:30 — 早盘自动化(选股→watchlist→RSI信号→下单→早报飞书)
      09:31 — 盘中信号监控开启(IntradayMonitor)
      15:00 — 收盘晚报
      15:10 — 日终选股分析
      15:30 — 每日组合风险报告(CVaR + 蒙特卡洛)
      15:45 — TCA 反馈闭环
      16:00 — 每日运营报告

    非交易日全部跳过。防重复:同一任务在 ±60 秒触发窗口内只触发一次。
    """

    DAILY_TASKS = [
        (9,  30, '_trigger_morning_runner'),
        (9,  31, '_trigger_intraday_monitor'),
        (15,  0, '_trigger_afternoon_report'),
        (15, 10, '_trigger_analysis'),
        (15, 30, '_trigger_daily_risk_report'),
        (15, 45, '_trigger_daily_tca'),
        (16,  0, '_trigger_daily_ops_report'),
    ]

    def __init__(self, api_port: int = 5555):
        self.api_port = api_port
        self.logger = logging.getLogger('backend.scheduler')
        self._stop = threading.Event()
        self._triggered_today: set = set()
        self._triggered_date: str = ''

    # ── 任务触发 ─────────────────────────────────────────

    def _trigger_morning_runner(self):
        """09:30 — 调用 morning_runner.run()。"""
        self.logger.info('[Scheduler] 09:30 — triggering morning_runner')
        try:
            import importlib.util
            scripts_dir = os.path.join(PROJ_DIR, 'scripts')
            sys.path.insert(0, scripts_dir)
            spec = importlib.util.spec_from_file_location(
                'morning_runner', os.path.join(scripts_dir, 'morning_runner.py'))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.run()
            self.logger.info('[Scheduler] morning_runner completed')
        except Exception as e:
            self.logger.error('[Scheduler] morning_runner failed: %s', e)

    def _trigger_intraday_monitor(self):
        """09:31 — 启动盘中信号扫描线程。"""
        self.logger.info('[Scheduler] 09:31 — triggering intraday monitor')
        try:
            # 延迟引用,避免循环 import
            from quant_app import main as _qm
            monitor = getattr(_qm, '_monitor', None)
            if monitor is not None:
                monitor.start()
                self.logger.info('[Scheduler] IntradayMonitor started')
            else:
                self.logger.warning('[Scheduler] IntradayMonitor not yet initialized')
        except Exception as e:
            self.logger.error('[Scheduler] IntradayMonitor start failed: %s', e)

    def _trigger_afternoon_report(self):
        """15:00 — 调用 afternoon_report.run()。"""
        self.logger.info('[Scheduler] 15:00 — triggering afternoon_report')
        try:
            import importlib.util
            scripts_dir = os.path.join(PROJ_DIR, 'scripts')
            sys.path.insert(0, scripts_dir)
            spec = importlib.util.spec_from_file_location(
                'afternoon_report', os.path.join(scripts_dir, 'afternoon_report.py'))
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mod.run()
            self.logger.info('[Scheduler] afternoon_report completed')
        except Exception as e:
            self.logger.error('[Scheduler] afternoon_report failed: %s', e)

    def _trigger_analysis(self):
        """15:10 — HTTP POST /analysis/run + 周期性任务(行业轮动 / 配对交易 / 基本面)。"""
        self.logger.info('[Scheduler] 15:10 — triggering /analysis/run')
        import urllib.request
        try:
            req = urllib.request.Request(
                f'http://127.0.0.1:{self.api_port}/analysis/run', method='POST')
            with urllib.request.urlopen(req, timeout=120) as r:
                body = r.read()
            self.logger.info('[Scheduler] analysis triggered: %s',
                             body.decode('utf-8', errors='replace')[:200])
        except Exception as e:
            self.logger.error('[Scheduler] /analysis/run failed: %s', e)

        now = datetime.now()
        if now.weekday() == 0:
            self._trigger_sector_rotation()
            is_quarter_end = now.month in (3, 6, 9, 12) and now.day >= 25
            is_earnings_season = now.month in (1, 4, 7, 10) and 1 <= now.day <= 7
            if is_quarter_end or is_earnings_season:
                label = 'quarter-end' if is_quarter_end else 'earnings-season'
                self.logger.info('[Scheduler] %s — refreshing fundamental data', label)
                self._refresh_fundamentals()

        if now.weekday() == 2:
            self._trigger_pairs_trading()

    def _trigger_sector_rotation(self):
        """每周一 15:10 后 — HTTP POST /analysis/sector_rotation。"""
        self.logger.info('[Scheduler] Monday — triggering sector rotation')
        import urllib.request, json as _json
        try:
            req = urllib.request.Request(
                f'http://127.0.0.1:{self.api_port}/analysis/sector_rotation',
                data=_json.dumps({}).encode(), method='POST',
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=60) as r:
                data = _json.loads(r.read())
            buy = data.get('data', {}).get('buy', [])
            sell = data.get('data', {}).get('sell', [])
            self.logger.info('[Scheduler] 行业轮动信号 — 买入: %s  卖出: %s', buy, sell)
        except Exception as e:
            self.logger.error('[Scheduler] sector rotation failed: %s', e)

    def _trigger_pairs_trading(self):
        """每周三 15:10 后 — HTTP POST /analysis/pairs_trading + 告警。"""
        self.logger.info('[Scheduler] Wednesday — triggering pairs trading scan')
        import urllib.request, json as _json
        from pathlib import Path

        symbols = []
        try:
            with urllib.request.urlopen(
                f'http://127.0.0.1:{self.api_port}/watchlist', timeout=5,
            ) as r:
                data = _json.loads(r.read())
            symbols = [w['symbol'] for w in data.get('watchlist', []) if w.get('symbol')]
        except Exception as e:
            self.logger.warning('[Scheduler] watchlist fetch failed: %s', e)
        if len(symbols) < 2:
            self.logger.info('[Scheduler] pairs_trading skipped: <2 symbols in watchlist')
            return

        try:
            payload = _json.dumps({'symbols': symbols}).encode()
            req = urllib.request.Request(
                f'http://127.0.0.1:{self.api_port}/analysis/pairs_trading',
                data=payload, method='POST',
                headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=120) as r:
                resp = _json.loads(r.read())
        except Exception as e:
            self.logger.error('[Scheduler] pairs_trading API failed: %s', e)
            return

        pairs = resp.get('data', {}).get('pairs', resp.get('pairs', []))
        n_found = resp.get('data', {}).get('n_pairs_found', resp.get('n_pairs_found', 0))
        self.logger.info('[Scheduler] pairs_trading: %d pair(s) found', n_found)

        try:
            out_dir = Path(PROJ_DIR) / 'outputs' / 'pairs_signals'
            out_dir.mkdir(parents=True, exist_ok=True)
            today_str = datetime.now().strftime('%Y-%m-%d')
            with open(out_dir / f'pairs_{today_str}.json', 'w', encoding='utf-8') as f:
                _json.dump({
                    'date': today_str, 'symbols': symbols,
                    'pairs': pairs, 'n_pairs_found': n_found,
                }, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.warning('[Scheduler] pairs_trading write failed: %s', e)

        try:
            entry_z = 2.0
            actionable = []
            for p in pairs or []:
                sig = p.get('signal') or {}
                z = float(sig.get('spread_zscore', 0))
                if abs(z) >= entry_z:
                    actionable.append((
                        p.get('symbol_a', '?'), p.get('symbol_b', '?'), z,
                        sig.get('action_a', '?'), sig.get('action_b', '?'),
                    ))
            if actionable:
                from core.alerting import get_alert_manager
                lines = ['📊 配对交易信号触发：']
                for a, b, z, aa, ab in actionable[:5]:
                    lines.append(f'  • {a}/{b}: z={z:+.2f} | {a}={aa} {b}={ab}')
                get_alert_manager().send_warning('\n'.join(lines))
        except Exception as e:
            self.logger.warning('[Scheduler] pairs_trading alert failed: %s', e)

    def _refresh_fundamentals(self):
        """季度末 / 财报季强制刷新持仓标的基本面。"""
        import urllib.request, json as _json
        try:
            with urllib.request.urlopen(
                f'http://127.0.0.1:{self.api_port}/positions', timeout=5,
            ) as r:
                d = _json.loads(r.read())
            symbols = [p['symbol'] for p in d.get('positions', []) if p.get('shares', 0) > 0]
        except Exception:
            symbols = []

        if not symbols:
            self.logger.info('[Scheduler] Fundamental refresh: no positions, skipping')
            return

        sys.path.insert(0, PROJ_DIR)
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

    def _trigger_daily_risk_report(self):
        """15:30 — 每日组合风险报告(CVaR + 蒙特卡洛)。"""
        self.logger.info('[Scheduler] 15:30 — triggering daily risk report')
        try:
            sys.path.insert(0, PROJ_DIR)
            from scripts.daily_risk_report import run_report
            summary = run_report(
                n_simulations=10000, horizon_days=21,
                api_port=self.api_port, enable_alert=True,
            )
            breach = summary.get('breach', [])
            equity = summary.get('equity', 0.0)
            n_pos = summary.get('positions_count', 0)
            if breach:
                self.logger.warning(
                    '[Scheduler] risk report — equity=%.0f positions=%d BREACH=%s',
                    equity, n_pos, breach,
                )
            else:
                self.logger.info(
                    '[Scheduler] risk report ok — equity=%.0f positions=%d',
                    equity, n_pos,
                )
        except Exception as e:
            self.logger.error('[Scheduler] daily risk report failed: %s', e)

    def _trigger_daily_tca(self):
        """15:45 — TCA 反馈闭环。"""
        self.logger.info('[Scheduler] 15:45 — triggering daily TCA report')
        try:
            sys.path.insert(0, PROJ_DIR)
            from scripts.daily_tca import run_report
            summary = run_report(api_port=self.api_port, enable_alert=True)
            n = summary.get('n_trades', 0)
            avg_is = summary.get('avg_is_bps', 0.0)
            self.logger.info(
                '[Scheduler] TCA done — n_trades=%d avg_is=%.2f bps', n, avg_is,
            )
        except Exception as e:
            self.logger.error('[Scheduler] daily TCA failed: %s', e)

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
            self.logger.info('[Scheduler] ops report done — trades=%d unrealized_pnl=%.2f',
                             n_trades, pnl)
        except Exception as e:
            self.logger.error('[Scheduler] daily ops report failed: %s', e)

    # ── 核心循环 ─────────────────────────────────────────

    def _run_loop(self):
        self.logger.info('[Scheduler] started — tasks: %s',
                         [(f'{h:02d}:{m:02d}', fn) for h, m, fn in self.DAILY_TASKS])

        while not self._stop.is_set():
            now = datetime.now()
            today_str = now.strftime('%Y-%m-%d')

            if self._triggered_date != today_str:
                self._triggered_today.clear()
                self._triggered_date = today_str
                self.logger.info('[Scheduler] 新的一天 %s，重置触发记录', today_str)

            # 非交易日整体休眠至次日 08:25
            if not is_trading_day():
                sleep_secs = self._seconds_until_next_check(now)
                self.logger.info('[Scheduler] 非交易日，sleep %.0f 秒（约 %.1f 小时）',
                                 sleep_secs, sleep_secs / 3600)
                if self._stop.wait(timeout=sleep_secs):
                    return
                continue

            # 检查每个定时任务
            for target_hour, target_min, method_name in self.DAILY_TASKS:
                task_key = (method_name, target_hour, target_min)

                if task_key in self._triggered_today:
                    continue

                target = now.replace(hour=target_hour, minute=target_min,
                                     second=0, microsecond=0)
                if abs((now - target).total_seconds()) < 60:
                    self._triggered_today.add(task_key)
                    self.logger.info('[Scheduler] >>> %02d:%02d 触发 %s',
                                     target_hour, target_min, method_name)
                    handler = getattr(self, method_name, None)
                    if handler:
                        t = threading.Thread(target=handler, name=f'Scheduler-{method_name}')
                        t.start()
                        t.join()
                    else:
                        self.logger.error('[Scheduler] 方法不存在: %s', method_name)
                    break
            else:
                time.sleep(30)

    @staticmethod
    def _seconds_until_next_check(now: datetime) -> float:
        """非交易日休眠秒数:到次日 08:25。"""
        next_check = (now + timedelta(days=1)).replace(
            hour=8, minute=25, second=0, microsecond=0)
        delta = (next_check - now).total_seconds()
        return max(60.0, delta)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self._run_loop, daemon=True, name='Scheduler')
        t.start()
        return t

    def stop(self):
        self.logger.info('[Scheduler] stopping')
        self._stop.set()


# ── Worker 装配辅助函数 ──────────────────────────────────────────

def build_intraday_monitor(api_port: int, logger):
    """构造 IntradayMonitor + PaperBroker + LLM service。返回 (monitor, broker)。"""
    sys.path.insert(0, BACKEND_DIR)
    from services.intraday_monitor import IntradayMonitor
    from services.portfolio import PortfolioService
    from services.broker import PaperBroker

    svc = None
    try:
        from api import _svc as _api_svc
        svc = _api_svc
    except ImportError:
        pass
    if svc is None:
        svc = PortfolioService()

    broker = PaperBroker(portfolio_service=svc)
    broker.connect()

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
    logger.info('IntradayMonitor created (broker=PaperBroker, max_pos_pct=%.0f%%, llm=%s)',
                max_pos_pct * 100, llm_service is not None)
    return monitor, broker


def start_strategy_runner_thread(api_port: int, monitor, logger) -> Optional[threading.Thread]:
    """启动 StrategyRunner 后台线程,并注入 monitor。返回线程对象;失败返回 None。"""
    try:
        from core.pipeline_factory import build_runner

        def _runner_symbols():
            """持仓 ∪ watchlist;空时退化为宽基 ETF。"""
            import urllib.request as _req, json as _j
            symbols: set = set()
            try:
                with _req.urlopen(f'http://127.0.0.1:{api_port}/positions', timeout=3) as r:
                    d = _j.loads(r.read())
                for p in d.get('positions', []):
                    if p.get('shares', 0) > 0 and p.get('symbol'):
                        symbols.add(p['symbol'])
            except Exception:
                pass
            try:
                with _req.urlopen(f'http://127.0.0.1:{api_port}/watchlist', timeout=3) as r:
                    d = _j.loads(r.read())
                for w in d.get('watchlist', []):
                    sym = w.get('symbol')
                    if sym:
                        symbols.add(sym)
            except Exception:
                pass
            return sorted(symbols) if symbols else ['510300.SH', '159915.SZ', '512690.SH']

        runner = build_runner(
            symbols=_runner_symbols,
            dry_run=True,
            interval=300,
            signal_threshold=0.5,
        )
        if monitor is not None:
            monitor.set_strategy_runner(runner)
        # AsyncStrategyRunner.run_loop 是协程,需通过 run_sync 调起
        target_fn = getattr(runner, 'run_sync', None) or runner.run_loop
        runner_t = threading.Thread(
            target=target_fn, daemon=True, name='StrategyRunner')
        runner_t.start()
        logger.info('StrategyRunner started (%s, DynamicWeightPipeline, dry_run=True)',
                    type(runner).__name__)
        return runner_t
    except Exception as exc:
        logger.warning('StrategyRunner start failed (non-fatal): %s', exc)
        return None
