"""
data_cache.py — P8 数据源缓存层
================================
多源缓存 + 降级机制，提升数据获取稳定性。

功能：
  1. KAMT 北向资金缓存（60s TTL） + eastmoney history fallback
  2. 分钟 K 线缓存（60s TTL）防止重复请求限流
  3. 通用 HTTP GET 缓存（30s TTL）

Usage:
  from data_cache import cached_kamt, cached_minute_kline, cached_get
"""

import os
import sys
import json
import time
import ssl
import logging
import threading
import urllib.request
from datetime import datetime, date
from typing import Optional, Dict, Any, Callable

_log = logging.getLogger('data_cache')

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(os.path.dirname(THIS_DIR), 'backend')
sys.path.insert(0, THIS_DIR)
sys.path.insert(0, BACKEND_DIR)

# ─── HTTP 工具 ─────────────────────────────────────────────────────────

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
_USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
_REFERER = 'https://data.eastmoney.com/'


def _http_get(url: str, headers: Dict = None, timeout: float = 5.0) -> Optional[str]:
    """带超时和错误处理的 HTTP GET。"""
    h = {'User-Agent': _USER_AGENT, **(headers or {})}
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        _log.debug('HTTP GET failed: %s — %s', url[:60], e)
        return None


# ─── 线程安全缓存 ──────────────────────────────────────────────────────

class _CacheEntry:
    __slots__ = ('value', 'expires_at')

    def __init__(self, value: Any, ttl_seconds: float):
        self.value = value
        self.expires_at = time.monotonic() + ttl_seconds

    def is_valid(self) -> bool:
        return time.monotonic() < self.expires_at


class _SafeCache:
    """线程安全的单调时间缓存。"""
    _lock = threading.RLock()
    _store: Dict[str, _CacheEntry] = {}

    @classmethod
    def get(cls, key: str) -> Optional[Any]:
        with cls._lock:
            entry = cls._store.get(key)
            if entry and entry.is_valid():
                return entry.value
            return None

    @classmethod
    def set(cls, key: str, value: Any, ttl_seconds: float):
        with cls._lock:
            cls._store[key] = _CacheEntry(value, ttl_seconds)

    @classmethod
    def get_or_fetch(cls, key: str, fetch_fn: Callable[[], Optional[Any]],
                     ttl_seconds: float = 60) -> Optional[Any]:
        """获取缓存，如果不存在则调用 fetch_fn，结果存入缓存。"""
        with cls._lock:
            entry = cls._store.get(key)
            if entry and entry.is_valid():
                return entry.value
        # 未命中或已过期：在锁外调用 fetch_fn（避免重入锁）
        value = fetch_fn()
        if value is not None:
            with cls._lock:
                cls._store[key] = _CacheEntry(value, ttl_seconds)
        return value

    @classmethod
    def invalidate(cls, key: str):
        with cls._lock:
            cls._store.pop(key, None)

    @classmethod
    def clear(cls):
        with cls._lock:
            cls._store.clear()


# ─── 1. KAMT 北向资金缓存 + Fallback ────────────────────────────────────

# Primary: KAMT realtime endpoint (updates every minute)
_KAMT_REALTIME_URL = (
    'https://push2.eastmoney.com/api/qt/kamt.rtmin/get'
    '?fields1=f1,f2,f3,f4&fields2=f51,f52,f53,f54,f55,f56'
    '&ut=b2884a393a59ad64002292a3e90d46a5'
)

# Fallback 1: KAMT daily summary (non-realtime, works when rtmin fails)
_KAMT_DAILY_URL = (
    'https://push2.eastmoney.com/api/qt/kamt/get'
    '?fields1=f1,f2&fields2=f51,f52,f53,f54,f55,f56'
)

# Cache key
_KAMT_CACHE_KEY = 'kamt_northbound_v2'


