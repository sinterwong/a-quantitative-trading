"""
fetcher.py — IPO Stars 数据获取层
====================================
已接入数据源：
    - HKEX 官网 IPO 日历（HTML 解析）→ fetch_upcoming_ipos()
    - HKEX 招股书 PDF（PyMuPDF 解析）→ fetch_prospectus()
    - HKEX 分配结果 PDF（PyMuPDF 解析）→ fetch_allotment_results()
    - 新浪恒生科技指数（hq.sinajs.cn）→ fetch_market_context()

待接入：
    - 券商 API（富途 OpenAPI）→ fetch_subscription_data()
    - 稳价人历史战绩 → fetch_stabilizer_history()
"""

import io
import re
import ssl
import logging
import urllib.request
from html.parser import HTMLParser
from typing import List, Dict, Optional

logger = logging.getLogger('ipo_stars.fetcher')

# ─── 常量 ────────────────────────────────────────────────────────

HKEX_NEW_LISTINGS_URL = (
    'https://www2.hkexnews.hk/New-Listings/New-Listing-Information'
    '/Main-Board?sc_lang=en'
)

SINA_HSTECH_URL = 'https://hq.sinajs.cn/list=rt_hkHSTECH'

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_HEADERS = {'User-Agent': 'Mozilla/5.0'}


# ─── HTML Parser ─────────────────────────────────────────────────

class _HKEXTableParser(HTMLParser):
    """解析 HKEX New Listings 页面的 IPO 表格。

    表格结构（5 列）：
        Stock Code | Stock Name | New Listing Announcements | Prospectuses | Allotment Results

    每列的 PDF 下载链接在 <a href="..."> 中。
    """

    def __init__(self):
        super().__init__()
        self._in_tbody = False
        self._in_td = False
        self._col_idx = 0
        self._current_row: Dict = {}
        self._current_text = ''
        self._current_links: List[str] = []
        self.rows: List[Dict] = []

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)
        if tag == 'tbody':
            self._in_tbody = True
        elif tag == 'tr' and self._in_tbody:
            self._current_row = {}
            self._col_idx = 0
        elif tag == 'td' and self._in_tbody:
            self._in_td = True
            self._current_text = ''
            self._current_links = []
        elif tag == 'a' and self._in_td:
            href = attr_dict.get('href', '')
            if href and href.endswith('.pdf'):
                self._current_links.append(href)

    def handle_endtag(self, tag):
        if tag == 'tbody':
            self._in_tbody = False
        elif tag == 'td' and self._in_tbody:
            self._in_td = False
            text = self._current_text.strip()
            links = self._current_links

            if self._col_idx == 0:
                self._current_row['code'] = text.strip()
            elif self._col_idx == 1:
                # 清理名称中的换行和多余空白
                name = re.sub(r'\s+', ' ', text).strip()
                # 去除尾部标记如 " - B" " - P"
                self._current_row['name'] = name
            elif self._col_idx == 2:
                if links:
                    self._current_row['announcement_url'] = links[0]
            elif self._col_idx == 3:
                if links:
                    self._current_row['prospectus_url'] = links[0]
            elif self._col_idx == 4:
                if links:
                    self._current_row['allotment_url'] = links[0]

            self._col_idx += 1
        elif tag == 'tr' and self._in_tbody and self._current_row.get('code'):
            self.rows.append(self._current_row)

    def handle_data(self, data):
        if self._in_td:
            self._current_text += data


# ─── 招股书 PDF 解析 ─────────────────────────────────────────────

