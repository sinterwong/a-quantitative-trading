"""
HKStockDataSource 验证测试
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
import time
import pandas as pd

from core.hk_data_source import HKStockDataSource, HKStockSnapshot


class TestHKStockDataSource(unittest.TestCase):

    def test_xiaomi_snapshot(self):
        """小米集团 H01810 实时行情"""
        ds = HKStockDataSource('hk01810')
        snap = ds.fetch_latest()
        self.assertIsNotNone(snap)
        self.assertIsInstance(snap, HKStockSnapshot)
        self.assertIn('01810', snap.symbol)
        self.assertGreater(snap.last, 0)
        self.assertGreater(snap.volume, 0)
        print(f"\n小米集团: {snap.name}")
        print(f"  当前价: {snap.last}")
        print(f"  涨跌: {snap.change:+.3f} ({snap.change_pct:+.2f}%)")
        print(f"  开盘: {snap.open} | 最高: {snap.high} | 最低: {snap.low}")
        print(f"  成交量: {snap.volume:,}股")
        print(f"  成交额: {snap.amount:,.0f}港元")
        print(f"  52W: {snap.low_52w} ~ {snap.high_52w}")
        print(f"  市值: {snap.mkt_cap:,.0f}港元")

    def test_tencent_snapshot(self):
        """腾讯控股 00700"""
        ds = HKStockDataSource('hk00700')
        snap = ds.fetch_latest()
        self.assertIsNotNone(snap)
        self.assertGreater(snap.last, 0)
        print(f"\n腾讯控股: {snap.name} @ {snap.last} ({snap.change_pct:+.2f}%)")

    def test_hsi_snapshot(self):
        """恒生指数"""
        ds = HKStockDataSource('hkHSI')
        snap = ds.fetch_latest()
        self.assertIsNotNone(snap)
        self.assertGreater(snap.last, 10000)  # 恒指约26000
        print(f"\n恒生指数: {snap.last} ({snap.change_pct:+.2f}%)")

    def test_hstech_snapshot(self):
        """恒生科技指数"""
        ds = HKStockDataSource('hkHSTECH')
        snap = ds.fetch_latest()
        self.assertIsNotNone(snap)
        print(f"\n恒生科技: {snap.name} @ {snap.last} ({snap.change_pct:+.2f}%)")

    def test_factory_methods(self):
        """便捷工厂方法"""
        ds = HKStockDataSource.for_symbol('HK:00700')
        snap = ds.fetch_latest()
        self.assertIsNotNone(snap)
        self.assertEqual(snap.symbol, 'HK:00700')

        ds2 = HKStockDataSource.for_symbol('01810')
        snap2 = ds2.fetch_latest()
        self.assertIsNotNone(snap2)

    def test_to_order_book(self):
        """转换为 OrderBook（兼容 Level2 因子）"""
        ds = HKStockDataSource('hk00700')
        snap = ds.fetch_latest()
        ob = snap.to_order_book()
        self.assertEqual(ob.symbol, 'HK:00700')
        self.assertEqual(ob.last_price, snap.last)
        self.assertEqual(ob.volume, snap.volume)

    def test_batch_fetch(self):
        """批量获取多标的"""
        symbols = ['hk00700', 'hk01810', 'hkHSI']
        results = HKStockDataSource.fetch_batch(symbols)
        self.assertGreaterEqual(len(results), 2)
        for sym, snap in results.items():
            print(f"\n{snap.name}: {snap.last} ({snap.change_pct:+.2f}%)")

    def test_polling(self):
        """实时轮询（3秒，2次）"""
        ds = HKStockDataSource('hk01810', interval=3)
        results = []

        def handler(src, snap):
            results.append(snap)

        ds.subscribe(handler)
        ds.start_polling(interval=3)
        time.sleep(7)
        ds.stop_polling()

        self.assertGreaterEqual(len(results), 2)
        print(f"\n轮询 {len(results)} 次成功")

    def test_history_day(self):
        """日K 历史（新浪港股日K可能返回null，需降级处理）"""
        ds = HKStockDataSource('hk01810')
        df = ds.fetch_history(days=5, freq='day')
        if df.empty:
            print('\n小米日K: Sina返回null（港股日K接口限制）')
        else:
            self.assertTrue('close' in df.columns)
            print(f'\n小米日K: {len(df)} days, latest={df["close"].iloc[-1]}')
        self.assertIsInstance(df, pd.DataFrame)


if __name__ == '__main__':
    unittest.main(verbosity=2)