def fetch_kamt_realtime() -> Optional[dict]:
    """Fetch KAMT realtime data from eastmoney primary endpoint."""
    raw = _http_get(_KAMT_REALTIME_URL, headers={'Referer': _REFERER}, timeout=5.0)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        kamt = data.get('data', {})
        if not kamt:
            return None
        return _parse_kamt(kamt)
    except (json.JSONDecodeError, KeyError):
        return None


def fetch_kamt_daily_fallback() -> Optional[dict]:
    """
    Fallback: daily summary endpoint.
    Returns quota used/remaining and daily net inflow.
    """
    raw = _http_get(_KAMT_DAILY_URL, headers={'Referer': _REFERER}, timeout=5.0)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        d = data.get('data', {})
        if not d:
            return None

        # hk2sh = northbound (HK -> Shanghai)
        # sh2hk = southbound (Shanghai -> HK)
        hk2sh = d.get('hk2sh', {})
        sh2hk = d.get('sh2hk', {})

        # dayNetAmtIn is in 万元 (10k yuan) for southbound
        # Convert to 元
        north_net_yi = hk2sh.get('dayNetAmtIn', 0) / 10000  # 亿元
        south_net_yi = sh2hk.get('dayNetAmtIn', 0) / 10000

        return {
            'source': 'eastmoney_daily',
            'today_date': date.today().isoformat(),
            'net_north_yi': north_net_yi,  # 亿元
            'net_south_yi': south_net_yi,
            'north_quota_used': hk2sh.get('dayAmtRemain', 0),  # 剩余配额
            'north_quota_total': hk2sh.get('dayAmtThreshold', 0),  # 总配额
            'last_time': hk2sh.get('date', ''),
        }
    except (json.JSONDecodeError, KeyError):
        return None


def _parse_kamt(kamt: dict) -> dict:
    """解析 KAMT realtime data."""
    n2s_raw = kamt.get('n2s', [])  # North to South (港资买A股)
    s2n_raw = kamt.get('s2n', [])  # South to North (沪深买港股)

    def parse_last(series: list) -> dict:
        for entry in reversed(series):
            parts = entry.split(',')
            if len(parts) >= 6 and parts[0]:
                try:
                    return {
                        'quota_used': float(parts[2]) if parts[2] else 0.0,
                        'quota_total': float(parts[4]) if parts[4] else 0.0,
                        'amount': float(parts[3]) if parts[3] else 0.0,
                        'cum_amount': float(parts[5]) if parts[5] else 0.0,
                        'last_time': parts[0],
                    }
                except (ValueError, IndexError):
                    continue
        return {'quota_used': 0.0, 'quota_total': 0.0, 'amount': 0.0, 'cum_amount': 0.0, 'last_time': ''}

    n2s = parse_last(n2s_raw)
    s2n = parse_last(s2n_raw)
    net_north = n2s.get('cum_amount', 0) - s2n.get('cum_amount', 0)

    return {
        'source': 'eastmoney_rtmin',
        'today_date': date.today().isoformat(),
        'net_north_cny': net_north,  # 元
        'net_north_yi': net_north / 1e8,  # 亿元
        'n2s': n2s,
        's2n': s2n,
        'last_time': n2s.get('last_time', ''),
    }


def cached_kamt(force_refresh: bool = False) -> Optional[dict]:
    """
    获取北向资金数据（带缓存）。
    缓存 TTL: 60 秒（数据每分钟更新）。
    Fallback 链: realtime → daily_summary → 缓存（旧数据+stale标记）
    """
    cache_key = _KAMT_CACHE_KEY

    if not force_refresh:
        cached = _SafeCache.get(cache_key)
        if cached is not None:
            return cached

    # Try realtime first
    data = fetch_kamt_realtime()
    source = 'realtime'
    if data is None:
        # Fallback to daily summary
        data = fetch_kamt_daily_fallback()
        source = 'daily_fallback'

    if data is None:
        # Last resort: return stale cached data
        cached = _SafeCache.get(cache_key)
        if cached is not None:
            cached = dict(cached)
            cached['stale'] = True
            _log.info('KAMT: all sources failed, returning stale cached data')
            return cached
        _log.warning('KAMT: all sources failed and no cache available')
        return None

    data['source'] = source
    data['fetched_at'] = datetime.now().isoformat()
    _SafeCache.set(cache_key, data, ttl_seconds=60)
    return data


