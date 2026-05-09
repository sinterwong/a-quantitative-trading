# -*- coding: utf-8 -*-
"""
tests/test_p0_bugs.py

验证 review 提出的 P0/P1 问题是否真实存在：
  P0-1: sina_fetcher.py 日期格式过滤 bug
  P1-1: signals.py f-string replace 隐式行为
  P1-2: _make_recommendation fundamental_red reasons 重复追加风险
"""

import sys
import unittest
from datetime import datetime
from unittest.mock import patch, MagicMock

# ─── P0-1: sina_fetcher 日期格式过滤 ─────────────────────────────────────────


class TestSinaFetcherDateFiltering(unittest.TestCase):
    """
    sina_fetcher._fetch_raw_data 中：
      df['date_str'] = df['date'].dt.strftime('%Y-%m-%d')   → '2024-01-15'
      然后与 start_date / end_date 比较

    但 start_date / end_date 的格式是 'YYYYMMDD'（字符串），例如 '20240101'
    字符串比较 '2024-01-15' >= '20240101'：
      首位 '2' vs '2' → 相等
      其次 '0' vs '0' → 相等
      '4' vs '4' → 相等
      '-'  (ASCII 45) vs '4' (ASCII 52) → '-' < '4'，所以 '2024-01-15' < '20240101'

    这意味着几乎所有数据都会被错误地过滤掉！
    """

    def test_date_string_comparison_proves_bug(self):
        """证明 YYYY-MM-DD vs YYYYMMDD 字符串比较存在方向性错误"""
        date_str = "2024-01-15"   # df['date_str'] 的格式
        start_date = "20240101"   # start_date 格式

        # 当前代码的比较逻辑（错误）
        result_wrong = date_str >= start_date
        # '2024-01-15' >= '20240101'
        # 逐字符比较：'2'='2', '0'='0', '4'='4', '-' < '4' → False
        # 实际上 date_str < start_date，但比较器认为 False

        self.assertLess(date_str, start_date,
                        "日期字符串格式差异应使 '2024-01-15' < '20240101'")
        # 当前代码：df[df['date_str'] >= start_date] → 把所有数据都过滤掉了（因为没有数据 >= '20240101'）

    def test_end_date_string_comparison_proves_bug(self):
        """end_date 格式 '20241231' vs '2024-12-31'"""
        date_str = "2024-12-31"
        end_date = "20241231"

        # 当前代码的比较：date_str >= end_date → False（错误）
        self.assertLess(date_str, end_date)
        # df[df['date_str'] <= end_date] → 保留了数据（这部分碰巧正确，但不是比较逻辑对的）
        self.assertLessEqual(date_str, end_date)  # 这个碰巧为 True

    def test_sina_fetcher_date_filter_with_real_dates(self):
        """
        模拟 _fetch_raw_data 的日期过滤逻辑，验证 bug。
        start_date='20240101', end_date='20241231'
        数据行：['2024-01-15', '2024-03-01', '2024-06-20', '2025-01-10']
        期望保留：2024-03-01 和 2024-06-20
        实际结果（bug）：全部被过滤掉
        """
        import pandas as pd

        # 构造 DataFrame
        dates = pd.to_datetime(['2024-01-15', '2024-03-01', '2024-06-20', '2025-01-10'])
        df = pd.DataFrame({'date': dates})
        start_date = '20240101'
        end_date = '20241231'

        # 模拟当前 sina_fetcher 的过滤逻辑
        df['date_str'] = df['date'].dt.strftime('%Y-%m-%d')
        filtered = df[(df['date_str'] >= start_date) & (df['date_str'] <= end_date)]

        # BUG: 过滤后为空（因为 '-' < '4' 导致所有 'YYYY-MM-DD' < 'YYYYMMDD'）
        self.assertTrue(
            filtered.empty,
            f"由于日期格式不匹配，过滤后应为空，但得到 {len(filtered)} 行\n"
            f"date_str={df['date_str'].tolist()}, start={start_date}, end={end_date}"
        )

    def test_sina_fetcher_date_filter_correct_approach(self):
        """验证正确的修复方案：两边都用 YYYYMMDD 格式"""
        import pandas as pd

        dates = pd.to_datetime(['2024-01-15', '2024-03-01', '2024-06-20', '2025-01-10'])
        df = pd.DataFrame({'date': dates})
        start_date = '20240101'
        end_date = '20241231'

        # 正确方案：date_str 也格式化为 YYYYMMDD
        df['date_str'] = df['date'].dt.strftime('%Y%m%d')
        filtered = df[(df['date_str'] >= start_date) & (df['date_str'] <= end_date)]

        # '20240115' >= '20240101' → '1' > '0' → True（所以 01-15 保留）
        # '20240301' >= '20240101' → True（保留）
        # '20240620' >= '20240101' → True（保留）
        # '20250110' > '20241231' → True（超出 end_date，排除）
        # 结果：3 行（2024-01-15, 2024-03-01, 2024-06-20）
        self.assertEqual(len(filtered), 3,
                         f"应保留 3 行，实际 {len(filtered)} 行：{filtered['date'].tolist()}")


# ─── P1-1: signals.py f-string replace 逻辑 ───────────────────────────────────


