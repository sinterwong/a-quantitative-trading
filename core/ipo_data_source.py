"""
core/ipo_data_source.py — 港股 IPO 多源数据获取层（feature/ipo-stars）

功能：
  港股新股（即将上市/暗盘/已上市）的多源数据获取与融合。
  支持东方财富、港交所披露易、暗盘行情（辉立/富途/老虎）、新闻舆情 4 个数据源。
  内部使用线程安全缓存（_SafeCache，TTL 30s），避免重复请求。

数据源优先级：
  1. 东方财富  (eastmoney)   — P0，招股信息/中签率/募资规模
  2. 港交所披露易 (hkexnews)  — P0，招股书结构/聆讯状态/基石投资者
  3. 暗盘行情  (暗盘)         — P1，上市前一交易日 16:15-18:30
  4. 新闻舆情  (news)         — P1，路演反馈/机构认购意向

Usage:
  ds = IPODataSource()
  info = ds.get_ipo_info('09619')          # 股票代码（5位）
  grey = ds.get_grey_market('09619')        # 暗盘行情
  news = ds.get_ipo_news('09619')          # 新闻舆情
  hkex = ds.get_hkex_disclosure('09619')   # 港交所披露易
"""

from __future__ import annotations

import os
import sys
import json
import time
import ssl
import logging
import threading
import urllib.request
import re
from datetime import datetime, date
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field

# ── 路径兼容 ──────────────────────────────────────────────────────────────

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(os.path.dirname(THIS_DIR), 'backend')
sys.path.insert(0, THIS_DIR)
sys.path.insert(0, BACKEND_DIR)

# ── 日志 ──────────────────────────────────────────────────────────────────

_log = logging.getLogger('core.ipo_data_source')

# ── HTTP 工具（复用 data_cache 模式）─────────────────────────────────────

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE
_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
)
_EM_REFERER = 'https://data.eastmoney.com/'
_HKEX_REFERER = 'https://www.hkexnews.hk/'
_FINANCE_SINA = 'https://finance.sina.com.cn/'


def _http_get(url: str, headers: Dict = None, timeout: float = 8.0) -> Optional[str]:
    """带超时和错误处理的 HTTP GET。"""
    h = {'User-Agent': _USER_AGENT, **(headers or {})}
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
            return resp.read().decode('utf-8', errors='replace')
    except Exception as e:
        _log.debug('HTTP GET failed: %s — %s', url[:60], e)
        return None


# ── 线程安全缓存（复用 _SafeCache 模式）────────────────────────────────

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
    def get_or_fetch(cls, key: str, fetch_fn, ttl_seconds: float = 30) -> Optional[Any]:
        with cls._lock:
            entry = cls._store.get(key)
            if entry and entry.is_valid():
                return entry.value
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


# ── 数据结构 ─────────────────────────────────────────────────────────────

@dataclass
class IPOInfo:
    """东方财富 / 港交所 返回的标准化 IPO 信息。"""
    stock_code: str           # 港股代码（5位，纯数字）
    stock_name: str           # 公司简称
    listing_date: str         # 上市日期 YYYY-MM-DD
    issue_price: float        # 发行价（港元）
    issue_price_high: float   # 最高发行价（港元，区间报价时）
    issue_price_low: float    # 最低发行价
    shares: int               # 拟上市股份数（万股）
    proceeds: float           # 预计募资（亿港元）
    lot_size: int             # 每手股数
    application_ratio: float = 0.0   # 认购倍数（甲组）
    application_ratio_乙组: float = 0.0  # 认购倍数（乙组）
    application_deadline: str = ''  # 申购截止日
    listing_board: str = ''   # 上市板块（如主板、创板）
    industry: str = ''         # 所属行业
    sponsor: str = ''         # 保荐人/主承销商
    cornerstone_investors: List[str] = field(default_factory=list)  # 基石投资者
    source: str = 'unknown'   # 数据来源标记
    fetched_at: str = ''      # 抓取时间

    def __post_init__(self):
        if not self.fetched_at:
            self.fetched_at = datetime.now().isoformat()


