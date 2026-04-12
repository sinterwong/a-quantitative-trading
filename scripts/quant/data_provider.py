"""
DataProvider 接口层
=======================
目标：让信号系统完全与数据源解耦

接口契约：
- get_kline(symbol, start, end) -> list[dict]  # OHLCV数据
- get_institutional(symbol, quarter) -> dict        # 机构持仓数据

两个实现：
1. HistoricalDataLoader：回测用（akshare → 本地缓存）
2. LiveDataLoader：实盘用（腾讯/新浪实时API）

信号系统完全不感知用的是哪个。
"""

import os
import sys
from abc import ABC, abstractmethod

THIS = os.path.abspath(__file__)
QUANT_DIR = os.path.dirname(THIS)
sys.path.insert(0, QUANT_DIR)

from data_loader import DataLoader as OriginalDataLoader


# ============================================================
# 接口定义
# ============================================================

class DataProvider(ABC):
    """
    数据源抽象接口

    所有数据访问必须通过这个接口，
    Engine/Signal系统完全不感知底层用的是哪个实现
    """

    @abstractmethod
    def get_kline(self, symbol, start, end) -> list:
        """
        获取K线数据

        Returns:
            list[dict], 每个dict包含:
            {
                'date': str,       # YYYY-MM-DD
                'open': float,
                'high': float,
                'low': float,
                'close': float,
                'volume': int,
                'turnover': float  # 可选
            }
        """
        pass

    @abstractmethod
    def get_institutional(self, symbol, quarter) -> dict:
        """
        获取机构持仓数据

        Returns:
            dict: {
                'total_score': float,
                'signal': 'buy'/'sell'/'hold',
                'avg_fund_count': int,
                'avg_hold_ratio': float,
                'top_stocks': list
            }
        """
        pass


# ============================================================
# 实现1：历史数据（回测用）
# ============================================================

class HistoricalDataProvider(DataProvider):
    """
    历史数据Provider

    使用已有的data_loader加载历史K线数据
    用于回测和历史信号分析
    """

    def __init__(self, cache_dir=None):
        self.loader = OriginalDataLoader(cache_dir=cache_dir)

    def get_kline(self, symbol, start, end) -> list:
        data = self.loader.get_kline(symbol, start, end)
        if not data:
            return []
        # 标准化字段
        result = []
        for d in data:
            result.append({
                'date': d.get('date', ''),
                'open': float(d.get('open', 0)),
                'high': float(d.get('high', 0)),
                'low': float(d.get('low', 0)),
                'close': float(d.get('close', 0)),
                'volume': int(d.get('volume', 0)),
                'turnover': float(d.get('turnover', 0)),
            })
        return result

    def get_institutional(self, symbol, quarter) -> dict:
        import institutional_live as inst_live
        return inst_live.get_etf_institutional_score(symbol, quarter)

    def get_vwap(self, symbol, date_str: str) -> float:
        """
        Get actual VWAP for a given date: turnover / volume.
        Returns None if data unavailable.
        """
        klines = self.get_kline(symbol, date_str, date_str)
        if not klines:
            return None
        k = klines[0]
        turnover = float(k.get('turnover', 0) or 0)
        volume = float(k.get('volume', 0) or 0)
        if volume == 0 or turnover == 0:
            # Sina historical data has no turnover; Eastmoney live API has it
            return None
        return turnover / volume



# ============================================================
# 实现2：实时数据（实盘用）
# ============================================================

