#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dynamic_selector.py - 多维度动态选股模块 V2
==========================================
五个评分维度：
1. 新闻热度分 (15%) - 政策/业绩/产品/资金/传闻 分类加权
2. 板块行情分 (35%) - 今日板块涨跌幅相对排名（硬数据）
3. 资金流向分 (25%) - 北向/主力净流入排名（硬数据）
4. 技术趋势分 (15%) - 成分股涨跌幅信号
5. 成分股一致性 (10%) - 板块内部联动强度

架构：
- 直接用东方财富板块涨跌幅+资金数据，通过BK码获取成分股
- 多数据源 fallback：东方财富 -> 同花顺 -> 文件缓存
- 域级 rate limiter + 实例级缓存 + 文件缓存 三层防护
- 降级机制：所有API失败时自动切换宽基ETF
"""

import urllib.request
import ssl
import os
import json
import time as _time
from datetime import datetime
from typing import List, Dict, Tuple, Optional

# 新闻质量评分（过滤含糊信号）
try:
    from quant.news_quality import score_news_item, score_and_filter_news
except ImportError:
    score_news_item = None
    score_and_filter_news = None
    def score_news_item(title): return 0.5  # fallback
    def score_and_filter_news(news, **kw): return news

# 新闻情绪打分（基于关键词+真实新闻）
try:
    from quant.news_scorer import NewsSentimentScorer
except ImportError:
    NewsSentimentScorer = None


class _SentimentScorerWrapper:
    """延迟实例化+单例封装 NewsSentimentScorer，避免网络请求拖慢启动"""
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self, regime: str = 'CALM'):
        self._scorer = None

    def get_scorer(self):
        if self._scorer is None and NewsSentimentScorer:
            self._scorer = NewsSentimentScorer(cache_minutes=10)
        return self._scorer

    def get_sector_sentiment(self) -> Dict[str, float]:
        """返回 {板块名: 情绪分数 -100~100} 用于评分增强"""
        scorer = self.get_scorer()
        if not scorer:
            return {}
        try:
            sentiment = scorer.get_market_sentiment()
            return sentiment.get('sector_scores', {})  # {sector: avg_score}
        except Exception:
            return {}

# 日志级别: DEBUG / INFO / WARNING / ERROR
LOG_LEVEL = 'INFO'


def _log(level: str, msg: str):
    if level == 'DEBUG' and LOG_LEVEL != 'DEBUG':
        return
    if level == 'INFO' and LOG_LEVEL not in ('INFO', 'DEBUG'):
        return
    ts = datetime.now().strftime('%H:%M:%S')
    print(f"[{ts}] [{level}] {msg}")

# 禁用代理
for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy']:
    if key in os.environ:
        del os.environ[key]

SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

THIS_DIR = os.path.dirname(os.path.abspath(__file__))

# Global rate limit tracker (per-domain)
_last_call: Dict[str, float] = {}

def get(url: str, headers: dict = None, timeout: int = 10) -> Optional[str]:
    """
    HTTP GET with exponential backoff on 429/503, and domain-level rate limiting.

    Strategy:
    - Normal calls: respect 200ms gap to avoid triggering rate limits
    - On 429/503: exponential backoff (1s -> 2s -> 4s), max 3 retries
    - On other errors: give up immediately
    """
    import time
    domain = url.split('/')[2] if '://' in url else ''
    _last_call.setdefault(domain, 0)

    for attempt in range(4):   # 0=first, 1=retry1(1s), 2=retry2(2s), 3=retry3(4s)
        try:
            # Domain rate limit: 200ms gap between calls to same domain
            elapsed = time.time() - _last_call[domain]
            if elapsed < 0.2:
                time.sleep(0.2 - elapsed)

            h = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            if headers:
                h.update(headers)
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as r:
                _last_call[domain] = time.time()
                if r.status in (429, 503):
                    # Rate limited - back off and retry
                    if attempt < 3:
                        backoff = 2 ** attempt  # 1s, 2s, 4s
                        _log('WARNING', 'get: HTTP ' + str(r.status) + ' from ' + domain
                             + ', retry ' + str(attempt + 1) + '/3 in ' + str(backoff) + 's')
                        time.sleep(backoff)
                        continue
                    else:
                        _log('WARNING', 'get: HTTP 429/503 exhausted retries from ' + domain)
                        return None
                return r.read().decode('utf-8', errors='replace')
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                if attempt < 3:
                    backoff = 2 ** attempt
                    _log('WARNING', 'get: HTTPError ' + str(e.code) + ' from ' + domain
                         + ', retry ' + str(attempt + 1) + '/3 in ' + str(backoff) + 's')
                    time.sleep(backoff)
                    continue
                else:
                    _log('WARNING', 'get: HTTPError 429/503 exhausted from ' + domain)
                    return None
            else:
                _log('WARNING', 'get: HTTPError ' + str(e.code) + ' from ' + domain + ': ' + str(e))
                return None
        except Exception as e:
            if attempt < 3:
                _log('WARNING', 'get: error from ' + domain + ' (attempt ' + str(attempt + 1)
                     + '): ' + str(e)[:80])
                time.sleep(0.5 * (attempt + 1))
            else:
                _log('WARNING', 'get: all retries failed for ' + domain + ': ' + str(e)[:80])
                return None
    return None


def get_gbk(url: str, headers: dict = None, timeout: int = 10) -> Optional[str]:
    """HTTP GET (GBK) with same exponential backoff strategy as get()."""
    import time
    domain = url.split('/')[2] if '://' in url else ''
    _last_call.setdefault(domain, 0)

    for attempt in range(4):
        try:
            elapsed = time.time() - _last_call[domain]
            if elapsed < 0.2:
                time.sleep(0.2 - elapsed)

            h = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
            if headers:
                h.update(headers)
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, context=SSL_CTX, timeout=timeout) as r:
                _last_call[domain] = time.time()
                if r.status in (429, 503):
                    if attempt < 3:
                        backoff = 2 ** attempt
                        _log('WARNING', 'get_gbk: HTTP ' + str(r.status) + ' from ' + domain
                             + ', retry ' + str(attempt + 1) + '/3 in ' + str(backoff) + 's')
                        time.sleep(backoff)
                        continue
                    return None
                return r.read().decode('gbk', errors='replace')
        except urllib.error.HTTPError as e:
            if e.code in (429, 503):
                if attempt < 3:
                    backoff = 2 ** attempt
                    time.sleep(backoff)
                    continue
                return None
            return None
        except Exception as e:
            if attempt < 3:
                time.sleep(0.5 * (attempt + 1))
            else:
                return None
    return None
# ============================================================
# 文件级缓存（进程重启后仍有效）
# ============================================================

CACHE_DIR = os.path.join(THIS_DIR, 'cache')


def _ensure_cache_dir():
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
    except Exception:
        pass


def _read_file_cache(filename: str, max_age_seconds: int = 3600) -> Optional[Dict]:
    """
    读取文件缓存，如果文件存在且未过期则返回内容
    max_age_seconds=3600 表示1小时内有效
    """
    try:
        import time as _time
        _ensure_cache_dir()
        path = os.path.join(CACHE_DIR, filename)
        if not os.path.exists(path):
            return None
        # 检查文件修改时间
        mtime = os.path.getmtime(path)
        if _time.time() - mtime > max_age_seconds:
            return None
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _write_file_cache(filename: str, data: Dict) -> bool:
    """写入文件缓存，同时清理超限文件（最多保留5个缓存文件）"""
    try:
        _ensure_cache_dir()
        _cleanup_cache(max_files=5)
        path = os.path.join(CACHE_DIR, filename)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        return True
    except Exception:
        return False


def _cleanup_cache(max_files: int = 5) -> None:
    """
    清理缓存目录：
    - 保留最多 max_files 个 .json 缓存文件（按修改时间，最新的优先）
    - 删除 .tmp 临时文件
    """
    try:
        if not os.path.exists(CACHE_DIR):
            return
        files = [
            f for f in os.listdir(CACHE_DIR)
            if f.endswith('.json') or f.endswith('.tmp')
        ]
        # 删除临时文件
        for f in files:
            if f.endswith('.tmp'):
                try:
                    os.remove(os.path.join(CACHE_DIR, f))
                except Exception:
                    pass
        # 超过数量限制时，删除最旧的 .json 文件
        json_files = sorted(
            [f for f in os.listdir(CACHE_DIR) if f.endswith('.json')],
            key=lambda x: os.path.getmtime(os.path.join(CACHE_DIR, x))
        )
        if len(json_files) > max_files:
            for old_file in json_files[:-max_files]:
                try:
                    os.remove(os.path.join(CACHE_DIR, old_file))
                    _log('INFO', '_cleanup_cache: removed oldest cache file: ' + old_file)
                except Exception:
                    pass
    except Exception:
        pass


def safe_float(val, default=0.0):
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def safe_int(val, default=0):
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


# ============================================================
# 新闻重要性分级
# ============================================================

NEWS_WEIGHTS = {
    '政策': 10,
    '业绩': 8,
    '产品': 7,
    '资金': 6,
    '行业': 4,
    '一般': 3,
    '传闻': 1,
}

NEWS_POLICY_KEYWORDS = ['央行', '降准', '降息', '证监会', '国务院', '发改委', '财政部', '工信部', '监管', '政策', '部署', '纲领', '加快', '推动', '支持']
NEWS_EARNINGS_KEYWORDS = ['业绩', '财报', '净利润', '营收', '增长', '盈利', '预盈', '预亏', '订单', '签约', '超预期']
NEWS_PRODUCT_KEYWORDS = ['发布', '推出', '上市', '交付', '突破', '首创', '独家', '专利', '完成', '实现']
NEWS_FUND_KEYWORDS = ['北向', '净流入', '增持', '大单', '定增', '回购', '社保', '险资', '机构']
NEWS_RUMOR_KEYWORDS = ['传闻', '疑似', '或因', '知情人士', '接近']

# 板块关键词（用于新闻→板块映射）
SECTOR_NEWS_KEYWORDS = {
    'AI': ['人工智能', 'AI', '大模型', 'LLM', 'AIGC', '算力', 'ChatGPT', 'DeepSeek'],
    '半导体': ['半导体', '芯片', '集成电路', '晶圆', '光刻', '刻蚀', 'HBM', '存储'],
    '机器人': ['机器人', '人形机器人', '工业机器人', '具身智能', 'eVTOL'],
    '新能源': ['新能源', '锂电池', '锂电', '动力电池', '储能', '固态电池', '光伏', '逆变器'],
    '医药': ['医药', '生物医药', '创新药', '中药', '医疗器械', 'CXO', '医院'],
    '白酒': ['白酒', '茅台', '五粮液', '泸州老窖', '酒企', '酒类'],
    '军工': ['军工', '国防', '航空航天', '导弹', '舰船', '军机'],
    '金融': ['券商', '银行', '保险', '证券', '金融'],
    '地产': ['房地产', '楼市', '万科', '保利', '碧桂园', '地产股'],
    '消费': ['消费', '零售', '食品', '家电', '汽车'],
    '有色金属': ['有色金属', '铜', '铝', '稀土', '锂矿', '钴', '黄金'],
    '化工': ['化工', '化学制品', 'MDI', 'TDI', '化肥'],
    '电力': ['电力', '绿电', '虚拟电厂', '电力改革', '电网'],
    '高速铜连接': ['铜连接', '铜缆', '高速铜缆', 'DAC', '连接器'],
    '可控核聚变': ['核聚变', '人造太阳', '托卡马克'],
}


# ============================================================
# 宽基ETF（用于分散/防御）
# ============================================================

FALLBACK_ETFS = ['510300.SH', '159915.SZ', '512690.SH']  # 沪深300、创业板、酒ETF


# ============================================================
# 主类
# ============================================================

class DynamicStockSelectorV2:

    WEIGHT_NEWS = 0.15       # 新闻热度（降低，消息有噪声）
    WEIGHT_SECTOR = 0.35   # 板块行情（涨跌幅排名，硬数据）
    WEIGHT_FLOW = 0.25     # 资金流向（北向净流入，硬数据）
    WEIGHT_TECH = 0.15     # 技术趋势（成分股涨跌信号）
    WEIGHT_CONSISTENCY = 0.10  # 成分股一致性（板块内部联动强度）

    def __init__(self, regime: str = 'CALM'):
        self.news_cache: List[Dict] = []
        self.sectors_raw: List[Dict] = []     # 东方财富原始板块数据
        self.sector_scores: Dict = {}         # 最终综合评分 {板块名: score_dict}
        self.bk_scores: Dict = {}            # BK码评分 {bk_code: {total, perf, flow, tech}}
        self._news_fetched = False
        self._sectors_fetched = False
        # 实例级缓存：避免同一板块成分股重复请求
        self._constituent_cache: Dict[str, List[Dict]] = {}
        # 数据来源追踪: 'cache' | 'eastmoney' | 'ths' | 'failed'
        self._last_source: str = 'not_tried'
        self._last_news_source: str = 'not_tried'
        self.regime: str = regime  # BULL / BEAR / VOLATILE / CALM

    # ---------------------------------------------------------
    # 数据获取
    # ----------------------------------------------------------

    def fetch_market_news(self, limit: int = 30) -> List[Dict]:
        """获取市场资讯，依次尝试：缓存 -> 东方财富 -> 同花顺"""
        if self.news_cache:
            return self.news_cache[:limit]

        # 1. 文件缓存（30分钟内有效）
        cached = _read_file_cache('news.json', max_age_seconds=1800)
        if cached:
            self.news_cache = cached
            self._news_fetched = True
            self._last_news_source = 'cache'
            _log('INFO', 'fetch_market_news: using file cache (' + str(len(cached)) + ' items)')
            return self.news_cache[:limit]

        # 2. 东方财富主数据源
        url = (
            'https://np-listapi.eastmoney.com/comm/web/getNPList'
            '?client=web&bdr=0&page=1&pagesize=50&order=0&lmt=0'
            '&token=586e590d6c8b07833eb5d2e487e1a77'
        )
        raw = get(url, {'Referer': 'https://www.eastmoney.com/'})
        if raw:
            try:
                data = json.loads(raw)
                if data.get('data') and data['data'].get('list'):
                    self.news_cache = []
                    for item in data['data']['list'][:limit]:
                        self.news_cache.append({
                            'title': item.get('title', ''),
                            'time': item.get('time', ''),
                            'hot_value': item.get('hotValue', 0),
                            'url': item.get('url', '')
                        })
                    _write_file_cache('news.json', self.news_cache)
                    self._news_fetched = True
                    self._last_news_source = 'eastmoney'
                    _log('INFO', 'fetch_market_news: eastmoney ok (' + str(len(self.news_cache)) + ' items)')
                    return self.news_cache
            except Exception as e:
                _log('WARNING', 'fetch_market_news: eastmoney parse failed: ' + str(e))
        else:
            _log('WARNING', 'fetch_market_news: eastmoney empty (rate limited)')

        # 3. 同花顺备用
        url2 = f'https://news.10jqka.com.cn/tapp/news/push/stock/?page=1&tag=&track=website&pagesize={limit}'
        raw2 = get(url2, {'Referer': 'https://www.10jqka.com.cn/'})
        if raw2:
            try:
                data = json.loads(raw2)
                if data.get('data') and data['data'].get('list'):
                    self.news_cache = []
                    for item in data['data']['list'][:limit]:
                        self.news_cache.append({
                            'title': item.get('title', ''),
                            'time': item.get('ctime', ''),
                            'hot_value': 0,
                            'url': item.get('url', '')
                        })
                    _write_file_cache('news.json', self.news_cache)
                    self._news_fetched = True
                    self._last_news_source = 'ths'
                    _log('INFO', 'fetch_market_news: tonghuashun ok (' + str(len(self.news_cache)) + ' items)')
                    return self.news_cache
            except Exception as e:
                _log('WARNING', 'fetch_market_news: ths parse failed: ' + str(e))
        else:
            _log('WARNING', 'fetch_market_news: ths empty')

        self._news_fetched = True
        self._last_news_source = 'failed'
        _log('WARNING', 'fetch_market_news: all sources failed, returning empty')
        return []

    def fetch_sectors(self) -> List[Dict]:
        """获取板块行情+资金流向，依次尝试：文件缓存 -> 东方财富 -> 失败"""
        if self._sectors_fetched and self.sectors_raw:
            return self.sectors_raw

        # 1. 文件缓存（1小时内有效）
        cached = _read_file_cache('sectors.json', max_age_seconds=3600)
        if cached:
            self.sectors_raw = cached
            self._sectors_fetched = True
            self._last_source = 'cache'
            _log('INFO', 'fetch_sectors: using file cache (' + str(len(cached)) + ' sectors)')
            return self.sectors_raw

        # 2. 东方财富主数据源
        url = (
            'https://push2.eastmoney.com/api/qt/clist/get'
            '?pn=1&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3'
            '&fs=m:90+t:2+f:!50'
            '&fields=f2,f3,f4,f5,f6,f7,f12,f14,f62'
        )
        raw = get(url, {'Referer': 'https://quote.eastmoney.com/'})
        if raw:
            try:
                data = json.loads(raw)
                sectors = data.get('data', {}).get('diff', []) if isinstance(data.get('data'), dict) else []
                if sectors:
                    self.sectors_raw = sectors
                    self._last_source = 'eastmoney'
                    _write_file_cache('sectors.json', self.sectors_raw)
                    self._sectors_fetched = True
                    _log('INFO', 'fetch_sectors: eastmoney API ok (' + str(len(sectors)) + ' sectors)')
                    return self.sectors_raw
            except Exception as e:
                _log('WARNING', 'fetch_sectors: eastmoney parse failed: ' + str(e))
        else:
            _log('WARNING', 'fetch_sectors: eastmoney API returned empty (rate limited or network error)')


        # 3. Sina 财经板块数据（东方财富限流时的备用）
        try:
            import ssl as _ssl, urllib.request as _urllib, re as _re, json as _json
            _ctx = _ssl.create_default_context()
            _ctx.check_hostname = False
            _ctx.verify_mode = _ssl.CERT_NONE
            _sina_url = 'https://vip.stock.finance.sina.com.cn/q/view/newFLJK.php?param=class'
            _req = _urllib.Request(_sina_url, headers={'User-Agent': 'Mozilla/5.0'})
            with _urllib.urlopen(_req, context=_ctx, timeout=8) as _resp:
                _raw = _resp.read().decode('gbk', errors='replace')
            _m = _re.search(r'= ({.+?})', _raw, _re.DOTALL)
            if _m:
                _sina_data = _json.loads(_m.group(1))
                _sina_sectors = []
                for _k, _v in _sina_data.items():
                    _parts = _v.split(',')
                    if len(_parts) < 7:
                        continue
                    try:
                        _change_pct = float(_parts[4])
                    except (ValueError, IndexError):
                        _change_pct = 0.0
                    try:
                        _amount = float(_parts[6])
                    except (ValueError, IndexError):
                        _amount = 0.0
                    _synth_bk = 'SINA_' + _k.replace('gn_', 'GN')
                    _sina_sectors.append({
                        'f12': _synth_bk,
                        'f14': _parts[1],
                        'f3': _change_pct,
                        'f6': _amount,
                        'f62': 0,
                        '_source': 'sina'
                    })
                if _sina_sectors:
                    self.sectors_raw = _sina_sectors
                    self._last_source = 'sina'
                    _write_file_cache('sectors.json', self.sectors_raw)
                    self._sectors_fetched = True
                    _log('INFO', 'fetch_sectors: Sina ok (%d sectors)' % len(_sina_sectors))
                    return self.sectors_raw
        except Exception as _e:
            _log('WARNING', 'fetch_sectors: Sina fallback failed: %s' % str(_e))

        # 3. 无备用数据源，依赖文件缓存；标记失败状态
        self._sectors_fetched = True
        self._last_source = 'failed'
        _log('WARNING', 'fetch_sectors: all sources failed, returning empty list')
        return []

    def fetch_sector_constituents(self, bk_code: str, top_n: int = 5) -> List[Dict]:
        """获取板块成分股（按涨幅排序，取前N），带实例缓存"""
        # 缓存检查
        cache_key = f'{bk_code}:{top_n}'
        if cache_key in self._constituent_cache:
            return self._constituent_cache[cache_key]
        
        url = (
            f'https://push2.eastmoney.com/api/qt/clist/get'
            f'?pn=1&pz={top_n}&po=1&np=1&fltt=2&invt=2&fid=f3'
            f'&fs=b:{bk_code}'
            f'&fields=f2,f3,f4,f5,f6,f12,f14'
        )
        raw = get(url, {'Referer': 'https://quote.eastmoney.com/'})
        if not raw:
            return []

        try:
            data = json.loads(raw)
            items = data.get('data', {}).get('diff', []) if isinstance(data.get('data'), dict) else []
            result = []
            for item in items:
                code = item.get('f12', '')
                # 跳过指数（5位数代码）
                if not code or len(code) != 6:
                    continue
                market = 'SH' if code.startswith(('6', '5')) else 'SZ'
                result.append({
                    'code': code,
                    'full_code': f'{code}.{market}',
                    'name': item.get('f14', ''),
                    'price': safe_float(item.get('f2')),
                    'change_pct': safe_float(item.get('f3')),
                    'amount': safe_float(item.get('f6')),
                })
            # 缓存结果
            self._constituent_cache[cache_key] = result
            return result
        except Exception:
            return []

    def fetch_etf_price(self, code: str) -> Optional[Dict]:
        """获取单只ETF/股票实时价格"""
        # code: 512480.SH -> sh512480
        if '.' in code:
            num, market = code.split('.', 1)
            qt_code = market.lower() + num
        else:
            qt_code = 'sh' + code

        url = f'https://qt.gtimg.cn/q={qt_code}'
        raw = get_gbk(url)
        if not raw:
            return None

        try:
            for line in raw.strip().split(';'):
                if '=' not in line:
                    continue
                fields = line.split('=')[1].strip().strip('"').split('~')
                if len(fields) < 45:
                    continue
                return {
                    'name': fields[1],
                    'code': fields[2],
                    'price': safe_float(fields[3]),
                    'pre_close': safe_float(fields[4]),
                    'open': safe_float(fields[5]),
                    'volume': safe_int(fields[6]),
                    'amount': safe_float(fields[36]),
                    'high': safe_float(fields[33]),
                    'low': safe_float(fields[34]),
                    'change_pct': safe_float(fields[32]),
                    'change': safe_float(fields[33]),
                }
        except Exception:
            pass
        return None

    # ----------------------------------------------------------
    # 评分维度
    # ----------------------------------------------------------

    def calc_news_score(self) -> Dict[str, float]:
        """
        计算新闻热度分（已整合质量评分），返回 {大类板块名: 分数}
        - D级（质量<0.35）新闻直接丢弃
        - C级新闻权重打5折
        - B级以上新闻正常计入
        """
        if not self.news_cache:
            self.fetch_market_news()

        # 应用新闻质量过滤（丢弃D级）
        if score_and_filter_news:
            news_to_score = score_and_filter_news(self.news_cache, min_quality=0.35)
        else:
            news_to_score = self.news_cache

        scores = {}

        for news in news_to_score:
            title = news.get('title', '')
            if not title:
                continue

            # 质量折扣系数：C级×0.5，A/B级×1.0
            quality = news.get('quality', 0.5)
            if quality < 0.35:
                quality_factor = 0    # D级丢弃
            elif quality < 0.50:
                quality_factor = 0.5  # C级半折
            else:
                quality_factor = 1.0   # A/B级全价

            # 确定新闻类型基础权重
            if any(k in title for k in NEWS_POLICY_KEYWORDS):
                w = NEWS_WEIGHTS['政策']
            elif any(k in title for k in NEWS_EARNINGS_KEYWORDS):
                w = NEWS_WEIGHTS['业绩']
            elif any(k in title for k in NEWS_PRODUCT_KEYWORDS):
                w = NEWS_WEIGHTS['产品']
            elif any(k in title for k in NEWS_FUND_KEYWORDS):
                w = NEWS_WEIGHTS['资金']
            elif any(k in title for k in NEWS_RUMOR_KEYWORDS):
                w = NEWS_WEIGHTS['传闻']
            else:
                w = NEWS_WEIGHTS['一般']

            # 热度加成
            hot = news.get('hot_value', 0)
            if hot > 1000:
                w *= 1.5
            elif hot > 500:
                w *= 1.2

            # 应用质量折扣
            w *= quality_factor

            # 命中大类板块
            for sector, keywords in SECTOR_NEWS_KEYWORDS.items():
                for kw in keywords:
                    if kw in title:
                        scores[sector] = scores.get(sector, 0) + w
                        break

        # 归一化到100分
        if scores:
            max_score = max(scores.values())
            if max_score > 0:
                scores = {k: v / max_score * 100 for k, v in scores.items()}

        self.news_scores = scores
        return scores

    def calc_sector_scores_from_bk(self) -> Dict[str, Dict]:
        """
        直接用BK板块数据评分（不依赖名称匹配）
        返回 {bk_code: {perf_rank, flow_rank, change_pct, flow_amount}}
        """
        if not self.sectors_raw:
            self.fetch_sectors()

        sectors = self.sectors_raw
        if not sectors:
            return {}

        # 按涨跌幅排序
        by_change = sorted(sectors, key=lambda x: safe_float(x.get('f3'), 0), reverse=True)
        n = len(by_change)
        for rank, s in enumerate(by_change):
            s['_perf_score'] = (n - rank) / n * 100

        # 按资金流排序
        by_flow = sorted(sectors, key=lambda x: safe_float(x.get('f62'), 0), reverse=True)
        m = len(by_flow)
        for rank, s in enumerate(by_flow):
            s['_flow_score'] = (m - rank) / m * 100

        result = {}
        for s in sectors:
            bk = s.get('f12', '')
            if not bk:
                continue
            result[bk] = {
                'name': s.get('f14', ''),
                'change_pct': safe_float(s.get('f3', 0)),
                'net_flow': safe_float(s.get('f62', 0)),
                'perf_score': s.get('_perf_score', 50),
                'flow_score': s.get('_flow_score', 50),
            }

        self.bk_scores = result
        return result

    def calc_tech_score_for_bk(self, bk_code: str) -> float:
        """
        获取板块TOP成分股的技术信号
        基于成分股涨跌幅给出趋势评分
        """
        constituents = self.fetch_sector_constituents(bk_code, top_n=3)
        if not constituents:
            return 50

        scores = []
        for stock in constituents:
            chg = stock.get('change_pct', 0)
            if chg > 3:
                scores.append(100)
            elif chg > 1.5:
                scores.append(80)
            elif chg > 0.5:
                scores.append(65)
            elif chg > 0:
                scores.append(55)
            elif chg > -0.5:
                scores.append(45)
            elif chg > -1.5:
                scores.append(30)
            elif chg > -3:
                scores.append(15)
            else:
                scores.append(5)

        return sum(scores) / len(scores) if scores else 50

    def calc_consistency_score_for_bk(self, bk_code: str) -> float:
        """
        计算板块成分股涨跌一致性
        获取更多成分股（10只），看有多少在上涨
        80%+上涨 = 强一致（100分）
        50-80%上涨 = 中等一致（60分）
        <50%上涨 = 分化（20分）
        下跌板块反过来判断（跌的越多越一致）
        """
        constituents = self.fetch_sector_constituents(bk_code, top_n=3)
        if not constituents:
            return 50

        n = len(constituents)
        up_count = sum(1 for c in constituents if c.get('change_pct', 0) > 0)
        down_count = sum(1 for c in constituents if c.get('change_pct', 0) < 0)
        flat_count = n - up_count - down_count

        up_ratio = up_count / n
        down_ratio = down_count / n

        # 判断板块整体方向
        avg_change = sum(c.get('change_pct', 0) for c in constituents) / n

        if avg_change > 0.5:  # 强势上涨板块
            # 上涨家数越多，一致性越强
            if up_ratio >= 0.8:
                return 100
            elif up_ratio >= 0.6:
                return 80
            elif up_ratio >= 0.5:
                return 60
            else:
                return 30  # 涨了但很多在跌，分化
        elif avg_change < -0.5:  # 弱势下跌板块
            # 下跌家数越多，一致性越强（抛售信号）
            if down_ratio >= 0.8:
                return 100
            elif down_ratio >= 0.6:
                return 80
            elif down_ratio >= 0.5:
                return 60
            else:
                return 30
        else:  # 震荡板块，看是否齐涨共跌
            if up_ratio >= 0.7 or down_ratio >= 0.7:
                return 80
            elif up_ratio >= 0.5 or down_ratio >= 0.5:
                return 60
            else:
                return 40  # 严重分化

    def calc_all_scores(self, top_n_for_constituents: int = 30) -> Dict[str, Dict]:
        """
        计算所有维度的综合评分。

        性能优化：tech/consistency 两个维度需要对每个板块发起 HTTP 请求抓成分股，
        开销较大（约 200ms/次 × 板块数）。为避免超时，先按 perf+flow 初筛出
        top_n_for_constituents 个板块，只对这些板块发起成分股请求；
        其余板块 tech=50、consistency=50（中性分，不影响排名精度）。
        """
        news = self.calc_news_score()
        bk_data = self.calc_sector_scores_from_bk()

        # 获取新闻情绪分数（基于关键词+真实新闻）
        sentiment_scores = {}
        try:
            sentiment_scores = _SentimentScorerWrapper.get_sector_sentiment()
        except Exception:
            pass

        # ── 性能优化：只对 perf+flow Top-N 板块抓成分股 ──────────────
        # 先按 perf_score + flow_score 粗排，取前 top_n_for_constituents
        sorted_by_pf = sorted(
            bk_data.items(),
            key=lambda x: x[1].get('perf_score', 0) + x[1].get('flow_score', 0),
            reverse=True,
        )
        constituent_candidates = {bk for bk, _ in sorted_by_pf[:top_n_for_constituents]}
        _log('INFO', f'calc_all_scores: fetching constituents for top {len(constituent_candidates)}/{len(bk_data)} sectors')

        # 为每个BK板块计算技术分和综合分
        bk_final = {}
        for bk, info in bk_data.items():
            perf = info['perf_score']
            flow = info['flow_score']
            # 只对初筛板块发 HTTP 请求；其余给中性分（50），不影响最终 Top 结果
            if bk in constituent_candidates:
                tech = self.calc_tech_score_for_bk(bk)
                consistency = self.calc_consistency_score_for_bk(bk)
            else:
                tech = 50
                consistency = 50

            # 新闻分：尝试从板块名匹配
            bk_name = info.get('name', '')
            news_score = 0
            for sector, kws in SECTOR_NEWS_KEYWORDS.items():
                for kw in kws:
                    if kw in bk_name:
                        news_score = news.get(sector, 0)
                        break
                if news_score > 0:
                    break

            # 情绪分数：新闻情绪打分器提供额外情绪信号
            sentiment_bonus = 0.0
            for sector, sent_score in sentiment_scores.items():
                for kw in SECTOR_NEWS_KEYWORDS.get(sector, []):
                    if kw in bk_name:
                        # 情绪分数标准化到 ±10 分（新闻权重15%中的一部分）
                        sentiment_bonus += sent_score * 0.10
                        break

            total = (
                news_score * self.WEIGHT_NEWS +
                perf * self.WEIGHT_SECTOR +
                flow * self.WEIGHT_FLOW +
                tech * self.WEIGHT_TECH +
                consistency * self.WEIGHT_CONSISTENCY +
                sentiment_bonus
            )

            base_info = {
                'name': bk_name,
                'total': total,
                'news': news_score,
                'perf': perf,
                'flow': flow,
                'tech': tech,
                'consistency': consistency,
                'sentiment': round(sentiment_bonus, 2),
                'change_pct': info['change_pct'],
                'net_flow': info['net_flow'],
            }
            bk_final[bk] = _regime_modulate(base_info, getattr(self, 'regime', 'CALM'))

        self.sector_scores = bk_final
        return bk_final

    def get_top_bk_sectors(self, top_n: int = 5) -> List[Tuple[str, Dict]]:
        """获取评分最高的N个BK板块"""
        if not self.sector_scores:
            self.calc_all_scores()

        sorted_sectors = sorted(
            self.sector_scores.items(),
            key=lambda x: x[1].get('total', 0),
            reverse=True
        )
        return sorted_sectors[:top_n]

    def select_stocks(self, top_n: int = 5, regime: str = None) -> List[str]:
        """
        最终选股入口
        1. 获取TOP板块
        2. 每个板块取TOP成分股
        3. 合并，去重
        """
        top_bks = self.get_top_bk_sectors(top_n)
        selected = []
        seen_codes = set()

        for bk, score_info in top_bks:
            total_score = score_info.get('total', 0)
            if total_score < 20:
                continue

            # 获取板块TOP成分股
            constituents = self.fetch_sector_constituents(bk, top_n=3)
            for stock in constituents:
                code = stock.get('full_code', '')
                if code and code not in seen_codes and len(selected) < top_n * 3:
                    selected.append(code)
                    seen_codes.add(code)

            if len(selected) >= top_n * 3:
                break

        # 不足时用宽基ETF填充
        for etf in FALLBACK_ETFS:
            if etf not in seen_codes and len(selected) < top_n:
                selected.append(etf)
                seen_codes.add(etf)

        return selected[:top_n]

    def get_stock_with_context(self, top_n: int = 5) -> List[Dict]:
        """返回选股结果及完整上下文"""
        stocks = self.select_stocks(top_n)
        top_bks = dict(self.get_top_bk_sectors(top_n))

        result = []
        for code in stocks:
            price_data = self.fetch_etf_price(code)
            # 找所属板块
            bk_info = None
            for bk, info in top_bks.items():
                cons = self.fetch_sector_constituents(bk, top_n=3)
                if any(s.get('full_code') == code for s in cons):
                    bk_info = info
                    break

            result.append({
                'code': code,
                'name': price_data.get('name', code) if price_data else code,
                'price': price_data.get('price', '-') if price_data else '-',
                'change_pct': f"{price_data.get('change_pct', 0):.2f}%" if price_data else '-',
                'sector_name': bk_info.get('name', '宽基') if bk_info else '宽基',
                'total_score': bk_info.get('total', 0) if bk_info else 0,
                'perf_score': bk_info.get('perf', 0) if bk_info else 0,
                'flow_score': bk_info.get('flow', 0) if bk_info else 0,
                'tech_score': bk_info.get('tech', 0) if bk_info else 0,
                'news_score': bk_info.get('news', 0) if bk_info else 0,
                'change': bk_info.get('change_pct', 0) if bk_info else 0,
            })
        return result

    def get_news_summary(self, limit: int = 10) -> str:
        """格式化新闻列表"""
        if not self.news_cache:
            self.fetch_market_news()

        if not self.news_cache:
            return "暂无资讯"

        lines = []
        for i, news in enumerate(self.news_cache[:limit], 1):
            title = news.get('title', '')

            # 新闻类型
            if any(k in title for k in NEWS_POLICY_KEYWORDS):
                t = '政策'
            elif any(k in title for k in NEWS_EARNINGS_KEYWORDS):
                t = '业绩'
            elif any(k in title for k in NEWS_PRODUCT_KEYWORDS):
                t = '产品'
            elif any(k in title for k in NEWS_FUND_KEYWORDS):
                t = '资金'
            elif any(k in title for k in NEWS_RUMOR_KEYWORDS):
                t = '传闻'
            else:
                t = '一般'

            # 关联板块
            sectors = []
            for sec, kws in SECTOR_NEWS_KEYWORDS.items():
                if any(kw in title for kw in kws):
                    sectors.append(sec)

            type_tag = f'[{t}]'
            sector_tag = f"[{','.join(sectors)}]" if sectors else ""
            lines.append(f"{i}. {type_tag}{sector_tag} {title}")

        return '\n'.join(lines)

    def to_report(self, top_n: int = 5) -> str:
        """生成完整报告"""
        top_bks = self.get_top_bk_sectors(top_n)
        news_summary = self.get_news_summary(limit=10)

        lines = []
        lines.append('【二、今日要闻】')
        lines.append('-' * 50)
        lines.append('(来源: 东方财富)')
        lines.append('')
        lines.append(news_summary)
        lines.append('')
        lines.append('【三、热门板块】(多维度综合评分)')
        lines.append('-' * 50)
        for bk, info in top_bks:
            name = info.get('name', bk)
            total = info.get('total', 0)
            chg = info.get('change_pct', 0)
            n = info.get('news', 0)
            perf = info.get('perf', 0)
            flow = info.get('flow', 0)
            tech = info.get('tech', 0)
            cons = info.get('consistency', 0)
            lines.append(f"- {name} [{bk}]: 涨幅{chg:+.2f}% 综合{total:.1f}分")
            lines.append(f"  新闻:{n:.0f} + 行情:{perf:.0f} + 资金:{flow:.0f} + 技术:{tech:.0f} + 一致性:{cons:.0f}")

        lines.append('')
        lines.append('【四、选股结果】')
        lines.append('-' * 50)
        for info in self.get_stock_with_context(top_n):
            lines.append(
                f"- {info['name']} ({info['code']}) [{info['change_pct']}]"
                f" - {info['sector_name']} 综合:{info['total_score']:.0f}分"
            )

        return '\n'.join(lines)



# ─── P6.3 环境感知 ───────────────────────────────────────────────────────

DEFENSIVE_SECTORS = {'电力', '医药', '医疗', '消费', '银行', '食品', '家电', '农业'}
MOMENTUM_SECTORS = {'AI', '芯片', '半导体', '5G', '新能源', '军工', '新能源汽车',
                    '人工智能', 'eVTOL', '机器人', '算力', '光模块', '游戏'}

def _regime_modulate(score_dict: dict, regime: str) -> dict:
    """Modulate sector score based on market regime."""
    import copy
    d = copy.copy(score_dict)
    total = d.get('total', 0)
    boost = 0

    if regime == 'BULL':
        for m in MOMENTUM_SECTORS:
            if m in d.get('name', ''):
                total *= 1.2
                boost = 1
                break
    elif regime == 'BEAR':
        defended = any(ds in d.get('name', '') for ds in DEFENSIVE_SECTORS)
        total *= 1.2 if defended else 0.85
        boost = 1 if defended else -1
    elif regime == 'VOLATILE':
        total *= 0.80
        boost = -1

    d['total'] = total
    d['regime_boost'] = boost
    return d


if __name__ == '__main__':
    print('=' * 60)
    print('  Multi-dimension Dynamic Stock Selector V2')
    print('=' * 60)
    print()

    sel = DynamicStockSelectorV2()

    print('[1/4] Fetching market news...')
    news = sel.fetch_market_news(30)
    print(f'    Got {len(news)} news items')

    print('[2/4] Fetching sector data...')
    sectors = sel.fetch_sectors()
    print(f'    Got {len(sectors)} sectors')

    print('[3/4] Calculating multi-dimension scores...')
    scores = sel.calc_all_scores()
    top_bks = sel.get_top_bk_sectors(5)
    print('    TOP5 BK sectors:')
    for bk, info in top_bks:
        t = info.get('total', 0)
        chg = info.get('change_pct', 0)
        n = info.get('news', 0)
        perf = info.get('perf', 0)
        flow = info.get('flow', 0)
        tech = info.get('tech', 0)
        cons = info.get('consistency', 0)
        print(f'      {info.get("name","?")} [{bk}]: chg={chg:+.2f}% total={t:.1f}  news={n:.0f} perf={perf:.0f} flow={flow:.0f} tech={tech:.0f} cons={cons:.0f}')

    print()
    print('[4/4] Stock selection...')
    selected = sel.select_stocks(5)
    print(f'    Selected: {selected}')
    print()
    for code in selected:
        pd = sel.fetch_etf_price(code)
        if pd:
            print(f'    {pd.get("name","?")} ({code}): {pd.get("price")} ({pd.get("change_pct"):+.2f}%)')