# ─── 2. 分钟 K 线缓存 ──────────────────────────────────────────────────────

_MINUTE_KLINE_PREFIX = 'min_kline_'


def cached_minute_kline(symbol: str, fetch_fn: Callable[[], Optional[list]]) -> Optional[list]:
    """
    获取分钟 K 线数据（带 60s TTL 缓存）。
    fetch_fn: 无参数的函数，返回 kline list 或 None
    """
    key = f'{_MINUTE_KLINE_PREFIX}{symbol}'
    cached = _SafeCache.get(key)
    if cached is not None:
        return cached

    data = fetch_fn()
    if data is not None:
        _SafeCache.set(key, data, ttl_seconds=60)
    return data


# ─── 3. 通用 HTTP 缓存 ───────────────────────────────────────────────────

def cached_get(url: str, ttl_seconds: float = 30, headers: Dict = None) -> Optional[str]:
    """通用 HTTP GET 缓存（默认 30s TTL）。"""
    key = f'http_get_{hash(url)}'
    cached = _SafeCache.get(key)
    if cached is not None:
        return cached

    data = _http_get(url, headers=headers)
    if data is not None:
        _SafeCache.set(key, data, ttl_seconds=ttl_seconds)
    return data


# ─── 4. 集成到 northbound.py ───────────────────────────────────────────

def patch_northbound():
    """
    向 northbound.py 注入缓存能力。
    将 fetch_kamt 和 fetch_stock_northbound 替换为缓存版本。
    """
    nb_path = os.path.join(BACKEND_DIR, 'services', 'northbound.py')
    if not os.path.exists(nb_path):
        _log.warning('northbound.py not found at %s', nb_path)
        return

    with open(nb_path, encoding='utf-8') as f:
        content = f.read()

    # Check if already patched
    if 'from data_cache import cached_kamt' in content:
        _log.info('northbound.py already patched with data_cache')
        return

    # Add import after the existing imports
    marker = 'def fetch_kamt()'
    import_line = '\nfrom data_cache import cached_kamt  # P8 cache layer\n'

    if marker in content and 'from data_cache' not in content:
        content = content.replace(marker, import_line + marker, 1)
        with open(nb_path, 'w', encoding='utf-8') as f:
            f.write(content)
        _log.info('northbound.py patched: added cached_kamt import')
    else:
        _log.warning('Could not patch northbound.py (marker not found or already patched)')


# ─── CLI 测试 ──────────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    print('=== P8 Data Cache Test ===')

    # Test 1: KAMT cache
    print('\n[KAMT] Fetching (should hit network)...')
    data = cached_kamt()
    if data:
        print(f"  source={data.get('source')}  stale={data.get('stale', False)}")
        print(f"  net_north_yi={data.get('net_north_yi', data.get('net_north_cny', 0) / 1e8):.2f}亿")
        print(f"  last_time={data.get('last_time', data.get('last_time', ''))}")
    else:
        print('  FAILED: no data')

    print('\n[KAMT] Fetching again (should hit cache)...')
    data2 = cached_kamt()
    print(f"  source={data2.get('source')}  stale={data2.get('stale', False)}")

    # Test 2: force refresh
    print('\n[KAMT] Force refresh...')
    data3 = cached_kamt(force_refresh=True)
    print(f"  source={data3.get('source')}")

    # Test 3: cached_get
    print('\n[HTTP Cache] Testing generic cache...')
    url = 'https://httpbin.org/ip'
    r1 = cached_get(url)
    r2 = cached_get(url)  # should be cached
    print(f'  First: {r1 is not None}, Second: {r2 is not None} (same object={r1 is r2})')

    print('\n=== All tests complete ===')