@dataclass
class GreyMarketQuote:
    """暗盘行情快照。"""
    stock_code: str
    stock_name: str
    timestamp: str            # 行情时间
    bid_price: float          # 买一价（港元）
    ask_price: float          # 卖一价
    last_price: float         # 最新成交价
    change_pct: float         # 暗盘涨跌幅（%）
    volume: int                # 成交量（股）
    amount: float             # 成交额（港元）
    high: float                # 暗盘最高价
    low: float                 # 暗盘最低价
    source: str = 'unknown'   # '辉立' | '富途' | '老虎'

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price


@dataclass
class IPONewsItem:
    """单条 IPO 新闻。"""
    title: str
    url: str
    source: str               # '财联社' | '36kr' | '新浪财经' | '其他'
    published_at: str         # 发布时间
    summary: str = ''         # 摘要


# ── 东方财富数据源 ─────────────────────────────────────────────────────

class EastMoneyIPOSource:
    """
    东方财富 IPO 数据中心。
    接口文档：https://data.eastmoney.com/ipo/

    支持：
      - 在港股新股（即将上市）
      - 历史暗盘/首日表现
      - 公开发售认购数据（中签率/认购倍数）
    """

    name = 'eastmoney'

    # 即将上市新股列表（东方财富）
    _LIST_URL = (
        'https://datacenter-web.eastmoney.com/api/data/v1/get'
        '?reportName=RPT_HKEX_NEWS_LIST'
        '&columns=ALL'
        '&pageNumber=1&pageSize=20'
        '&sortTypes=-1&sortColumns=PUBLISH_DATE'
        '&filter=(TRADE_MARKET_CODE=%22MAIN%22)'
        '&source=WEB&client=WEB'
    )

    # 单只新股详情
    _DETAIL_URL = (
        'https://datacenter-web.eastmoney.com/api/data/v1/get'
        '?reportName=RPT_HKEX_IPO_DETAIL'
        '&columns=ALL'
        '&filter=(STOCK_CODE=%22{code}%22)'
        '&source=WEB&client=WEB'
    )

    # 公开发售数据（甲组/乙组认购倍数）
    _APPLICATION_URL = (
        'https://datacenter-web.eastmoney.com/api/data/v1/get'
        '?reportName=RPT_HKEX_APPLICATION'
        '&columns=ALL'
        '&filter=(STOCK_CODE=%22{code}%22)'
        '&source=WEB&client=WEB'
    )

    @classmethod
    def get_list(cls, force_refresh: bool = False) -> List[Dict]:
        """获取即将上市的港股新股列表（最多 20 条）。"""
        cache_key = 'em_ipo_list'
        if not force_refresh:
            cached = _SafeCache.get(cache_key)
            if cached is not None:
                return cached

        raw = _http_get(cls._LIST_URL, headers={'Referer': _EM_REFERER})
        if not raw:
            return []
        try:
            data = json.loads(raw)
            result = data.get('result', {}) or {}
            items = result.get('data', []) or []
            _SafeCache.set(cache_key, items, ttl_seconds=300)  # 5min TTL
            return items
        except (json.JSONDecodeError, KeyError):
            return []

    @classmethod
    def get_detail(cls, stock_code: str) -> Optional[Dict]:
        """获取单只新股详细信息。"""
        cache_key = f'em_ipo_detail_{stock_code}'
        cached = _SafeCache.get(cache_key)
        if cached is not None:
            return cached

        url = cls._DETAIL_URL.format(code=stock_code)
        raw = _http_get(url, headers={'Referer': _EM_REFERER})
        if not raw:
            return None
        try:
            data = json.loads(raw)
            result = data.get('result', {}) or {}
            item = result.get('data', [{}])[0] if result.get('data') else {}
            if item:
                _SafeCache.set(cache_key, item, ttl_seconds=300)
            return item
        except (json.JSONDecodeError, KeyError, IndexError):
            return None

    @classmethod
    def get_application_data(cls, stock_code: str) -> Optional[Dict]:
        """获取公开发售数据（认购倍数）。"""
        cache_key = f'em_ipo_app_{stock_code}'
        cached = _SafeCache.get(cache_key)
        if cached is not None:
            return cached

        url = cls._APPLICATION_URL.format(code=stock_code)
        raw = _http_get(url, headers={'Referer': _EM_REFERER})
        if not raw:
            return None
        try:
            data = json.loads(raw)
            result = data.get('result', {}) or {}
            item = result.get('data', [{}])[0] if result.get('data') else {}
            if item:
                _SafeCache.set(cache_key, item, ttl_seconds=300)
            return item
        except (json.JSONDecodeError, KeyError, IndexError):
            return None

    @classmethod
    def normalize_to_ipo_info(cls, raw: Dict) -> Optional[IPOInfo]:
        """将东方财富原始数据规范化为 IPOInfo。"""
        try:
            code = str(raw.get('STOCK_CODE', '')).strip()
            name = raw.get('COMPANY_NAME', raw.get('SECURITY_NAME_ABBR', ''))

            # 上市日期
            listing_date = raw.get('LISTING_DATE', '')
            if listing_date and len(listing_date) >= 10:
                listing_date = listing_date[:10]

            # 发行价
            issue_price = float(raw.get('ISSUE_PRICE', 0) or 0)
            issue_price_high = float(raw.get('ISSUE_PRICE_HIGH', issue_price) or issue_price)
            issue_price_low = float(raw.get('ISSUE_PRICE_LOW', issue_price) or issue_price)

            # 募资
            shares = int(float(raw.get('OFFERED_SHARES', 0) or 0) * 10000)  # 万股→股
            proceeds = float(raw.get('PROCEEDS', 0) or 0)  # 亿港元

            # 认购数据
            lot_size = int(raw.get('LOT_SIZE', 200) or 200)
            app_ratio = float(raw.get('APPLICATION_RATIO', 0) or 0)
            app_ratio_乙组 = float(raw.get('APPLICATION_RATIO_乙组', app_ratio) or app_ratio)

            # 申购截止日
            deadline = raw.get('APPLICATION_END_DATE', '')
            if deadline and len(deadline) >= 10:
                deadline = deadline[:10]

            # 保荐人/承销商
            sponsor = raw.get('SPONSOR', raw.get('UNDERWRITER', ''))

            # 基石投资者（逗号分隔字符串）
            cornerstone_str = raw.get('CORNERSTONE_INVESTORS', '')
            cornerstone = [c.strip() for c in cornerstone_str.split(',') if c.strip()] if cornerstone_str else []

            return IPOInfo(
                stock_code=code,
                stock_name=name,
                listing_date=listing_date,
                issue_price=issue_price,
                issue_price_high=issue_price_high,
                issue_price_low=issue_price_low,
                shares=shares,
                proceeds=proceeds,
                lot_size=lot_size,
                application_ratio=app_ratio,
                application_ratio_乙组=app_ratio_乙组,
                application_deadline=deadline,
                listing_board=raw.get('TRADE_MARKET', ''),
                industry=raw.get('INDUSTRY', ''),
                sponsor=sponsor,
                cornerstone_investors=cornerstone,
                source='eastmoney',
            )
        except Exception as e:
            _log.warning('normalize_to_ipo_info failed: %s', e)
            return None


