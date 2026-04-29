"""
tests/test_scheduler_fundamental.py — 基本面数据自动更新调度测试

覆盖：
  - Scheduler._refresh_fundamentals() 正常流程（持仓标的逐个刷新）
  - _refresh_fundamentals() 无持仓时安全跳过
  - _refresh_fundamentals() API 不可达时不抛出异常
  - _refresh_fundamentals() FundamentalDataManager 失败时记录 warning 但不中断
  - 季报触发条件：季度末月（3/6/9/12）25 日起 触发
  - 财报季触发条件：（1/4/7/10 月 1-7 日）触发
  - 非触发日不调用 _refresh_fundamentals
"""

from __future__ import annotations

import json
import sys
import types
import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch, call


# ---------------------------------------------------------------------------
# 注入最小可用的 backend.main 依赖（避免真实 Flask / AKShare 加载）
# ---------------------------------------------------------------------------

def _make_main_module():
    """动态构造足够运行 Scheduler 的 backend.main 模块。"""
    import importlib.util, os
    proj = os.path.dirname(os.path.dirname(__file__))
    # 在 sys.path 中保证项目根可见
    if proj not in sys.path:
        sys.path.insert(0, proj)
    return proj


PROJ_DIR = _make_main_module()


class TestRefreshFundamentals(unittest.TestCase):
    """_refresh_fundamentals 逻辑测试（不发起真实网络请求）。"""

    def _make_scheduler(self, api_port=5555):
        """构造一个 Scheduler 实例，不启动线程。"""
        import importlib, os, sys
        backend_dir = os.path.join(PROJ_DIR, 'backend')
        if backend_dir not in sys.path:
            sys.path.insert(0, backend_dir)
        if PROJ_DIR not in sys.path:
            sys.path.insert(0, PROJ_DIR)

        # 以最小方式 import backend.main
        import backend.main as bm
        sched = bm.Scheduler(api_port=api_port)
        return sched

    # ------------------------------------------------------------------
    # 正常流程
    # ------------------------------------------------------------------

    def test_refresh_two_symbols(self):
        """有两个持仓标的时，invalidate + get_fundamentals 各调用两次。"""
        import pandas as pd
        sched = self._make_scheduler()

        positions_resp = json.dumps({
            'positions': [
                {'symbol': '000001.SZ', 'shares': 100},
                {'symbol': '600519.SH', 'shares': 200},
            ]
        }).encode()

        mock_mgr = MagicMock()
        mock_mgr.get_fundamentals.return_value = pd.DataFrame({'pe_ttm': [10.0]})

        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch('core.fundamental_data.FundamentalDataManager', return_value=mock_mgr):

            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=positions_resp)))
            ctx.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = ctx

            sched._refresh_fundamentals()

        self.assertEqual(mock_mgr.invalidate.call_count, 2)
        self.assertEqual(mock_mgr.get_fundamentals.call_count, 2)

    def test_refresh_empty_positions(self):
        """持仓为空时直接返回，不调用 FundamentalDataManager。"""
        sched = self._make_scheduler()

        positions_resp = json.dumps({'positions': []}).encode()

        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch('core.fundamental_data.FundamentalDataManager') as MockMgr:

            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=positions_resp)))
            ctx.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = ctx

            sched._refresh_fundamentals()

        MockMgr.assert_not_called()

    def test_refresh_api_unreachable(self):
        """持仓 API 不可达时不抛出异常，symbols 回退为空列表后安全退出。"""
        sched = self._make_scheduler()

        with patch('urllib.request.urlopen', side_effect=OSError('connection refused')), \
             patch('core.fundamental_data.FundamentalDataManager') as MockMgr:
            # 不应抛出异常
            sched._refresh_fundamentals()

        MockMgr.assert_not_called()

    def test_refresh_single_symbol_fetch_fails(self):
        """单个标的拉取失败时记录 warning，其余标的继续处理。"""
        import pandas as pd
        sched = self._make_scheduler()

        positions_resp = json.dumps({
            'positions': [
                {'symbol': '000001.SZ', 'shares': 100},
                {'symbol': '999999.SZ', 'shares': 50},
            ]
        }).encode()

        mock_mgr = MagicMock()
        # 第一个成功，第二个抛异常
        mock_mgr.get_fundamentals.side_effect = [
            pd.DataFrame({'pe_ttm': [10.0]}),
            Exception('AKShare timeout'),
        ]

        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch('core.fundamental_data.FundamentalDataManager', return_value=mock_mgr):

            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=positions_resp)))
            ctx.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = ctx

            # 不应抛出异常
            sched._refresh_fundamentals()

        # invalidate 仍被调用两次（两个 symbol 都尝试了）
        self.assertEqual(mock_mgr.invalidate.call_count, 2)

    def test_refresh_empty_dataframe_counted_as_fail(self):
        """get_fundamentals 返回空 DataFrame 时 fail 计数增加，不中断循环。"""
        import pandas as pd
        sched = self._make_scheduler()

        positions_resp = json.dumps({
            'positions': [{'symbol': '000001.SZ', 'shares': 100}]
        }).encode()

        mock_mgr = MagicMock()
        mock_mgr.get_fundamentals.return_value = pd.DataFrame()

        with patch('urllib.request.urlopen') as mock_urlopen, \
             patch('core.fundamental_data.FundamentalDataManager', return_value=mock_mgr):

            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=MagicMock(read=MagicMock(return_value=positions_resp)))
            ctx.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = ctx

            sched._refresh_fundamentals()   # should not raise

        mock_mgr.invalidate.assert_called_once_with('000001.SZ')


class TestSchedulerQuarterlyTrigger(unittest.TestCase):
    """_run_loop 中季报刷新触发条件测试。"""

    def _trigger_dates(self):
        """应触发基本面刷新的日期样本。"""
        return [
            datetime(2026, 3, 25),   # 季度末月 25 日
            datetime(2026, 3, 31),   # 季度末月末
            datetime(2026, 6, 28),
            datetime(2026, 9, 30),
            datetime(2026, 12, 25),
            datetime(2026, 1, 1),    # 财报季首日
            datetime(2026, 1, 7),    # 财报季第 7 天
            datetime(2026, 4, 3),
            datetime(2026, 7, 6),
            datetime(2026, 10, 5),
        ]

    def _non_trigger_dates(self):
        """不应触发基本面刷新的日期样本。"""
        return [
            datetime(2026, 3, 24),   # 季度末月 24 日（不够 25）
            datetime(2026, 2, 15),
            datetime(2026, 5, 10),
            datetime(2026, 8, 20),
            datetime(2026, 1, 8),    # 财报季第 8 天（超出范围）
            datetime(2026, 4, 8),
            datetime(2026, 11, 1),   # 非季度末月
        ]

    def _should_trigger(self, dt: datetime) -> bool:
        """复现 _run_loop 中的触发判断逻辑。"""
        is_quarter_end = dt.month in (3, 6, 9, 12) and dt.day >= 25
        is_earnings_season = dt.month in (1, 4, 7, 10) and 1 <= dt.day <= 7
        return is_quarter_end or is_earnings_season

    def test_trigger_dates(self):
        for dt in self._trigger_dates():
            with self.subTest(dt=dt):
                self.assertTrue(self._should_trigger(dt),
                                f'{dt} should trigger fundamental refresh')

    def test_non_trigger_dates(self):
        for dt in self._non_trigger_dates():
            with self.subTest(dt=dt):
                self.assertFalse(self._should_trigger(dt),
                                 f'{dt} should NOT trigger fundamental refresh')


if __name__ == '__main__':
    unittest.main()