def _download_pdf(url: str) -> bytes:
    """下载 PDF 并返回原始字节。"""
    req = urllib.request.Request(url, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as resp:
        return resp.read()


def _llm_extract_prospectus_fields(
    pdf_bytes: bytes,
    existing: Dict,
    llm_service=None,
    timeout: int = 30,
) -> Dict:
    """
    用 LLM 从招股书 PDF 中抽取正则未拿到的字段（stabilizer / sponsor / cornerstone / industry）。

    策略：
        1. 用关键词在 PDF 中定位关键页（避免把整本招股书丢给 LLM）
        2. 提取每个关键词附近的 1500 字符上下文
        3. 拼成 prompt content，调用 ipo_prospectus 任务
        4. 把缺失字段合并回 existing

    Args:
        pdf_bytes: 招股书 PDF 字节
        existing: 正则已抽取的字段 dict
        llm_service: 可选 LLMService 实例。若为 None 则从 factory 创建
        timeout: LLM 单次调用超时（秒）

    Returns:
        增量字段 dict（仅包含 LLM 新抽取的非空字段）
    """
    import fitz
    import json

    # 仅当某些关键字段缺失时才动用 LLM（节省成本）
    needs = []
    if not existing.get('stabilizer'):
        needs.append('stabilizer')
    if not existing.get('sponsor') or _is_garbage_sponsor(existing.get('sponsor', '')):
        needs.append('sponsor')
    if not existing.get('cornerstone_pct') or not existing.get('cornerstone_names'):
        needs.append('cornerstone')
    if not existing.get('listing_date'):
        needs.append('listing_date')

    if not needs:
        logger.info('All key fields extracted by regex, skipping LLM fallback')
        return {}

    # 拿 LLM service
    if llm_service is None:
        try:
            from backend.services.llm.factory import create_llm_service
            llm_service = create_llm_service()
        except Exception as e:
            logger.warning('LLM service unavailable, skipping LLM extraction: %s', e)
            return {}

    if not getattr(llm_service, 'is_available', False):
        logger.info('LLM provider not available, skipping LLM extraction')
        return {}

    # 选关键页（每个关键词只取第一处 + 上下文 1500 字符）
    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    keywords = [
        'Stabilizing Manager', 'Stabilization Manager',
        'Sole Sponsor', 'Joint Sponsors',
        'Cornerstone Investors', 'cornerstone investor',
        'Dealings in', 'commence on',
        'Industry Overview', 'INDUSTRY OVERVIEW',
    ]
    snippets: List[str] = []
    seen_keys = set()
    max_pages = min(len(doc), 300)

    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in seen_keys:
            continue
        for i in range(max_pages):
            text = doc[i].get_text()
            idx = text.lower().find(kw_lower)
            if idx >= 0:
                start = max(0, idx - 200)
                end = min(len(text), idx + 1500)
                snippet = text[start:end].strip()
                if snippet and len(snippet) > 50:
                    snippets.append(f'[Page {i+1}, keyword: {kw}]\n{snippet}')
                    seen_keys.add(kw_lower)
                break
        if len(snippets) >= 8:
            break

    doc.close()

    if not snippets:
        logger.info('No useful snippets found for LLM extraction')
        return {}

    content = '\n\n---\n\n'.join(snippets)
    # 限制 token 用量（粗略估计 1 字符 ≈ 0.5 token）
    if len(content) > 12000:
        content = content[:12000]

    logger.info(
        'Calling LLM for prospectus extraction: needs=%s, content=%d chars',
        needs, len(content),
    )

    try:
        raw = llm_service._call_llm(
            task='ipo_prospectus',
            content=content,
            timeout=timeout,
        )
        parsed = llm_service._parse_json(raw)
    except Exception as e:
        logger.warning('LLM extraction failed: %s', e)
        return {}

    # 把 LLM 返回的非 null 字段合并回 result（不覆盖正则已拿到的非空值）
    increments: Dict = {}
    for key in ('stabilizer', 'sponsor', 'cornerstone_names', 'industry', 'listing_date'):
        val = parsed.get(key)
        if val and isinstance(val, str) and val.strip():
            existing_val = existing.get(key)
            if not existing_val or _is_garbage_sponsor(str(existing_val)):
                increments[key] = val.strip()

    cs_pct = parsed.get('cornerstone_pct')
    if cs_pct is not None and not existing.get('cornerstone_pct'):
        try:
            cs_pct_f = float(cs_pct)
            if 0 < cs_pct_f <= 1.0:
                increments['cornerstone_pct'] = cs_pct_f
        except (ValueError, TypeError):
            pass

    logger.info('LLM extracted %d new fields: %s', len(increments), list(increments.keys()))
    return increments


def _is_garbage_sponsor(s: str) -> bool:
    """判断正则抽到的 sponsor 是否是噪音（标题文本而非公司名）。"""
    if not s:
        return True
    s_lower = s.lower()
    if any(bad in s_lower for bad in [
        'overall coordinators', 'joint global coordinators',
        'joint sponsors\n', 'sole sponsor\n',
    ]):
        return True
    # 必须包含至少一个公司后缀
    return not any(suf in s_lower for suf in [
        'limited', 'ltd', 'securities', 'capital', 'inc', 'corp',
        'corporation', 'holdings', 'group',
    ])


def _parse_prospectus_pdf(pdf_bytes: bytes) -> Dict:
    """用 PyMuPDF 从招股书 PDF 中提取关键字段。

    提取字段：
        - offer_price_low / offer_price_high（发行价区间）
        - listing_date（上市日期）
        - sponsor（保荐人 / 联席保荐人）
        - stabilizer（稳价人）
        - cornerstone_names / cornerstone_pct（基石投资者）
        - issue_size（发行规模）
    """
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    result: Dict = {}

    # 只读前 300 页（招股书通常关键信息在前 250 页内）
    max_pages = min(len(doc), 300)

    # 缓存页面文本（避免重复读取）
    page_texts: Dict[int, str] = {}

    def get_text(page_idx: int) -> str:
        if page_idx not in page_texts:
            if page_idx < len(doc):
                page_texts[page_idx] = doc[page_idx].get_text()
            else:
                page_texts[page_idx] = ''
        return page_texts[page_idx]

    def search_pages(keyword: str, start: int = 0, end: int = 50) -> Optional[str]:
        """在指定页范围内搜索关键词，返回包含该词的页面文本。"""
        end = min(end, max_pages)
        kw_lower = keyword.lower()
        for i in range(start, end):
            text = get_text(i)
            if kw_lower in text.lower():
                return text
        return None

    # 1) 发行价 — 优先识别区间（HK$X to HK$Y），其次单价（HK$X per H Share）
    #    搜索范围扩到前 30 页（招股书摘要页有时不在前 5 页）
    for i in range(min(30, max_pages)):
        text = get_text(i)
        # 模式1: HK$24.00 to HK$30.00（区间）
        m = re.search(
            r'HK\$\s*([\d,.]+)\s*(?:to|至)\s*HK\$\s*([\d,.]+)',
            text, re.IGNORECASE,
        )
        if m:
            result['offer_price_low'] = float(m.group(1).replace(',', ''))
            result['offer_price_high'] = float(m.group(2).replace(',', ''))
            break
        # 模式2: 招股价介乎每股 X 港元至 Y 港元（中文区间）
        m = re.search(
            r'(?:每股|每股H股)\s*([\d,.]+)\s*港元\s*至\s*([\d,.]+)\s*港元',
            text,
        )
        if m:
            result['offer_price_low'] = float(m.group(1).replace(',', ''))
            result['offer_price_high'] = float(m.group(2).replace(',', ''))
            break
        # 模式3: 单价定价（"Offer Price\nHK$10.50 per H Share"）
        # 必须紧跟 "Offer Price" 上下文，避免误抓其他金额
        m = re.search(
            r'Offer\s+Price[^\n]*\n\s*HK\$\s*([\d,.]+)\s*(?:per\s+(?:H\s+)?Share)?',
            text, re.IGNORECASE,
        )
        if m:
            price = float(m.group(1).replace(',', ''))
            if 0.5 <= price <= 10000:  # 港股招股价合理区间
                result['offer_price_low'] = price
                result['offer_price_high'] = price
                break

    # 2) 上市日期 — 搜索范围扩到前 100 页（DEALING IN 章节常在 80~90 页）
    for i in range(min(100, max_pages)):
        text = get_text(i)
        # "Dealing in ... to commence on Wednesday, May 11, 2026"
        m = re.search(
            r'(?:commence|expected)\s+(?:to\s+commence\s+)?'
            r'(?:at\s+\d{1,2}:\d{2}\s*(?:a\.m\.|p\.m\.)?\s+)?'
            r'(?:on\s+)?'
            r'(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'
            r',?\s+(\w+\s+\d{1,2},?\s+\d{4})',
            text, re.IGNORECASE,
        )
        if m:
            raw_date = m.group(1).replace(',', '')
            try:
                from datetime import datetime as _dt
                dt = _dt.strptime(raw_date, '%B %d %Y')
                result['listing_date'] = dt.strftime('%Y-%m-%d')
            except ValueError:
                result['listing_date'] = raw_date
            break
        # 中文："预期于2026年5月11日开始买卖"
        m = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日.*?(?:开始买卖|上市)', text)
        if m:
            result['listing_date'] = f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}'
            break

    # 3) 保荐人 — 通常在首页或前 3 页
    for i in range(min(5, max_pages)):
        text = get_text(i)
        # "Joint Sponsors" / "Sole Sponsor"
        m = re.search(
            r'(?:Joint\s+)?Sponsors?\s*\n(.+?)(?:\n\n|\nJoint\s|'
            r'\nUnderwriter|\nOverall|\nLead|\nBook)',
            text, re.DOTALL | re.IGNORECASE,
        )
        if m:
            sponsors_raw = m.group(1).strip()
            # 提取每行非空文本作为保荐人
            sponsor_lines = [
                ln.strip() for ln in sponsors_raw.split('\n')
                if ln.strip() and not ln.strip().startswith('(')
            ]
            result['sponsor'] = ', '.join(sponsor_lines[:5])
            break

    # 4) 稳价人 — 通常在 Definitions 章节（30~50 页）或 Underwriting 章节（260+ 页）
    #    Definitions 章节常见格式（跨多行）：
    #        "Stabilizing Manager"
    #        CLSA Limited
    for i in range(min(150, max_pages)):
        text = get_text(i)
        # 模式1: 引号包裹的术语定义（"Stabilizing Manager"\nXXX Limited）
        m = re.search(
            r'["""]Stabili[sz](?:ing|ation)\s+Manager["""]\s*\n+\s*([^\n]+)',
            text, re.IGNORECASE,
        )
        if m:
            stabilizer = m.group(1).strip()
            stabilizer = re.sub(r'[,;.]+$', '', stabilizer).strip()
            # 必须像公司名（含 Limited/Securities/Capital 等）
            if 3 < len(stabilizer) < 100 and re.search(
                r'(Limited|Ltd|Securities|Capital|Inc|Corp|Group|Holdings|證券|有限)',
                stabilizer, re.IGNORECASE,
            ):
                result['stabilizer'] = stabilizer
                break
        # 模式2: 紧跟冒号或换行（Stabilizing Manager: XXX Limited）
        m = re.search(
            r'Stabili[sz](?:ing|ation)\s+Manager[\s:]*\n*\s*([A-Z][A-Za-z &.,\-]+(?:Limited|Ltd|Securities|Capital))',
            text, re.IGNORECASE,
        )
        if m:
            stabilizer = m.group(1).strip()
            stabilizer = re.sub(r'[,;.]+$', '', stabilizer).strip()
            if 3 < len(stabilizer) < 100:
                result['stabilizer'] = stabilizer
                break

    # 5) 基石投资者 — 通常在 150~250 页
    for start in range(0, max_pages, 50):
        text = search_pages('Cornerstone Investor', start, start + 50)
        if text and 'cornerstone_pct' not in result:
            # 提取基石占比百分比
            m = re.search(
                r'(?:approximately|about)\s+([\d.]+)%\s*'
                r'(?:of\s+the\s+(?:total\s+)?(?:Offer|Global)\s+'
                r'(?:Shares|Size))',
                text, re.IGNORECASE,
            )
            if m:
                result['cornerstone_pct'] = float(m.group(1)) / 100.0

            # 提取基石投资者名称
            names = re.findall(
                r'(?:^|\n)\s*(?:\d+[.)]\s*)?([A-Z][A-Za-z\s&,.()]+?'
                r'(?:Limited|Ltd|Inc|Corp|Fund|Capital|Investment|Holdings))',
                text,
            )
            if names:
                # 去重 + 取前 10 个
                seen = set()
                unique_names = []
                for n in names:
                    clean = n.strip()
                    if clean not in seen and len(clean) > 5:
                        seen.add(clean)
                        unique_names.append(clean)
                result['cornerstone_names'] = ','.join(unique_names[:10])

    # 5b) 每手股数 — 通常在 "DEALING IN H SHARES" 章节
    #     格式："Shares will be traded in board lots of 500 Shares each"
    for i in range(min(150, max_pages)):
        text = get_text(i)
        m = re.search(
            r'(?:in\s+)?board\s+lots?\s+of\s+([\d,]+)\s+(?:H\s+)?Shares?',
            text, re.IGNORECASE,
        )
        if m:
            try:
                result['lot_size'] = int(m.group(1).replace(',', ''))
                break
            except ValueError:
                pass

    # 6) 发行股数 → 推算发行规模
    for i in range(min(10, max_pages)):
        text = get_text(i)
        # "333,334,000 Shares" / "33,333,400 H Shares"
        m = re.search(
            r'([\d,]+)\s+(?:H\s+)?(?:Shares?|股)',
            text, re.IGNORECASE,
        )
        if m and 'issue_shares' not in result:
            shares_str = m.group(1).replace(',', '')
            try:
                shares = int(shares_str)
                if shares > 1_000_000:
                    result['issue_shares'] = shares
                    # 如有价格区间可算出发行规模（亿港元）
                    mid_price = (
                        result.get('offer_price_low', 0)
                        + result.get('offer_price_high', 0)
                    ) / 2
                    if mid_price > 0:
                        result['issue_size'] = round(
                            shares * mid_price / 1e8, 2
                        )
            except ValueError:
                pass

    doc.close()

    # 清洗：sponsor 抓到标题文本时直接丢弃，留空给 LLM 兜底
    if _is_garbage_sponsor(result.get('sponsor', '')):
        result.pop('sponsor', None)

    # 清洗：cornerstone_names 必须像投资机构名（含 Limited/Capital/Fund 等且不含明显标点段落）
    cs_names = result.get('cornerstone_names', '')
    if cs_names:
        bad_keywords = [
            'underwriting agreement', 'cornerstone investment',
            'agreement', 'as defined', '\n',
        ]
        if any(bk in cs_names.lower() for bk in bad_keywords) or len(cs_names) > 200:
            result.pop('cornerstone_names', None)

    return result