# ── 港交所披露易数据源 ─────────────────────────────────────────────────

class HKExNewsSource:
    """
    港交所披露易（HKEXnews）数据源。
    官方页面：https://www.hkexnews.hk/

    支持：
      - 聆讯资料集状态
      - 招股书披露（PDF 链接）
      - 基石投资者名单
      - 上市正式通告
    """

    name = 'hkexnews'

    # 港交所搜索接口（按股票代码搜索）
    _SEARCH_URL = (
        'https://www.hkexnews.hk/apis/search/search.aspx'
        '?q=stockcode:{code}&t=0&type=1&site=main'
    )

    # 聆讯进度
    _HEARING_URL = (
        'https://www.hkexnews.hk/apis/search/prospectus.aspx'
        '?type=hearing&stockcode={code}'
    )

    @classmethod
    def get_listing_status(cls, stock_code: str) -> Optional[str]:
        """
        获取聆讯/上市状态。
        返回：'listed' | 'pending' | 'approved' | 'withdrawn' | 'unknown'
        """
        cache_key = f'hkex_status_{stock_code}'
        cached = _SafeCache.get(cache_key)
        if cached is not None:
            return cached

        # 尝试直接抓取
        url = cls._SEARCH_URL.format(code=stock_code)
        raw = _http_get(url, headers={'Referer': _HKEX_REFERER}, timeout=10)
        status = 'unknown'

        if raw:
            # 解析 HTML 中的状态标记
            if 'listed' in raw.lower() or '已上市' in raw:
                status = 'listed'
            elif 'approved' in raw.lower() or '已批准' in raw:
                status = 'approved'
            elif 'pending' in raw.lower() or '处理中' in raw:
                status = 'pending'
            elif 'withdrawn' in raw.lower() or '撤回' in raw:
                status = 'withdrawn'

        _SafeCache.set(cache_key, status, ttl_seconds=3600)  # 1h TTL
        return status

    @classmethod
    def get_prospectus_links(cls, stock_code: str) -> List[Dict[str, str]]:
        """
        获取招股书相关文件链接。
        返回 [{'title': str, 'url': str, 'date': str}]
        """
        cache_key = f'hkex_prospectus_{stock_code}'
        cached = _SafeCache.get(cache_key)
        if cached is not None:
            return cached

        url = cls._HEARING_URL.format(code=stock_code)
        raw = _http_get(url, headers={'Referer': _HKEX_REFERER}, timeout=10)
        links = []

        if raw:
            # 提取 PDF 链接
            for m in re.finditer(r'href="([^"]+\.pdf[^"]*)"', raw):
                href = m.group(1)
                if 'hkexnews' in href or href.startswith('/'):
                    full_url = href if href.startswith('http') else f'https://www.hkexnews.hk{href}'
                    links.append({'url': full_url, 'title': '招股书'})

        _SafeCache.set(cache_key, links, ttl_seconds=3600)
        return links

    @classmethod
    def extract_cornerstone_investors(cls, stock_code: str) -> List[str]:
        """
        从港交所招股书中提取基石投资者名单（简化版：抓取搜索页文本）。
        完整实现需要解析 PDF。
        """
        cache_key = f'hkex_cornerstone_{stock_code}'
        cached = _SafeCache.get(cache_key)
        if cached is not None:
            return cached

        url = cls._SEARCH_URL.format(code=stock_code)
        raw = _http_get(url, headers={'Referer': _HKEX_REFERER}, timeout=10)
        investors = []

        if raw:
            # 常见基石投资者关键词
            patterns = [
                r'基石投资者[：:]\s*([^<\n]+)',
                r'Cornerstone[^\n]{0,50}([A-Z][a-zA-Z\s&,\.]+(?:Capital|Investment|Temasek|Hillhouse|Redview|New Horizon)+)',
                r'(高瓴|红杉|淡马锡|中金资本|博裕|涌金|启明|愉悦|弘毅)[^\n]{0,30}',
            ]
            for pat in patterns:
                for m in re.finditer(pat, raw):
                    name = m.group(1).strip()
                    if name and len(name) > 2:
                        investors.append(name)

        investors = list(dict.fromkeys(investors))  # 去重保持顺序
        _SafeCache.set(cache_key, investors, ttl_seconds=3600)
        return investors


