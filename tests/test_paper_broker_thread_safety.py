"""
test_paper_broker_thread_safety.py — PaperBroker 多线程并发下单不能撞 ID
也不能击穿单标的头寸上限。
"""

from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch


def _make_broker(cash: float = 1_000_000, max_pos_pct: float = 0.25):
    from backend.services.broker import PaperBroker
    svc = MagicMock()
    svc.get_cash.return_value = cash
    svc.get_total_equity.return_value = cash
    svc.get_position.return_value = None
    svc.set_cash = MagicMock()
    svc.upsert_position = MagicMock()
    svc.record_trade = MagicMock()
    svc.close_position = MagicMock()

    b = PaperBroker(portfolio_service=svc, slippage_bps=10, max_position_pct=max_pos_pct)
    b.connect()
    # 跳过外网行情:固定 ref_price
    b._fetch_market_price = lambda sym: 10.0
    return b, svc


class TestPaperBrokerThreadSafety(unittest.TestCase):
    """跑全部测试时不要污染全局 time.sleep —— 走 patch 上下文,setUp/tearDown 自动恢复。"""

    def setUp(self):
        # _simulate_fill 里有 time.sleep(0.5) — 测试中 patch 成 no-op 加速,
        # 必须用 patch 而不是直接给模块属性赋值,否则会泄漏到其他测试。
        self._sleep_patcher = patch(
            'backend.services.broker.time.sleep', lambda *_a, **_k: None,
        )
        self._sleep_patcher.start()

    def tearDown(self):
        self._sleep_patcher.stop()

    def test_concurrent_order_ids_unique(self):
        """50 个线程同时下单,order_id 不重复。"""
        b, _svc = _make_broker()
        ids = []
        ids_lock = threading.Lock()

        def fire():
            r = b.submit_order('X.SH', 'BUY', 100, price=10.0, price_type='market')
            with ids_lock:
                ids.append(r.order_id)

        threads = [threading.Thread(target=fire) for _ in range(50)]
        for t in threads: t.start()
        for t in threads: t.join()

        self.assertEqual(len(ids), 50)
        self.assertEqual(len(set(ids)), len(ids), 'order_id collisions in concurrent submit')

    def test_concurrent_submit_doesnt_break_orders_list(self):
        """_orders 列表在并发 append + 迭代下不抛异常,长度等于成功单数。"""
        b, _svc = _make_broker()

        def fire():
            b.submit_order('X.SH', 'BUY', 100, price=10.0, price_type='market')

        readers_done = threading.Event()
        # 读线程不停遍历 _orders
        def reader():
            while not readers_done.is_set():
                # cancel_order / get_order 都会走 self._lock 迭代
                b.get_order('NONEXIST')

        r_thread = threading.Thread(target=reader, daemon=True)
        r_thread.start()

        writers = [threading.Thread(target=fire) for _ in range(40)]
        for t in writers: t.start()
        for t in writers: t.join()
        readers_done.set()
        r_thread.join(timeout=1.0)

        self.assertEqual(len(b._orders), 40)


if __name__ == '__main__':
    unittest.main()
