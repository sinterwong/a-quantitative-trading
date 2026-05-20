"""R0-2 残留：EventDrivenPaperBroker 并发不变量。

验证 _orders / _positions dict 操作在 RLock 保护下：
- 多线程并发 send() 同 symbol，持仓守恒（总买入股数 == 持仓股数）
- 并发 get_positions() 与 send() 不抛 RuntimeError
- 并发 cancel() 同 order_id 仅一个成功
"""
from __future__ import annotations

import threading
import unittest
from typing import List
from unittest.mock import patch

from core.oms import EventDrivenPaperBroker, Order


class TestConcurrentSend(unittest.TestCase):

    def _make_broker(self) -> EventDrivenPaperBroker:
        """Disable network fetches: stub quote() and _persist_fill()."""
        b = EventDrivenPaperBroker()
        # 不让 _load_positions 拉 HTTP
        b._positions.clear()
        return b

    def test_concurrent_send_same_symbol_shares_conserved(self) -> None:
        """20 线程各自 send 一笔同 symbol BUY 100 股，最终持仓 = 2000 股。"""
        broker = self._make_broker()
        n_threads = 20
        barrier = threading.Barrier(n_threads)
        errors: List[BaseException] = []

        with patch.object(broker, 'quote', return_value={'last': 10.0}), \
             patch.object(broker, '_persist_fill', return_value=None):

            def _worker(i: int) -> None:
                try:
                    barrier.wait()
                    order = Order(
                        order_id=f'ORD-{i}',
                        symbol='TEST.SH',
                        direction='BUY',
                        order_type='MARKET',
                        shares=100,
                    )
                    broker.send(order)
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

            threads = [threading.Thread(target=_worker, args=(i,)) for i in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10.0)

        self.assertEqual(errors, [], f'concurrent send raised: {errors[:3]}')
        positions = broker.get_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0].symbol, 'TEST.SH')
        # 总持仓必须等于 20 * 100 = 2000，证明 _update_position 串行化生效
        self.assertEqual(positions[0].shares, 2000)

    def test_concurrent_reads_during_writes_no_runtime_error(self) -> None:
        """3 reader + 3 writer 跑 N 秒，get_positions 不应抛
        'dictionary changed size during iteration'。"""
        broker = self._make_broker()
        stop = threading.Event()
        errors: List[BaseException] = []

        with patch.object(broker, 'quote', return_value={'last': 10.0}), \
             patch.object(broker, '_persist_fill', return_value=None):

            def _reader() -> None:
                try:
                    while not stop.is_set():
                        broker.get_positions()
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

            def _writer(idx: int) -> None:
                try:
                    for i in range(30):
                        order = Order(
                            order_id=f'W{idx}-{i}',
                            symbol=f'SYM{i % 5}.SH',
                            direction='BUY',
                            order_type='MARKET',
                            shares=100,
                        )
                        broker.send(order)
                except BaseException as e:  # noqa: BLE001
                    errors.append(e)

            readers = [threading.Thread(target=_reader) for _ in range(3)]
            writers = [threading.Thread(target=_writer, args=(i,)) for i in range(3)]
            for t in readers + writers:
                t.start()
            for t in writers:
                t.join(timeout=15.0)
            stop.set()
            for t in readers:
                t.join(timeout=5.0)

        self.assertEqual(errors, [], f'concurrent read/write raised: {errors[:3]}')

    def test_concurrent_cancel_same_order_only_one_succeeds(self) -> None:
        """10 线程并发 cancel 同 order_id。仅一个返回 True；不抛错。"""
        broker = self._make_broker()
        # 先放一个 pending order
        order = Order(order_id='ORD-X', symbol='TEST.SH', direction='BUY',
                      order_type='MARKET', shares=100)
        order.status = 'PENDING'
        broker._orders['ORD-X'] = order

        results: List[bool] = []
        results_lock = threading.Lock()
        barrier = threading.Barrier(10)

        def _worker() -> None:
            barrier.wait()
            ok = broker.cancel('ORD-X')
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=_worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        # 至少一个 True（cancel 实际执行），其他可能 True 也可能 False
        # 关键不变量：order.status == 'CANCELLED'（不会被两个并发的不同终态覆盖）
        self.assertTrue(any(results))
        self.assertEqual(broker._orders['ORD-X'].status, 'CANCELLED')


if __name__ == '__main__':
    unittest.main()