class LiveDataProvider(DataProvider):
    """
    实时数据Provider

    从腾讯/新浪财经获取实时行情
    用于每日实盘信号计算

    注意：实时数据只包含当日快照，
    历史数据仍需要从HistoricalDataProvider获取
    """

    def __init__(self):
        self._today_cache = {}  # symbol -> today's kline
        self._hist_provider = HistoricalDataProvider()

    def get_kline(self, symbol, start, end) -> list:
        """
        实时+历史混合K线

        - start~=today之前：用历史数据
        - =today：用实时快照
        """
        from datetime import date, timedelta

        today_str = date.today().strftime('%Y-%m-%d')
        yesterday_str = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')

        # 历史部分
        if start < today_str:
            return self._hist_provider.get_kline(symbol, start, end)

        # 当日实时数据
        if start <= today_str <= end:
            hist = self._hist_provider.get_kline(symbol, start, today_str)
            # 追加今日实时数据
            live_today = self._fetch_today_realtime(symbol)
            if live_today:
                # 如果历史已经有今天的数据（收盘后），用历史的
                if not any(d['date'] == today_str for d in hist):
                    hist.append(live_today)
            return hist

        return self._hist_provider.get_kline(symbol, start, end)

    def get_institutional(self, symbol, quarter) -> dict:
        """机构数据用历史的（季度更新，实时性足够）"""
        return self._hist_provider.get_institutional(symbol, quarter)

    def get_vwap(self, symbol, date_str: str) -> float:
        """
        Get today's real-time VWAP from Eastmoney push2 API.
        Fields: f47=volume, f48=turnover. VWAP = f48/f47
        Falls back to HistoricalDataProvider.get_vwap() if API fails.
        """
        import urllib.request, ssl, json
        sym_map = {'SH': '1.', 'SZ': '0.'}
        prefix = sym_map.get(symbol[-2:], '1.')
        code = symbol[:6]
        em_url = (
            f'https://push2.eastmoney.com/api/qt/stock/get'
            f'?ut=fa5fd1943c7b386f172d6893dbfba10b'
            f'&fields=f43,f44,f45,f46,f47,f48'
            f'&secid={prefix}{code}'
        )
        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            req = urllib.request.Request(
                em_url,
                headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://quote.eastmoney.com/'}
            )
            with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
                data = json.loads(resp.read())
                fields = data.get('data', {})
                volume = float(fields.get('f47', 0) or 0)
                turnover = float(fields.get('f48', 0) or 0)
                if volume > 0:
                    return turnover / volume
        except Exception:
            pass
        return self._hist_provider.get_vwap(symbol, date_str)


    def _fetch_today_realtime(self, symbol) -> dict:
        """
        获取今日实时行情

        使用腾讯财经实时API
        返回当日截至当前的OHLCV快照
        """
        import urllib.request
        import json

        # 腾讯实时行情接口
        # hq.gtimg.cn/pulse?symbol=sz300750&ext=&r=timestamp&pos=1-3-1
        # 实际应该用: https://qt.gtimg.cn/q=s_sz300750
        try:
            url = f'https://qt.gtimg.cn/q={symbol.lower().replace(".sh","sh").replace(".sz","sz")}'
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = resp.read().decode('gbk', errors='replace')
        except Exception:
            # 备选：使用新浪
            try:
                sym_map = {'SH': 'sh', 'SZ': 'sz'}
                prefix = sym_map.get(symbol[-2:], 'sh')
                code = symbol[:6]
                url = f'https://hq.sinajs.cn/list={prefix}{code}'
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    raw = resp.read().decode('gbk', errors='replace')
            except Exception:
                return None

        try:
            # 解析腾讯数据格式
            # var qt_sz300750={...}
            data_str = raw.split('="')[1].split('"')[0] if '="' in raw else raw
            fields = data_str.split('~')

            if len(fields) < 50:
                return None

            from datetime import date
            today_str = date.today().strftime('%Y-%m-%d')

            # 腾讯字段: 0=名称, 1=代码, 3=当前价, 4=昨收, 5=今开,
            #          6=成交量(手), 7=外盘, 8=内盘, 9=涨跌额, 10=涨跌%
            #          33=最高, 34=最低, 36=成交额
            open_price = float(fields[5]) if fields[5] else 0
            close_price = float(fields[3]) if fields[3] else 0  # 当前价=收盘价（盘中）
            prev_close = float(fields[4]) if fields[4] else 0
            high_price = float(fields[33]) if fields[33] else 0
            low_price = float(fields[34]) if fields[34] else 0
            volume = int(fields[6]) if fields[6] else 0
            turnover = float(fields[36]) if fields[36] else 0

            # 如果当前价=0说明停牌
            if close_price == 0:
                return None

            return {
                'date': today_str,
                'open': open_price,
                'high': high_price,
                'low': low_price,
                'close': close_price,
                'volume': volume,
                'turnover': turnover,
            }
        except Exception:
            return None


# ============================================================
# 使用示例
# ============================================================

if __name__ == '__main__':
    print("=" * 50)
    print("DataProvider Interface Test")
    print("=" * 50)

    # 历史Provider（回测用）
    print("\n[HistoricalDataProvider]")
    hist = HistoricalDataProvider()
    data = hist.get_kline('600276.SH', '20260101', '20260110')
    print(f"  600276.SH 2026-01: {len(data)} records")
    if data:
        print(f"  First: {data[0]}")

    inst = hist.get_institutional('600276.SH', '20243')
    print(f"  Institutional: score={inst.get('total_score', 0):.1f}, signal={inst.get('signal')}")

    # 实时Provider（实盘用）
    print("\n[LiveDataProvider]")
    live = LiveDataProvider()
    print("  Fetching today's realtime data...")
    # 注意：盘中才能获取实时数据，收盘后腾讯数据即为当日收盘价