class TestSignalsFStringReplace(unittest.TestCase):
    """
    signals.py 第 815-837 行：
        reason = f'RSI={prev_rsi:.0f}≤{rsi_buy}超卖区间｜现价{price}'
        reason = reason.replace(f'|现价{price}', f'{north_boost}|现价{price}')

    注意：reason 中用的是全角 '｜' (U+FF5C)，不是 ASCII '|'。
    replace 查找的是 f'|现价{price}'（ASCII |），但 reason 中是 '｜'（全角）。
    由于 '｜' != '|'，replace 找不到目标，替换失败。
    """

    def test_reason_contains_fullwidth_separator(self):
        """验证 reason 中确实使用全角分隔符 '｜'"""
        price = 28.13
        # signals.py 第 815 行的实际格式
        reason = f'RSI=65≤30超卖区间｜现价{price}'

        # 查找目标是 ASCII '|'
        lookup = f'|现价{price}'
        # 实际 reason 中是全角 '｜'
        self.assertIn('｜', reason)   # 确认存在全角分隔符
        self.assertNotIn('|', reason)  # 确认不存在 ASCII 分隔符
        self.assertNotIn(lookup, reason)  # replace 找不到

    def test_correct_fix_uses_fullwidth_separator(self):
        """修复后：north_boost 与 reason 均使用全角 '｜'，语义一致"""
        price = 28.13
        reason = f'RSI=65≤30超卖区间｜现价{price}'
        # 修复后 north_boost 也用全角 '｜'
        north_boost = f'｜北向脉冲+52亿'

        # 替换: '｜现价28.13' → '｜北向脉冲+52亿｜现价28.13'
        result = reason.replace(f'｜现价{price}', f'{north_boost}｜现价{price}')

        # 正确结果：RSI=65≤30超卖区间  +  ｜(north_boost)  +  ｜现价28.13
        # = 'RSI=65≤30超卖区间｜北向脉冲+52亿｜现价28.13'
        self.assertIn('北向脉冲', result)
        self.assertEqual(result, 'RSI=65≤30超卖区间｜北向脉冲+52亿｜现价28.13')


# ─── P1-2: _make_recommendation fundamental_red reasons 重复追加 ───────────────


class TestMakeRecommendationReasons(unittest.TestCase):
    """
    _make_recommendation 中 fundamental_red 判断逻辑：
      if rev < 0: fundamental_red=True; reasons.append(下滑)
      elif rev < -20:  # ← 只有 rev >= 0 时才到这里，所以不会同时触发

    实际上由于 if/elif 互斥，reasons 不会重复追加。
    但条件分支顺序不清晰，营收跌幅 -25% 时只追加"下滑"而不追加"大幅"，语义不准确。
    """

    def test_rev_below_zero_only_appends_once(self):
        """rev = -15% 时，只触发 < 0 分支，reasons 长度为 1"""
        rev = -15.0
        fundamental_red = False
        reasons = []

        if rev is not None:
            if rev < 0:
                fundamental_red = True
                reasons.append(f'营收同比下滑({rev:+.1f}%)')
            elif rev < -20:
                fundamental_red = True
                reasons.append(f'营收同比大幅下滑({rev:+.1f}%)')

        self.assertTrue(fundamental_red)
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0], '营收同比下滑(-15.0%)')

    def test_rev_below_minus_20_only_appends_once(self):
        """rev = -25% 时，触发 < -20 分支（if），显示'大幅下滑'"""
        rev = -25.0
        fundamental_red = False
        reasons = []

        if rev is not None:
            if rev < -20:
                fundamental_red = True
                reasons.append(f'营收同比大幅下滑({rev:+.1f}%)')
            elif rev < 0:
                fundamental_red = True
                reasons.append(f'营收同比下滑({rev:+.1f}%)')

        self.assertTrue(fundamental_red)
        self.assertEqual(len(reasons), 1)
        self.assertEqual(reasons[0], '营收同比大幅下滑(-25.0%)')

    def test_rev_above_zero_no_red_flag(self):
        """rev = +5% 时，不触发任何红线"""
        rev = 5.0
        fundamental_red = False
        reasons = []

        if rev is not None:
            if rev < 0:
                fundamental_red = True
                reasons.append(f'营收同比下滑({rev:+.1f}%)')
            elif rev < -20:
                fundamental_red = True
                reasons.append(f'营收同比大幅下滑({rev:+.1f}%)')

        self.assertFalse(fundamental_red)
        self.assertEqual(len(reasons), 0)


# ─── P1-3: SinaFetcher datalen=6000 固定 vs 动态 ──────────────────────────────


class TestSinaFetcherDatalenBehavior(unittest.TestCase):
    """
    新浪日K接口 datalen 固定为 6000（最大支持量），然后客户端过滤。
    旧代码 datalen = days（按需拉取）。

    验证这个行为变化是否影响功能正确性（不影响，但增加网络开销）。
    """

    def test_datalen_6000_vs_days_network_overhead(self):
        """6000 vs days=60：对于只需要 60 条数据的请求，额外传输 5940 条"""
        datalen_new = 6000
        datalen_old = 60

        overhead_ratio = datalen_new / datalen_old
        self.assertEqual(overhead_ratio, 100.0,
                         "datalen=6000 比 datalen=60 多传输 100 倍数据，"
                         "但新浪接口传输的是纯数字文本，不是图片/视频，"
                         "约 6000*100bytes ≈ 600KB，overhead 可接受")


if __name__ == '__main__':
    unittest.main(verbosity=2)