def _parse_allotment_pdf(pdf_bytes: bytes) -> Dict:
    """从分配结果 PDF 提取关键数据。

    分配结果 PDF 结构（基于 HKEX 标准格式）：
        - Page 1-2: Final Offer Price, Number of Offer Shares, Stock code
        - Page 3-4: Subscription level (超购倍数), Claw-back, 公开/国际发售分配比例
        - Page 5+:  Cornerstone investors 明细
    """
    import fitz

    doc = fitz.open(stream=pdf_bytes, filetype='pdf')
    result: Dict = {}
    max_pages = min(len(doc), 30)

    # 合并前 15 页文本供搜索
    all_text = ''
    for i in range(min(15, max_pages)):
        all_text += doc[i].get_text() + '\n'

    # 1) 最终定价
    m = re.search(
        r'Final\s+Offer\s+Price\s*[:\s]*HK\$\s*([\d,.]+)',
        all_text, re.IGNORECASE,
    )
    if m:
        result['offer_price_final'] = float(m.group(1).replace(',', ''))

    # 2) 股票代码
    m = re.search(r'Stock\s+code\s*[:\s]*(\d{4,5})', all_text, re.IGNORECASE)
    if m:
        result['code'] = m.group(1).zfill(5)

    # 3) 上市日期
    m = re.search(
        r'(?:Dealings?\s+commencement\s+date|Listing\s+Date)\s*[:\s]*'
        r'(\w+\s+\d{1,2},?\s+\d{4})',
        all_text, re.IGNORECASE,
    )
    if m:
        raw_date = m.group(1).replace(',', '')
        try:
            from datetime import datetime as _dt
            dt = _dt.strptime(raw_date, '%B %d %Y')
            result['listing_date'] = dt.strftime('%Y-%m-%d')
        except ValueError:
            result['listing_date'] = raw_date

    # 4) 公开发售超购倍数 — "Subscription level  399.08 times"
    m = re.search(
        r'(?:HONG\s+KONG\s+PUBLIC\s+OFFERING.*?)?'
        r'Subscription\s+level\s*[:\s]*([\d,.]+)\s*times',
        all_text, re.IGNORECASE | re.DOTALL,
    )
    if m:
        result['public_offer_multiple'] = float(m.group(1).replace(',', ''))

    # 5) 回拨 — "Claw-back triggered  Yes / N/A"
    m = re.search(
        r'Claw-?back\s+triggered\s*[:\s]*(Yes|No|N/A)',
        all_text, re.IGNORECASE,
    )
    if m:
        result['clawback_triggered'] = m.group(1).upper() not in ('NO', 'N/A')

    # 6) 公开发售占比 — "% of Offer Shares under the Hong Kong Public Offering
    #    to the Global Offering  10.00%"
    m = re.search(
        r'%\s+of\s+Offer\s+Shares\s+under\s+the\s+Hong\s+Kong\s+'
        r'Public\s+Offering.*?\n\s*([\d.]+)%',
        all_text, re.IGNORECASE | re.DOTALL,
    )
    if m:
        result['hk_public_offering_pct'] = float(m.group(1)) / 100.0

    # 7) 基石投资者总占比 — 在 Cornerstone Investors 表中找 "Total" 或 "Sub-total"
    m = re.search(
        r'(?:Sub-?total|Total)\s+[\d,]+\s+[\d,]+\s+([\d.]+)%',
        all_text, re.IGNORECASE,
    )
    if m:
        result['cornerstone_pct'] = float(m.group(1)) / 100.0

    # 8) 发行股数
    m = re.search(
        r'Number\s+of\s+Offer\s+Shares\s*[:\s]*([\d,]+)',
        all_text, re.IGNORECASE,
    )
    if m:
        shares_str = m.group(1).replace(',', '')
        try:
            result['offer_shares'] = int(shares_str)
        except ValueError:
            pass

    # 9) 净募资额
    m = re.search(
        r'(?:Net\s+proceeds|Gross\s+proceeds)\s*.*?HK\$\s*([\d,.]+)\s*(million|billion)',
        all_text, re.IGNORECASE,
    )
    if m:
        amount = float(m.group(1).replace(',', ''))
        unit = m.group(2).lower()
        if unit == 'billion':
            amount *= 10  # 十亿 → 亿
        else:
            amount /= 100  # 百万 → 亿
        result['issue_size'] = round(amount, 2)

    doc.close()
    return result


