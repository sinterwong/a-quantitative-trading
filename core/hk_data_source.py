"""
core/hk_data_source.py — 港股数据源

支持：
  - 港股实时行情（新浪 hkXXXXX）
  - 恒生指数（hkHSI）、恒生科技（hkHSTECH）
  - 历史分钟K线（新浪 hkXXXXX）
  - 港股窝轮（Warrant）/ 牛熊证（CBBC）标记

字段格式（新浪 19字段，实测于 2026-04-15）：
  [0]  stock_code
  [1]  chinese_name
  [2]  open       开盘
  [3]  prev_close 昨收
  [4]  high       最高
  [5]  low       最低
  [6]  last      当前/收盘
  [7]  change    涨跌额
  [8]  change_pct 涨跌幅(%)
  [9]  bid1_price  买1价
  [10] bid1_vol    买1量
  [11] volume      成交量
  [12] amount      成交额
  [13] 52w_high    52周最高
  [14] 52w_low     52周最低
  [15] mkt_cap_H   总市值（港元）
  [16] mkt_cap     流通市值
  [17] date
  [18] time

接入：与 Level2DataSource 的 OrderBook 兼容
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Dict, List, Optional, Any, Literal
import time
import threading
import os

import requests
import pandas as pd

from core.level2 import OrderBook


@dataclass
class HKStockSnapshot:
    """港股行情快照（兼容 A 股 OrderBook）"""
    timestamp: datetime
    symbol: str          # e.g. 'HK:00700'
    name: str

    # 价格
    last: float = 0       # 当前/收盘价
    open: float = 0
    high: float = 0
    low: float = 0
    prev_close: float = 0

    # 涨跌
    change: float = 0
    change_pct: float = 0

    # 成交
    volume: int = 0       # 成交量（股）
    amount: float = 0     # 成交额（港元）

    # 52周
    high_52w: float = 0
    low_52w: float = 0

    # 市值
    mkt_cap: float = 0

    # 盘口（5档，买1价/量）
    bid1_price: float = 0
    bid1_vol: int = 0

    def to_order_book(self) -> OrderBook:
        """转换为 OrderBook（兼容 Level2 因子）"""
        return OrderBook(
            timestamp=self.timestamp,
            symbol=self.symbol,
            bids=[(self.bid1_price, self.bid1_vol)],
            asks=[],
            last_price=self.last,
            volume=self.volume,
            amount=self.amount,
            change_pct=self.change_pct,
        )

    @property
    def pe_ttm(self) -> float:
        """简化 PE（如果有数据）"""
        return 0  # 新浪港股无 PE 字段


class HKStockDataSource:
    """
    新浪港股实时行情数据源。
    支持：港股正股、窝轮(Warrant)、牛熊证(CBBC)、指数、ETF。
    """

    name = 'HKStock'

    # 常用标的速查
    PRESET_SYMBOLS = {
        'hk00700':  '腾讯控股',
        'hk01810':  '小米集团-W',
        'hk09988':  '阿里巴巴',
        'hk03690':  '美团',
        'hk02020':  '理想汽车',
        'hk09888':  'Keep',
        'hkHSI':    '恒生指数',
        'hkHSTECH': '恒生科技',
        'hkHSAHP':  '恒生AH溢价',
    }

    def __init__(self, symbol: str = 'hk00700', interval: int = 5):
        self.symbol = symbol          # e.g. 'hk00700'
        self.interval = interval      # 轮询间隔（秒）
        self._cache: Optional[HKStockSnapshot] = None
        self._cache_time: float = 0
        self._cache_ttl: int = 3     # 3秒
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._handlers: List = []

    def fetch_latest(self) -> Optional[HKStockSnapshot]:
        """抓取最新行情"""
        now = time.time()
        if self._cache and (now - self._cache_time) < self._cache_ttl:
            return self._cache

        snap = self._fetch_sina(self.symbol)
        if snap:
            self._cache = snap
            self._cache_time = now
        return snap

    def _fetch_sina(self, sym: str) -> Optional[HKStockSnapshot]:
        """新浪港股实时行情"""
        try:
            url = f'https://hq.sinajs.cn/rn={int(time.time())}&list={sym}'
            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://finance.sina.com.cn',
            }
            resp = requests.get(url, headers=headers, timeout=8)
            text = resp.content.decode('gbk', errors='replace')
            return self._parse_sina(sym, text)
        except Exception as e:
            print(f"[HKStock] Sina fetch error for {sym}: {e}")
            return None

    def _parse_sina(self, sym: str, text: str) -> Optional[HKStockSnapshot]:
        """解析新浪港股行情"""
        try:
            content = text.split('"')[1] if '"' in text else ''
            if not content:
                return None
            fields = content.split(',')
            if len(fields) < 19:
                print(f"[HKStock] {sym}: only {len(fields)} fields, expected 19")
                return None

            name_en = fields[0].strip()
            name_cn = fields[1].strip()
            open_p = float(fields[2]) if fields[2] else 0
            prev_close = float(fields[3]) if fields[3] else 0
            high = float(fields[4]) if fields[4] else 0
            low = float(fields[5]) if fields[5] else 0
            last = float(fields[6]) if fields[6] else 0
            change = float(fields[7]) if fields[7] else 0
            change_pct = float(fields[8]) if fields[8] else 0
            bid1_price = float(fields[9]) if fields[9] else 0
            bid1_vol = int(float(fields[10])) if fields[10] else 0
            volume = int(float(fields[11])) if fields[11] else 0
            amount = float(fields[12]) if fields[12] else 0
            high_52w = float(fields[13]) if fields[13] else 0
            low_52w = float(fields[14]) if fields[14] else 0
            mkt_cap = float(fields[15]) if fields[15] else 0
            dt_str = f"{fields[17]} {fields[18]}" if len(fields) > 18 else ''

            try:
                ts = datetime.strptime(dt_str.strip(), '%Y/%m/%d %H:%M')
            except Exception:
                ts = datetime.now()

            # 标准 symbol 格式
            code = sym.replace('hk', '').upper()
            hk_symbol = f'HK:{code}'

            return HKStockSnapshot(
                timestamp=ts,
                symbol=hk_symbol,
                name=name_cn or name_en,
                last=last,
                open=open_p,
                high=high,
                low=low,
                prev_close=prev_close,
                change=change,
                change_pct=change_pct,
                volume=volume,
                amount=amount,
                high_52w=high_52w,
                low_52w=low_52w,
                mkt_cap=mkt_cap,
                bid1_price=bid1_price,
                bid1_vol=bid1_vol,
            )
        except Exception as e:
            print(f"[HKStock] Parse error for {sym}: {e}: {text[:200]}")
            return None

    def fetch_history(self, days: int = 5, freq: str = 'day') -> pd.DataFrame:
        """
        获取历史 K 线。
        freq: 'day' (日K) | '60'/'30'/'15'/'5'/'1' (分钟K)

        注意：新浪港股历史K线接口对部分标的返回 null，
        此时返回空 DataFrame（需用 A 股或专有数据源补充）。
        """
        code = self.symbol.replace('hk', '').upper()
        url = (
            f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php'
            f'/CN_MarketData.getKLineData'
            f'?symbol=hk{code.lower()}'
            f'&scale={freq}'
            f'&ma=no&datalen={days}'
        )
        try:
            headers = {'User-Agent': 'Mozilla/5.0'}
            resp = requests.get(url, headers=headers, timeout=10)
            raw = resp.text.strip()
            # 新浪对港股常返回 'null'（字符串）
            if raw == 'null' or raw == '' or raw.startswith('null'):
                print(f"[HKStock] Sina HK history unavailable for {self.symbol} (returned null)")
                return pd.DataFrame()
            data = resp.json()
            if not data or not isinstance(data, list):
                print(f"[HKStock] No history data for {self.symbol}, freq={freq}")
                return pd.DataFrame()

            rows = []
            for bar in data:
                if not isinstance(bar, dict):
                    continue
                rows.append({
                    'day': bar.get('day'),
                    'open': float(bar.get('open', 0)),
                    'high': float(bar.get('high', 0)),
                    'low': float(bar.get('low', 0)),
                    'close': float(bar.get('close', 0)),
                    'volume': float(bar.get('volume', 0)),
                })
            df = pd.DataFrame(rows)
            if not df.empty:
                df['day'] = pd.to_datetime(df['day'])
                df = df.set_index('day').sort_index()
            return df
        except Exception as e:
            print(f"[HKStock] fetch_history error for {self.symbol}: {e}")
            return pd.DataFrame()

    # ── 多标的批量获取 ─────────────────────────────────────────────────────

    @staticmethod
    def fetch_batch(symbols: List[str]) -> Dict[str, HKStockSnapshot]:
        """批量获取多个港股标的（一次请求）"""
        if not symbols:
            return {}
        # 新浪支持批量：hk00700,hk01810,hkHSI
        sym_list = ','.join(s.replace('HK:', 'hk').replace('hk', 'hk') for s in symbols)
        try:
            url = f'https://hq.sinajs.cn/rn={int(time.time())}&list={sym_list}'
            headers = {
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://finance.sina.com.cn',
            }
            resp = requests.get(url, headers=headers, timeout=10)
            text = resp.content.decode('gbk', errors='replace')

            results = {}
            # 每个标的一段 "var hq_str_hkXXXXX=..."
            import re
            pattern = r'var hq_str_(hk\d+)=\"([^\"]+)\"'
            for m in re.finditer(pattern, text):
                sym = m.group(1)
                content = m.group(2)
                ds = HKStockDataSource(sym)
                snap = ds._parse_sina(sym, f'var hq_str_{sym}="{content}"')
                if snap:
                    results[snap.symbol] = snap
            return results
        except Exception as e:
            print(f"[HKStock] fetch_batch error: {e}")
            return {}

    # ── 实时订阅 ──────────────────────────────────────────────────────────

    def subscribe(self, handler):
        """注册实时回调 handler(src, snapshot)"""
        self._handlers.append(handler)

    def start_polling(self, interval: int = None):
        """启动轮询"""
        interval = interval or self.interval
        self._running = True

        def loop():
            while self._running:
                snap = self.fetch_latest()
                if snap:
                    for h in self._handlers:
                        try:
                            h(self, snap)
                        except Exception as e:
                            print(f"[HKStock] handler error: {e}")
                time.sleep(interval)

        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()
        return self._thread

    def stop_polling(self):
        self._running = False

    def get_latest(self) -> Optional[HKStockSnapshot]:
        return self._cache

    # ── 便捷工厂 ─────────────────────────────────────────────────────────

    @classmethod
    def for_symbol(cls, symbol: str) -> 'HKStockDataSource':
        """从各种格式创建：'HK:00700' | 'hk00700' | '00700'"""
        s = symbol.strip().upper()
        s = s.replace('HK:', '').replace('HK', '')
        s = s.zfill(5)  # 补齐5位
        return cls(f'hk{s.lower()}')

    @classmethod
    def Tencent(cls) -> 'HKStockDataSource':
        return cls('hk00700')

    @classmethod
    def Xiaomi(cls) -> 'HKStockDataSource':
        return cls('hk01810')

    @classmethod
    def Alibaba(cls) -> 'HKStockDataSource':
        return cls('hk09988')

    @classmethod
    def HSI(cls) -> 'HKStockDataSource':
        return cls('hkHSI')

    @classmethod
    def HSTECH(cls) -> 'HKStockDataSource':
        return cls('hkHSTECH')