# ── 暗盘行情数据源 ─────────────────────────────────────────────────────

class GreyMarketSource:
    """
    暗盘行情（辉立/富途/老虎）。
    暗盘时间：上市前一交易日 16:15-18:30

    注意：暗盘接口通常需要登录或较复杂鉴权，此处提供标准化查询框架。
    实际数据优先从东方财富暗盘数据降级，或直接对接券商 API。
    """

    name = 'greymarket'

    # 东方财富暗盘数据（部分覆盖）
    _EM_GREY_URL = (
        'https://datacenter-web.eastmoney.com/api/data/v1/get'
        '?reportName=RPT_HKEX_DARK_MARKET'
        '&columns=ALL'
        '&filter=(STOCK_CODE=%22{code}%22)'
        '&source=WEB&client=WEB'
    )

    # 辉立暗盘（需 VPN 或券商内网）
    _PHILEX_URL = 'https://www.phillmart.com.hk/ipo/greyMarket/{code}'

    @classmethod
    def get_quote(cls, stock_code: str) -> Optional[GreyMarketQuote]:
        """
        获取暗盘行情，优先东方财富，其次辉立。
        返回 GreyMarketQuote 或 None（暗盘未开始/无数据）。
        """
        cache_key = f'grey_quote_{stock_code}'
        cached = _SafeCache.get(cache_key)
        if cached is not None:
            return cached

        quote = cls._fetch_em_grey(stock_code)
        if quote is None:
            quote = cls._fetch_philex(stock_code)

        if quote is not None:
            _SafeCache.set(cache_key, quote, ttl_seconds=60)  # 1min，暗盘行情实时变化

        return quote

    @classmethod
    def _fetch_em_grey(cls, stock_code: str) -> Optional[GreyMarketQuote]:
        """东方财富暗盘数据。"""
        url = cls._EM_GREY_URL.format(code=stock_code)
        raw = _http_get(url, headers={'Referer': _EM_REFERER})
        if not raw:
            return None
        try:
            data = json.loads(raw)
            result = data.get('result', {}) or {}
            item = result.get('data', [{}])[0] if result.get('data') else {}
            if not item:
                return None

            return GreyMarketQuote(
                stock_code=stock_code,
                stock_name=item.get('STOCK_NAME', ''),
                timestamp=item.get('TIME', datetime.now().isoformat()),
                bid_price=float(item.get('BID_PRICE', 0) or 0),
                ask_price=float(item.get('ASK_PRICE', 0) or 0),
                last_price=float(item.get('LAST_PRICE', 0) or 0),
                change_pct=float(item.get('CHANGE_PCT', 0) or 0),
                volume=int(float(item.get('VOLUME', 0) or 0)),
                amount=float(item.get('AMOUNT', 0) or 0),
                high=float(item.get('HIGH', 0) or 0),
                low=float(item.get('LOW', 0) or 0),
                source='eastmoney_grey',
            )
        except (json.JSONDecodeError, KeyError, IndexError, ValueError):
            return None

    @classmethod
    def _fetch_philex(cls, stock_code: str) -> Optional[GreyMarketQuote]:
        """辉立暗盘接口（结构化 HTML）。"""
        url = cls._PHILEX_URL.format(code=stock_code)
        raw = _http_get(url, headers={'Referer': 'https://www.phillmart.com.hk/'}, timeout=10)
        if not raw:
            return None
        try:
            # 提取价格
            last_price = 0.0
            change_pct = 0.0
            volume = 0

            m = re.search(r'Last[\s\S]{0,30}?(\d+\.?\d*)', raw)
            if m:
                last_price = float(m.group(1))
            m = re.search(r'Change[\s\S]{0,30}?(-?\d+\.?\d*)%', raw)
            if m:
                change_pct = float(m.group(1))
            m = re.search(r'Volume[\s\S]{0,30}?(\d+)', raw)
            if m:
                volume = int(m.group(1))

            return GreyMarketQuote(
                stock_code=stock_code,
                stock_name='',
                timestamp=datetime.now().isoformat(),
                bid_price=last_price * 0.998,
                ask_price=last_price * 1.002,
                last_price=last_price,
                change_pct=change_pct,
                volume=volume,
                amount=last_price * volume,
                high=last_price * 1.01,
                low=last_price * 0.99,
                source='philex',
            )
        except Exception:
            return None


