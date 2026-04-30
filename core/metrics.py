"""
core/metrics.py — Prometheus 指标收集与暴露

功能：
  - 定义系统核心监控指标（Gauge / Counter / Histogram）
  - 提供 update_from_portfolio() 刷新接口
  - 暴露 /metrics 端点（generate_latest() 格式）

指标清单：
  trading_net_value          : 当前组合净值（归一化，初始=1.0）
  trading_total_pnl_yuan     : 累计浮动盈亏（元）
  trading_n_positions        : 当前持仓数量
  trading_cash_yuan          : 可用现金（元）
  trading_signal_count_total : 累计产生信号次数（Counter）
  trading_order_latency_ms   : 下单到成交延迟分布（Histogram）
  trading_api_requests_total : API 请求计数（Counter，按 endpoint）
  trading_api_errors_total   : API 错误计数（Counter，按 endpoint）
  trading_health_status      : 策略健康状态（0=OK, 1=WARN, 2=CRITICAL）
  trading_factor_ic          : 各因子最新 IC 值（Gauge，label=factor_name）

使用：
    from core.metrics import MetricsRegistry, get_registry

    # 在 backend/api.py 添加 /metrics 路由
    @app.route('/metrics')
    def metrics_endpoint():
        from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
        reg = get_registry()
        reg.update_from_api(api_port=5555)
        return generate_latest(reg.registry), 200, {'Content-Type': CONTENT_TYPE_LATEST}

    # 在下单路径记录延迟
    reg = get_registry()
    with reg.order_latency.time():
        broker.submit_order(order)

    # 记录信号
    reg.signal_count.inc()
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional

logger = logging.getLogger('core.metrics')

# ---------------------------------------------------------------------------
# MetricsRegistry
# ---------------------------------------------------------------------------

class MetricsRegistry:
    """
    Prometheus 指标注册表。

    单例模式：通过 get_registry() 获取全局实例，避免重复注册。
    prometheus_client 缺失时所有操作静默降级（不抛异常）。
    """

    def __init__(self):
        self._available = False
        self._lock = threading.Lock()
        self._last_update: float = 0.0

        try:
            from prometheus_client import (
                CollectorRegistry, Gauge, Counter, Histogram,
            )
            self.registry = CollectorRegistry()

            # --- 组合状态 ---
            self.net_value = Gauge(
                'trading_net_value',
                '组合净值（归一化，初始=1.0）',
                registry=self.registry,
            )
            self.total_pnl = Gauge(
                'trading_total_pnl_yuan',
                '累计浮动盈亏（元）',
                registry=self.registry,
            )
            self.n_positions = Gauge(
                'trading_n_positions',
                '当前持仓标的数量',
                registry=self.registry,
            )
            self.cash = Gauge(
                'trading_cash_yuan',
                '可用现金（元）',
                registry=self.registry,
            )

            # --- 信号 / 订单 ---
            self.signal_count = Counter(
                'trading_signal_count_total',
                '累计产生信号次数',
                registry=self.registry,
            )
            self.order_latency = Histogram(
                'trading_order_latency_ms',
                '下单到确认延迟（毫秒）',
                buckets=[10, 50, 100, 250, 500, 1000, 2000, 5000],
                registry=self.registry,
            )

            # --- API 请求 ---
            self.api_requests = Counter(
                'trading_api_requests_total',
                'API 请求计数',
                ['endpoint', 'method'],
                registry=self.registry,
            )
            self.api_errors = Counter(
                'trading_api_errors_total',
                'API 错误计数',
                ['endpoint', 'status_code'],
                registry=self.registry,
            )
            self.api_latency = Histogram(
                'trading_api_latency_ms',
                'API 响应延迟（毫秒）',
                ['endpoint'],
                buckets=[5, 20, 50, 100, 250, 500, 1000],
                registry=self.registry,
            )

            # --- 策略健康 ---
            self.health_status = Gauge(
                'trading_health_status',
                '策略健康状态（0=OK, 1=WARN, 2=CRITICAL）',
                registry=self.registry,
            )

            # --- 因子 IC ---
            self.factor_ic = Gauge(
                'trading_factor_ic',
                '各因子最新 IC 值',
                ['factor_name'],
                registry=self.registry,
            )

            self._available = True
            logger.info('MetricsRegistry initialized (prometheus_client available)')

        except ImportError:
            logger.warning('prometheus_client not installed — metrics disabled')
        except Exception as e:
            logger.error('MetricsRegistry init failed: %s', e)

    @property
    def available(self) -> bool:
        return self._available

    # ------------------------------------------------------------------
    # 刷新接口
    # ------------------------------------------------------------------

    def update_from_portfolio(
        self,
        net_value: float = 1.0,
        total_pnl: float = 0.0,
        n_positions: int = 0,
        cash: float = 0.0,
    ) -> None:
        """直接更新组合状态指标。"""
        if not self._available:
            return
        try:
            self.net_value.set(net_value)
            self.total_pnl.set(total_pnl)
            self.n_positions.set(n_positions)
            self.cash.set(cash)
            self._last_update = time.time()
        except Exception as e:
            logger.warning('metrics update_from_portfolio failed: %s', e)

    def update_from_api(self, api_port: int = 5555, timeout: int = 5) -> None:
        """从本地 backend API 拉取组合数据并刷新指标。"""
        if not self._available:
            return
        try:
            import json, urllib.request
            url = f'http://127.0.0.1:{api_port}/portfolio/summary'
            with urllib.request.urlopen(url, timeout=timeout) as r:
                data = json.loads(r.read())

            summary = data.get('summary', data)
            net_val  = float(summary.get('net_value', 1.0))
            pnl      = float(summary.get('total_pnl', summary.get('total_unrealized_pnl', 0.0)))
            n_pos    = int(summary.get('n_positions', 0))
            cash_val = float(summary.get('cash', 0.0))
            self.update_from_portfolio(net_val, pnl, n_pos, cash_val)
        except Exception as e:
            logger.debug('metrics update_from_api failed (non-fatal): %s', e)

    def set_health(self, level: str) -> None:
        """更新策略健康状态。level: 'OK' | 'WARN' | 'CRITICAL'"""
        if not self._available:
            return
        mapping = {'OK': 0, 'WARN': 1, 'CRITICAL': 2}
        self.health_status.set(mapping.get(level.upper(), 0))

    def set_factor_ic(self, factor_name: str, ic_value: float) -> None:
        """更新单个因子 IC 值。"""
        if not self._available:
            return
        try:
            self.factor_ic.labels(factor_name=factor_name).set(ic_value)
        except Exception as e:
            logger.warning('set_factor_ic failed: %s', e)

    def record_api_request(self, endpoint: str, method: str,
                            latency_ms: float, status_code: int) -> None:
        """记录 API 请求指标（请求数、延迟、错误）。"""
        if not self._available:
            return
        try:
            self.api_requests.labels(endpoint=endpoint, method=method).inc()
            self.api_latency.labels(endpoint=endpoint).observe(latency_ms)
            if status_code >= 400:
                self.api_errors.labels(endpoint=endpoint,
                                        status_code=str(status_code)).inc()
        except Exception as e:
            logger.warning('record_api_request failed: %s', e)

    def generate(self) -> bytes:
        """生成 Prometheus 文本格式输出。"""
        if not self._available:
            return b'# metrics not available (prometheus_client not installed)\n'
        try:
            from prometheus_client import generate_latest
            return generate_latest(self.registry)
        except Exception as e:
            logger.warning('generate_latest failed: %s', e)
            return b''

    @property
    def content_type(self) -> str:
        try:
            from prometheus_client import CONTENT_TYPE_LATEST
            return CONTENT_TYPE_LATEST
        except ImportError:
            return 'text/plain; version=0.0.4; charset=utf-8'


# ---------------------------------------------------------------------------
# 全局单例
# ---------------------------------------------------------------------------

_registry: Optional[MetricsRegistry] = None
_registry_lock = threading.Lock()


def get_registry() -> MetricsRegistry:
    """获取全局 MetricsRegistry 单例。"""
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = MetricsRegistry()
    return _registry


def reset_registry() -> None:
    """重置全局单例（主要用于测试隔离）。"""
    global _registry
    with _registry_lock:
        _registry = None