# ─── IPODataFetcher ──────────────────────────────────────────────

class IPODataFetcher:
    """
    港股 IPO 数据获取器。

    已实现：
        - fetch_upcoming_ipos()      → HKEX 官网 HTML
        - fetch_prospectus()         → HKEX 招股书 PDF + PyMuPDF
        - fetch_allotment_results()  → HKEX 分配结果 PDF + PyMuPDF
        - fetch_market_context()     → 新浪恒生科技指数

    待实现：
        - fetch_subscription_data()   → 需富途 Open API
        - fetch_stabilizer_history()  → 需历史数据积累
    """

    def __init__(self, llm_service=None):
        # LLM 兜底：构造时不强制创建，按需在 fetch_prospectus 内 lazy 创建
        self._llm_service = llm_service

    def fetch_upcoming_ipos(self) -> List[Dict]:
        """
        从 HKEX 官网获取最新 IPO 列表。

        Returns:
            [
                {
                    'code': '01236',
                    'name': 'SHENZHEN LDROBOT CO., LTD',
                    'status': 'upcoming',
                    'prospectus_url': 'https://...pdf',
                    'allotment_url': 'https://...pdf',  # 可选
                },
            ]
        """
        html = self._fetch_html(HKEX_NEW_LISTINGS_URL)

        parser = _HKEXTableParser()
        parser.feed(html)

        results = []
        for row in parser.rows:
            code = row.get('code', '').strip()
            if not code or not code.isdigit():
                continue

            # 补齐为 5 位代码
            code = code.zfill(5)

            # 根据是否有 allotment_url 判断状态
            if row.get('allotment_url'):
                status = 'allotted'
            elif row.get('prospectus_url'):
                status = 'subscripting'
            else:
                status = 'upcoming'

            item = {
                'code': code,
                'name': row.get('name', ''),
                'status': status,
            }
            if row.get('prospectus_url'):
                item['prospectus_url'] = row['prospectus_url']
            if row.get('allotment_url'):
                item['allotment_url'] = row['allotment_url']
            if row.get('announcement_url'):
                item['announcement_url'] = row['announcement_url']

            results.append(item)

        logger.info('Fetched %d IPO candidates from HKEX', len(results))
        return results

    def fetch_prospectus(
        self,
        code: str,
        prospectus_url: str = '',
        use_llm: bool = True,
    ) -> Dict:
        """
        下载并解析招股书 PDF，提取关键数据。
        正则提取后，若关键字段缺失，自动用 LLM 兜底（可关闭）。

        Args:
            code: 港股代码（如 '01236'）
            prospectus_url: 招股书 PDF URL（如未提供，需先调用 fetch_upcoming_ipos 获取）
            use_llm: 是否启用 LLM 兜底抽取（默认 True）

        Returns:
            {
                'code': '01236',
                'offer_price_low': 24.0,
                'offer_price_high': 30.0,
                'listing_date': '2026-05-11',
                'sponsor': 'XXX Securities',
                'stabilizer': 'YYY Securities',
                'cornerstone_names': 'GIC,Temasek',
                'cornerstone_pct': 0.35,
                'issue_size': 9.0,
                'lot_size': 500,
            }
        """
        if not prospectus_url:
            raise ValueError(
                f'No prospectus URL for {code}. '
                f'Call fetch_upcoming_ipos() first to get the URL.'
            )

        logger.info('Downloading prospectus PDF for %s ...', code)
        pdf_bytes = _download_pdf(prospectus_url)
        logger.info('Downloaded %d bytes, parsing ...', len(pdf_bytes))

        result = _parse_prospectus_pdf(pdf_bytes)
        result['code'] = code

        # LLM 兜底（仅当关键字段缺失时）
        if use_llm:
            try:
                increments = _llm_extract_prospectus_fields(
                    pdf_bytes, result, llm_service=self._llm_service,
                )
                if increments:
                    result.update(increments)
                    logger.info(
                        'LLM filled fields for %s: %s',
                        code, list(increments.keys()),
                    )
            except Exception as e:
                logger.warning('LLM fallback skipped for %s: %s', code, e)

        logger.info(
            'Parsed prospectus for %s: price=%.2f~%.2f, date=%s, sponsor=%s, stab=%s',
            code,
            result.get('offer_price_low', 0),
            result.get('offer_price_high', 0),
            result.get('listing_date', 'N/A'),
            (result.get('sponsor') or 'N/A')[:30],
            (result.get('stabilizer') or 'N/A')[:30],
        )
        return result

    def fetch_allotment_results(self, code: str, allotment_url: str = '') -> Dict:
        """
        下载并解析分配结果 PDF，提取实际认购数据。

        Args:
            code: 港股代码
            allotment_url: 分配结果 PDF URL（来自 fetch_upcoming_ipos）

        Returns:
            {
                'code': '01187',
                'offer_price_final': 39.33,
                'public_offer_multiple': 399.08,
                'clawback_triggered': False,
                'hk_public_offering_pct': 0.10,
                'cornerstone_pct': 0.3576,
                'listing_date': '2026-05-06',
                'issue_size': 10.62,
            }
        """
        if not allotment_url:
            raise ValueError(
                f'No allotment URL for {code}. '
                f'Call fetch_upcoming_ipos() first to get the URL.'
            )

        logger.info('Downloading allotment results PDF for %s ...', code)
        pdf_bytes = _download_pdf(allotment_url)
        logger.info('Downloaded %d bytes, parsing ...', len(pdf_bytes))

        result = _parse_allotment_pdf(pdf_bytes)
        result['code'] = code

        logger.info(
            'Parsed allotment for %s: price=%.2f, subscription=%.1fx',
            code,
            result.get('offer_price_final', 0),
            result.get('public_offer_multiple', 0),
        )
        return result

    def fetch_subscription_data(self, code: str) -> Dict:
        """
        获取实时认购数据。

        ⚠️ 未实现：券商公开页面全部不可用（地域限制），待富途 Open API 接入。
        """
        raise NotImplementedError(
            "券商孖展数据不可用（地域限制），待富途 Open API 接入"
        )

    def fetch_market_context(self) -> Dict:
        """
        获取大盘环境数据（恒生科技指数）。

        数据源：新浪 hq.sinajs.cn（HTTP 200 已验证）

        Returns:
            {
                'hstech_close': 5089.11,
                'hstech_prev_close': 4969.20,
                'hstech_change_pct': 2.52,
                'hstech_bias_5d': None,   # 需要历史数据计算，暂返回 None
                'hsi_vix': None,          # 数据源待定
            }
        """
        req = urllib.request.Request(
            SINA_HSTECH_URL,
            headers={
                'User-Agent': 'Mozilla/5.0',
                'Referer': 'https://finance.sina.com.cn',
            },
        )
        with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
            raw = resp.read().decode('gbk', errors='replace')

        # 格式: var hq_str_rt_hkHSTECH="HSTECH,名称,现价,昨收,最高,最低,开盘,涨额,涨幅%,..."
        m = re.search(r'"(.+)"', raw)
        if not m:
            logger.warning('Failed to parse Sina HSTECH response')
            return {}

        fields = m.group(1).split(',')
        if len(fields) < 9:
            logger.warning('Unexpected Sina HSTECH field count: %d', len(fields))
            return {}

        try:
            current = float(fields[2])
            prev_close = float(fields[3])
            change_pct = float(fields[8])
        except (ValueError, IndexError) as e:
            logger.warning('Failed to parse Sina HSTECH numbers: %s', e)
            return {}

        result = {
            'hstech_close': current,
            'hstech_prev_close': prev_close,
            'hstech_change_pct': change_pct,
            'hstech_bias_5d': None,
            'hsi_vix': None,
        }

        logger.info(
            'HSTECH: %.2f (prev %.2f, %+.2f%%)',
            current, prev_close, change_pct,
        )
        return result

    def fetch_stabilizer_history(self, stabilizer: str) -> Dict:
        """
        获取稳价人历史战绩。

        ⚠️ 未实现：需历史数据积累，批量回填方案待定。
        """
        raise NotImplementedError("待接入历史 IPO 数据")

    # ─── 内部方法 ─────────────────────────────────────────────────

    @staticmethod
    def _fetch_html(url: str) -> str:
        """获取 HTML 页面内容。"""
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=15, context=_SSL_CTX) as resp:
            return resp.read().decode('utf-8', errors='replace')