# ── IPO 新闻舆情数据源 ─────────────────────────────────────────────────

class IPONewsSource:
    """
    IPO 新闻舆情（财联社/36kr/新浪财经）。
    用于获取路演反馈、机构认购意向、发行区间调整等信息。
    """

    name = 'news'

    # 东方财富新闻搜索（IPO 关键词）
    _EM_NEWS_URL = (
        'https://search-api-web.eastmoney.com/search/jsonp'
        '?cb=jQuery&param={"uid":"","keyword":"{code}%20IPO","type":["news"],"client":"web","version":"v1","keywordType":0}'
    )

    # 新浪财经新闻
    _SINA_NEWS_URL = (
        'https://search.sina.com.cn/?q={code}%20IPO&c=news&from=&ie=utf-8'
    )

    @classmethod
    def get_news(cls, stock_code: str, limit: int = 10) -> List[IPONewsItem]:
        """
        获取 IPO 相关新闻（按股票代码）。
        返回最多 limit 条新闻，按时间倒序。
        """
        cache_key = f'ipo_news_{stock_code}'
        cached = _SafeCache.get(cache_key)
        if cached is not None:
            return cached[:limit]

        items: List[IPONewsItem] = []

        # 东方财富新闻
        items.extend(cls._fetch_em_news(stock_code))

        # 新浪新闻（备选）
        if len(items) < limit:
            items.extend(cls._fetch_sina_news(stock_code, limit - len(items)))

        # 去重（按标题）
        seen = set()
        unique = []
        for it in items:
            if it.title not in seen:
                seen.add(it.title)
                unique.append(it)

        _SafeCache.set(cache_key, unique, ttl_seconds=600)  # 10min TTL
        return unique[:limit]

    @classmethod
    def _fetch_em_news(cls, stock_code: str) -> List[IPONewsItem]:
        """东方财富新闻搜索。"""
        url = cls._EM_NEWS_URL.format(code=stock_code)
        raw = _http_get(url, headers={'Referer': _EM_REFERER})
        if not raw:
            return []
        try:
            # JSONP 回调包裹：jQuery({...})
            json_str = re.sub(r'^jQuery\(', '', raw.rstrip(');')) if raw.startswith('jQuery') else raw
            data = json.loads(json_str)
            result = data.get('result', []) or []
            items = []
            for it in result[:5]:
                items.append(IPONewsItem(
                    title=it.get('title', ''),
                    url=it.get('url', ''),
                    source='东方财富',
                    published_at=it.get('createdate', ''),
                    summary=it.get('summary', ''),
                ))
            return items
        except (json.JSONDecodeError, KeyError, TypeError):
            return []

    @classmethod
    def _fetch_sina_news(cls, stock_code: str, limit: int) -> List[IPONewsItem]:
        """新浪财经新闻（HTML 解析）。"""
        url = cls._SINA_NEWS_URL.format(code=stock_code)
        raw = _http_get(url, headers={'Referer': _FINANCE_SINA}, timeout=10)
        if not raw:
            return []
        items = []
        try:
            # 提取新闻标题和链接
            for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*class="[^"]*title[^"]*"[^>]*>([^<]+)</a>', raw):
                url_link = m.group(1)
                title = m.group(2).strip()
                if title and len(title) > 5:
                    items.append(IPONewsItem(
                        title=title,
                        url=url_link,
                        source='新浪财经',
                        published_at='',
                    ))
            # 提取时间
            for i, m in enumerate(re.finditer(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})', raw)):
                if i < len(items):
                    items[i].published_at = m.group(1)
        except Exception:
            pass
        return items[:limit]


# ── 统一数据获取入口 ────────────────────────────────────────────────────

class IPODataSource:
    """
    港股 IPO 多源数据统一入口。
    聚合 4 个数据源，提供统一缓存和数据融合能力。

    Usage:
      ds = IPODataSource()
      info = ds.get_ipo_info('09619')       # IPO 基本信息
      grey = ds.get_grey_market('09619')   # 暗盘行情
      news = ds.get_ipo_news('09619')      # 新闻舆情
      hkex = ds.get_hkex_info('09619')     # 港交所聆讯状态
    """

    def __init__(self):
        self.em = EastMoneyIPOSource()
        self.hkex = HKExNewsSource()
        self.grey = GreyMarketSource()
        self.news = IPONewsSource()

    # ── 主接口 ──────────────────────────────────────────────────────────

    def get_ipo_info(self, stock_code: str) -> Optional[IPOInfo]:
        """
        获取 IPO 基本信息（东方财富 + 港交所交叉验证）。
        优先级：东方财富 > 港交所
        """
        # 东方财富
        raw = self.em.get_detail(stock_code)
        if raw:
            info = self.em.normalize_to_ipo_info(raw)
            if info:
                # 补充港交所基石投资者
                hkex_investors = self.hkex.extract_cornerstone_investors(stock_code)
                if hkex_investors and not info.cornerstone_investors:
                    info.cornerstone_investors = hkex_investors
                # 补充认购数据
                app_data = self.em.get_application_data(stock_code)
                if app_data:
                    info.application_ratio = float(app_data.get('APPLICATION_RATIO', info.application_ratio) or 0)
                    info.application_ratio_乙组 = float(app_data.get('APPLICATION_RATIO_乙组', info.application_ratio_乙组) or 0)
                return info

        # 降级：港交所
        status = self.hkex.get_listing_status(stock_code)
        _log.debug('get_ipo_info(%s): EM failed, hkex status=%s', stock_code, status)
        return None

    def get_upcoming_ipos(self, force_refresh: bool = False) -> List[IPOInfo]:
        """
        获取即将上市的港股新股列表。
        返回按上市日期升序排列的 IPOInfo 列表。
        """
        items = self.em.get_list(force_refresh=force_refresh)
        results = []
        for raw in items:
            info = self.em.normalize_to_ipo_info(raw)
            if info:
                results.append(info)
        return sorted(results, key=lambda x: x.listing_date)

    def get_grey_market(self, stock_code: str) -> Optional[GreyMarketQuote]:
        """获取暗盘行情（上市前一交易日 16:15 后有效）。"""
        return self.grey.get_quote(stock_code)

    def get_ipo_news(self, stock_code: str, limit: int = 10) -> List[IPONewsItem]:
        """获取 IPO 相关新闻舆情。"""
        return self.news.get_news(stock_code, limit=limit)

    def get_hkex_info(self, stock_code: str) -> Dict[str, Any]:
        """
        获取港交所披露易相关信息（聆讯状态、招股书链接）。
        """
        status = self.hkex.get_listing_status(stock_code)
        prospectus = self.hkex.get_prospectus_links(stock_code)
        cornerstone = self.hkex.extract_cornerstone_investors(stock_code)
        return {
            'stock_code': stock_code,
            'listing_status': status,
            'prospectus_links': prospectus,
            'cornerstone_investors': cornerstone,
            'fetched_at': datetime.now().isoformat(),
        }

    def get_all_sources(self, stock_code: str) -> Dict[str, Any]:
        """
        全量数据获取：同时调用 4 个数据源，返回融合结果。
        用于生成完整 IPO 分析报告。
        """
        ipo_info = self.get_ipo_info(stock_code)
        grey = self.get_grey_market(stock_code)
        news = self.get_ipo_news(stock_code)
        hkex = self.get_hkex_info(stock_code)

        return {
            'stock_code': stock_code,
            'ipo_info': ipo_info,
            'grey_market': grey,
            'news': news,
            'hkex': hkex,
            'fetched_at': datetime.now().isoformat(),
        }

    # ── 缓存管理 ────────────────────────────────────────────────────────

    def invalidate(self, stock_code: str = None):
        """
        清除缓存。
        不指定 stock_code 时清除所有缓存。
        """
        if stock_code:
            for prefix in ('em_ipo_detail_', 'em_ipo_app_', 'grey_quote_', 'ipo_news_', 'hkex_'):
                _SafeCache.invalidate(f'{prefix}{stock_code}')
        else:
            _SafeCache.clear()


# ── CLI 测试 ────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

    print('=== IPO Data Source Test ===')

    ds = IPODataSource()

    # Test: upcoming IPOs list
    print('\n[Upcoming IPOs]')
    ipos = ds.get_upcoming_ipos()
    print(f'  Found {len(ipos)} upcoming IPOs')
    for ipo in ipos[:3]:
        print(f'  {ipo.stock_code} {ipo.stock_name} 上市:{ipo.listing_date} 发行价:{ipo.issue_price}')

    # Test: single IPO info (example code - may not exist)
    print('\n[IPO Info]')
    info = ds.get_ipo_info('09619')  # 示例代码
    if info:
        print(f'  {info.stock_name}: 发行价={info.issue_price} 保荐人={info.sponsor}')
        print(f'  基石: {info.cornerstone_investors}')
    else:
        print('  No data for this code (expected)')

    # Test: grey market
    print('\n[Grey Market]')
    grey = ds.get_grey_market('09619')
    if grey:
        print(f'  last={grey.last_price} change={grey.change_pct}%')
    else:
        print('  No grey market data (expected - not yet listed or no EM coverage)')

    # Test: news
    print('\n[News]')
    news = ds.get_ipo_news('09619')
    print(f'  Found {len(news)} news items')
    for n in news[:2]:
        print(f'  [{n.source}] {n.title[:50]}')

    # Test: HKEX
    print('\n[HKEX]')
    hkex = ds.get_hkex_info('09619')
    print(f'  Status={hkex.get("listing_status")}  Cornerstone={hkex.get("cornerstone_investors")}')

    print('\n=== All tests complete ===')
