"""
AkShare 数据获取模块 v2
优先使用稳定的接口:
1. fund_etf_hist_em - ETF历史K线（稳定）
2. stock_zh_a_daily (sina) - A股历史K线（稳定）
3. 腾讯/新浪财经接口作为备用
"""

import os
import sys
import json
from datetime import datetime, timedelta

# 禁用代理
for key in list(os.environ.keys()):
    if 'proxy' in key.lower():
        del os.environ[key]

import urllib.request
import ssl
import warnings
warnings.filterwarnings('ignore')

try:
    import akshare as ak
    import pandas as pd
    AKSHARE_AVAILABLE = True
except ImportError:
    AKSHARE_AVAILABLE = False
    print("[WARN] AkShare not available")


class DataLoader:
    """数据加载器 v2 - 使用稳定的接口"""

    def __init__(self, cache_dir=None):
        self.cache_dir = cache_dir or os.path.join(os.path.dirname(__file__), 'cache')
        os.makedirs(self.cache_dir, exist_ok=True)

    def _get_cache(self, key: str):
        cache_file = os.path.join(self.cache_dir, f"{key}.json")
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return None

    def _save_cache(self, key: str, data):
        cache_file = os.path.join(self.cache_dir, f"{key}.json")
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, default=str)

    def _is_hk_stock(self, symbol: str) -> bool:
        """判断是否为港股
        支持格式: '01810.HK' / 'hk01810' / '01810' (纯数字，代码在 AkShare 港股范围内)
        """
        s = symbol.upper()
        if s.endswith('.HK'):
            return True
        if s.startswith('HK'):
            return True
        # 纯数字但明确是港股范围 (6位数以下，A股是6位，港股也是)
        pure = s.replace('.HK', '').replace('.SH', '').replace('.SZ', '')
        if pure.isdigit() and len(pure) == 5 and not pure.startswith(('1','5','8')):
            # 港股代码特征: 01810, 00700, 09988 等
            return True
        return False

    def _get_hk_hist(self, symbol: str, start_date: str, end_date: str) -> list:
        """
        使用 AkShare stock_hk_daily 获取港股历史K线
        数据源: Eastmoney（非push2，受proxy影响小）
        支持: 港股所有股票（01810/00700/09988等）
        """
        if not AKSHARE_AVAILABLE:
            return []
        # 提取纯数字代码: '01810.HK' -> '01810', 'hk00700' -> '00700'
        # 注意：AkShare 港股代码必须保留前导零，如 '01810' 不是 '1810'
        code = symbol.upper().replace('.HK', '').replace('HK', '')
        # 保留前导零，如 '01810'
        if not code:  # 如果变成空字符串（处理 'HK' 这种）
            code = symbol.upper().replace('HK', '').replace('.HK', '').replace('.SH', '').replace('.SZ', '')
        cache_key = f"hk_daily_{code}_{start_date}_{end_date}"
        cached = self._get_cache(cache_key)
        if cached:
            print(f"[CACHE HK] {code}: {len(cached)} records")
            return cached
        try:
            df = ak.stock_hk_daily(symbol=code)
            if df is None or df.empty:
                return []
            # 已经是英文列名: date, open, high, low, close, volume
            # 过滤日期范围
            df['date'] = pd.to_datetime(df['date'])
            start_dt = pd.to_datetime(start_date[:4] + '-' + start_date[4:6] + '-' + start_date[6:8])
            end_dt   = pd.to_datetime(end_date[:4] + '-' + end_date[4:6] + '-' + end_date[6:8])
            df = df[(df['date'] >= start_dt) & (df['date'] <= end_dt)].copy()
            df = df.sort_values('date').reset_index(drop=True)
            data = []
            for _, row in df.iterrows():
                data.append({
                    'date':   str(row['date'])[:10],
                    'open':   float(row['open']),
                    'close':  float(row['close']),
                    'high':   float(row['high']),
                    'low':    float(row['low']),
                    'volume': float(row['volume'])
                })
            self._save_cache(cache_key, data)
            print(f"[OK] HK {code}: {len(data)} records via stock_hk_daily ({start_date} ~ {end_date})")
            return data
        except Exception as e:
            print(f"[WARN] stock_hk_daily failed for {code}: {e}")
            return []

    def _get_akshare_a_stock(self, symbol: str, start_date: str, end_date: str) -> list:
        """
        使用 AkShare stock_zh_a_daily 获取 A 股完整历史数据。
        AkShare 可以获取到股票上市日的完整历史，而 Sina 只返回最近 6000 条。
        """
        if not AKSHARE_AVAILABLE:
            return []

        # 转换代码格式: '600900.SH' -> 'sh600900', '000001.SZ' -> 'sz000001'
        pure = symbol.replace('.SH', '').replace('.SZ', '')
        if not pure.isdigit() or len(pure) != 6:
            return []
        exchange = 'sh' if symbol.endswith('.SH') else 'sz'
        ak_code = exchange + pure

        cache_key = f'akshare_a_{ak_code}_{start_date}_{end_date}'
        cached = self._get_cache(cache_key)
        if cached:
            print(f'[CACHE A] {symbol}: {len(cached)} records')
            return cached

        try:
            # stock_zh_a_daily 返回全量历史（不受 datalen 限制）
            df = ak.stock_zh_a_daily(symbol=ak_code, adjust='qfq')
            if df is None or df.empty:
                return []

            # AkShare 返回中文列名: 日期, 开盘, 收盘, 最高, 最低, 成交量, 成交额, 振幅, 涨跌幅, 涨跌额, 换手率
            # 重命名为英文列名
            df = df.rename(columns={
                '日期': 'date', '开盘': 'open', '收盘': 'close',
                '最高': 'high', '最低': 'low', '成交量': 'volume',
            })

            # 日期过滤
            df['date'] = pd.to_datetime(df['date'])
            start_dt = pd.to_datetime(start_date[:4] + '-' + start_date[4:6] + '-' + start_date[6:8])
            end_dt = pd.to_datetime(end_date[:4] + '-' + end_date[4:6] + '-' + end_date[6:8])
            df = df[(df['date'] >= start_dt) & (df['date'] <= end_dt)].copy()
            df = df.sort_values('date').reset_index(drop=True)

            data = []
            for _, row in df.iterrows():
                data.append({
                    'date': str(row['date'])[:10],
                    'open': float(row['open']),
                    'close': float(row['close']),
                    'high': float(row['high']),
                    'low': float(row['low']),
                    'volume': float(row['volume']),
                })

            if data:
                self._save_cache(cache_key, data)
                print(f'[OK] A股 {symbol}: {len(data)} records via AkShare ({data[0]["date"]} ~ {data[-1]["date"]})')
            return data

        except Exception as e:
            print(f'[WARN] AkShare A-stock failed for {symbol}: {e}')
            return []

    def get_kline(self, symbol: str, start_date: str, end_date: str, adjust: str = 'qfq') -> list:
        """
        获取K线数据 - 优先使用稳定接口

        Args:
            symbol: 代码格式支持:
                - ETF: '159992.SZ', '512690.SH', '510300.SH' 等
                - A股: '600900.SH', '000001.SZ' 等
                - 港股: '01810.HK', 'hk01810', '00700.HK' 等
            start_date: 'YYYYMMDD'
            end_date: 'YYYYMMDD'

        Returns:
            list of dicts: [{date, open, high, low, close, volume}, ...]
        """
        # 港股（AkShare stock_hk_hist）
        if self._is_hk_stock(symbol):
            data = self._get_hk_hist(symbol, start_date, end_date)
            if data:
                return data

        # 尝试ETF接口
        if self._is_etf(symbol):
            data = self._get_etf_hist(symbol, start_date, end_date)
            if data:
                return data

        # A股：用 AkShare 获取完整历史数据（优先于 Sina/QT）
        if not self._is_hk_stock(symbol) and not self._is_etf(symbol):
            data = self._get_akshare_a_stock(symbol, start_date, end_date)
            if data:
                return data

        # 尝试新浪财经接口（备用）
        data = self._get_sina_kline(symbol, start_date, end_date)
        if data:
            return data

        # 尝试腾讯接口
        data = self._get_qt_kline(symbol, start_date, end_date)
        if data:
            return data

        print(f"[ERROR] All sources failed for {symbol}")
        return []

    def _is_etf(self, symbol: str) -> bool:
        """判断是否为ETF"""
        # ETF代码特征: 159xxx, 512xxx, 510xxx, 588xxx等
        pure = symbol.replace('.SH', '').replace('.SZ', '')
        if pure.isdigit():
            prefix = pure[:3]
            if prefix in ['159', '512', '510', '588', '563', '561']:
                return True
        return False

    def _get_etf_hist(self, symbol: str, start_date: str, end_date: str) -> list:
        """使用fund_etf_hist_em获取ETF历史数据"""
        if not AKSHARE_AVAILABLE:
            return []

        pure = symbol.replace('.SH', '').replace('.SZ', '')
        cache_key = f"etf_hist_{pure}_{start_date}_{end_date}"

        cached = self._get_cache(cache_key)
        if cached:
            print(f"[CACHE] {symbol}")
            return cached

        try:
            df = ak.fund_etf_hist_em(
                symbol=pure,
                period='daily',
                start_date=start_date,
                end_date=end_date
            )

            if df is None or df.empty:
                return []

            # 标准化列名
            df = df.rename(columns={
                '日期': 'date', '开盘': 'open', '收盘': 'close',
                '最高': 'high', '最低': 'low', '成交量': 'volume',
                '成交额': 'amount', '涨跌幅': 'pct_change', '涨跌额': 'change'
            })

            data = []
            for _, row in df.iterrows():
                data.append({
                    'date': str(row['date'])[:19],
                    'open': float(row['open']),
                    'close': float(row['close']),
                    'high': float(row['high']),
                    'low': float(row['low']),
                    'volume': float(row['volume'])
                })

            if data:
                self._save_cache(cache_key, data)
                print(f"[OK] ETF {symbol}: {len(data)} records via fund_etf_hist_em")
            return data

        except Exception as e:
            print(f"[WARN] fund_etf_hist_em failed for {symbol}: {e}")
            return []

    def _get_sina_kline(self, symbol: str, start_date: str, end_date: str) -> list:
        """使用新浪财经接口获取K线"""
        # 转换代码格式
        if '.SH' in symbol:
            code = 'sh' + symbol.replace('.SH', '')
        else:
            code = 'sz' + symbol.replace('.SZ', '')

        cache_key = f"sina_{code}_{start_date}_{end_date}"
        cached = self._get_cache(cache_key)
        if cached:
            print(f"[CACHE] {symbol}")
            return cached

        try:
            url = f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=no&datalen=6000'

            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/6.0')
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            with urllib.request.urlopen(req, timeout=30, context=ctx) as response:
                content = response.read().decode('utf-8')

            import json as json_mod
            data_list = json_mod.loads(content)

            if not data_list:
                return []

            data = []
            for item in data_list:
                try:
                    data.append({
                        'date': item['day'],
                        'open': float(item['open']),
                        'close': float(item['close']),
                        'high': float(item['high']),
                        'low': float(item['low']),
                        'volume': float(item['volume'])
                    })
                except:
                    continue

            if data:
                # Client-side date filter (Sina doesn't support server-side date range)
                if start_date:
                    data = [d for d in data if d['date'] >= start_date]
                if end_date:
                    data = [d for d in data if d['date'] <= end_date]
                self._save_cache(cache_key, data)
                print(f"[OK] {symbol}: {len(data)} records via sina [{start_date}~{end_date}]")

            return data

        except Exception as e:
            print(f"[WARN] Sina kline failed for {symbol}: {e}")
            return []

    def _get_qt_kline(self, symbol: str, start_date: str, end_date: str) -> list:
        """腾讯财经备用接口"""
        if '.SH' in symbol:
            code = 'sh' + symbol.replace('.SH', '')
        else:
            code = 'sz' + symbol.replace('.SZ', '')

        cache_key = f"qt_{code}_{start_date}_{end_date}"
        cached = self._get_cache(cache_key)
        if cached:
            print(f"[CACHE] {symbol}")
            return cached

        try:
            url = f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?_var=kline_dayqfq&param={code},day,{start_date},{end_date},320,qfq&r=0.1'

            req = urllib.request.Request(url)
            req.add_header('User-Agent', 'Mozilla/6.0')
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            with urllib.request.urlopen(req, timeout=30, context=ctx) as response:
                content = response.read().decode('utf-8')

            json_str = content.split('=', 1)[1] if '=' in content else content
            import json as json_mod
            data = json_mod.loads(json_str)

            qt_data = data.get('data', {}).get(code, {}).get('qfqday', [])
            if not qt_data:
                qt_data = data.get('data', {}).get(code, {}).get('day', [])

            records = []
            for item in qt_data:
                if len(item) >= 6:
                    records.append({
                        'date': item[0],
                        'open': float(item[1]),
                        'close': float(item[2]),
                        'high': float(item[3]),
                        'low': float(item[4]),
                        'volume': float(item[5]) if item[5] != '-' else 0
                    })

            if records:
                self._save_cache(cache_key, records)
                print(f"[OK] {symbol}: {len(records)} records via Qt")

            return records

        except Exception as e:
            print(f"[WARN] Qt kline failed for {symbol}: {e}")
            return []
