# -*- coding: utf-8 -*-
"""
eastmoney_sector_source.py — 东方财富板块数据源
================================================

提供两类数据（均为东方财富独家数据，无替代源）：
  1. 板块排名 (fetch_sector_rankings)
     - 接口: https://push2.eastmoney.com/api/qt/clist/get
       ?cb=jQuery&pn=1&pz=50&po=1&np=1&ut=b
       &fltt=2&invt=2&fid=f3&fs=m:90+t:2+f:!50
       &fields=f12,f14,f3,f62,f184
  2. 板块成分股 (fetch_sector_constituents)
     - 接口: https://push2.eastmoney.com/api/qt/clist/get
       ?cb=jQuery&pn=1&pz=20&po=1&np=1&ut=b
       &fltt=2&invt=2&fid=f3&fs=b:{bk_code}+f:!50
       &fields=f2,f3,f12,f14,f62,f15,f16,f17,f18

设计原则：
  - 封禁感知：对 ConnectionResetError / RemoteDisconnected / BrokenPipeError
    直接返回空列表，不重试（封禁期间重试无意义，只会在每个板块浪费 3×7s）
  - 文件缓存：板块排名缓存 1 小时，避免每次爬取
  - 标准化输出：统一 SectorData / SectorConstituentData 格式

Usage:
  from core.eastmoney_sector_source import EastmoneySectorSource

  src = EastmoneySectorSource()
  sectors = src.fetch_sector_rankings(limit=50)
  constituents = src.fetch_sector_constituents('BK0716', limit=20)
"""

import json
import logging
from datetime import datetime, timedelta
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from .quote_data_source import (
    SectorConstituentData, SectorData,
    normalize_to_sina,
)

logger = logging.getLogger('eastmoney_sector')

# ─── 常量 ────────────────────────────────────────────────────────────────────

_BASE_URL = 'https://push2.eastmoney.com/api/qt/clist/get'
_CACHE_DIR = Path(__file__).parent.parent / 'scripts' / 'cache'
_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_SECTOR_RANKINGS_CACHE = _CACHE_DIR / 'em_sector_rankings.json'
_SECTOR_CACHE_TTL = timedelta(hours=1)

# 东方财富板块代码前缀映射（用于识别板块类型）
# b: 板块(standard)  m: 行业  N: 概念  H: 地域
_BK_TYPE_MAP = {'b': '标准板块', 'm': '行业板块', 'N': '概念板块', 'H': '地域板块'}

# ─── 请求头 ─────────────────────────────────────────────────────────────────

_HEADERS: Dict[str, str] = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
    'Referer': 'https://quote.eastmoney.com/',
    'Accept': '*/*',
}

_TIMEOUT = 10


# ─── 工具函数 ────────────────────────────────────────────────────────────────


def _get(bk_code: str) -> Optional[dict]:
    """
    对东方财富发送 GET 请求，专门捕获网络层异常。

    封禁特征：ConnectionResetError / RemoteDisconnected / BrokenPipeError
    这类错误立刻返回 None，不重试。
    """
    try:
        params = {
            'cb': 'jQuery',
            'pn': 1,
            'pz': 200,          # 一次拉足够多
            'po': 1,            # 按涨跌幅降序
            'np': 1,
            'ut': 'b',
            'fltt': 2,
            'invt': 2,
            'fid': 'f3',       # 按涨跌幅排序
            'fs': bk_code,
            'fields': 'f2,f3,f4,f5,f6,f7,f8,f10,f12,f14,f15,f16,f17,f18,f20,f21,f23,f24,f25,f22,f11,f62,f184,f66,f69,f72,f75,f78,f81,f84,f87,f204,f205,f124',
        }
        resp = requests.get(_BASE_URL, params=params, headers=_HEADERS, timeout=_TIMEOUT)
        resp.encoding = 'utf-8'
        text = resp.text
        # JSONP 回调格式: jQueryxxx({...})
        if text.startswith('jQuery'):
            try:
                text = text[text.index('(') + 1: text.rindex(')')]
            except ValueError:
                logger.warning("[EM] JSONP 解析失败，响应格式不符预期: %s", text[:100])
                return None
        return json.loads(text)
    except (ConnectionResetError, RemoteDisconnected) as e:
        logger.warning("[EM] 网络层错误（疑似封禁）: %s — %s", bk_code, e)
        return None
    except Exception as e:
        logger.warning("[EM] 请求失败: %s — %s", bk_code, e)
        return None


def _parse_amount(raw: Any) -> float:
    """解析成交额（万元 → 元），兼容 None"""
    if raw is None:
        return 0.0
    try:
        val = float(raw)
        if 0 < val < 1:        # 可能是元→亿元
            return val * 1e8
        elif val < 1e6:        # 可能是万元
            return val * 1e4
        return val             # 已是元
    except (TypeError, ValueError):
        return 0.0


# ─── 数据源类 ───────────────────────────────────────────────────────────────


class EastmoneySectorSource:
    """
    东方财富板块数据源。

    提供：
      - fetch_sector_rankings(): 全部板块涨跌幅 + 资金流排名
      - fetch_sector_constituents(): 单板块成分股列表
    """

    name: str = 'eastmoney_sector'

    def __init__(self):
        pass

    def fetch_sector_rankings(self, limit: int = 100) -> List[SectorData]:
        """
        获取全市场板块排名（含涨跌幅和资金流）。

        数据来源：东方财富板块涨幅榜（唯一来源）
        缓存：文件缓存，TTL=1小时

        Returns:
            List[SectorData]，按涨跌幅降序排列
        """
        # 1. 读缓存
        if _SECTOR_RANKINGS_CACHE.exists():
            try:
                mtime = datetime.fromtimestamp(_SECTOR_RANKINGS_CACHE.stat().st_mtime)
                if datetime.now() - mtime < _SECTOR_CACHE_TTL:
                    cached = json.loads(_SECTOR_RANKINGS_CACHE.read_text(encoding='utf-8'))
                    logger.info("[EM] 板块排名缓存命中 (%s)", _SECTOR_RANKINGS_CACHE)
                    return [SectorData(**s) for s in cached[:limit]]
            except Exception as e:
                logger.warning("[EM] 缓存读取失败: %s", e)

        # 2. 拉取板块列表（东方财富固定参数，fs=m:90+t:2 即全部板块）
        raw = _get('m:90+t:2+f:!50')
        if raw is None:
            # 网络错误，返回空
            logger.warning("[EM] fetch_sector_rankings 失败（网络层），返回空列表")
            return []

        diff = raw.get('data', {}) or {}
        records = diff.get('diff', [])
        if not isinstance(records, list):
            records = []

        sectors: List[SectorData] = []
        for i, rec in enumerate(records, 1):
            bk_code = str(rec.get('f12', ''))
            name = str(rec.get('f14', ''))
            if not bk_code or not name:
                continue

            # 资金流（f62，单位元）
            net_flow = _parse_amount(rec.get('f62', 0))
            # 成交额（f20，单位元）
            amount = _parse_amount(rec.get('f20', 0))

            sectors.append(SectorData(
                bk_code=f'EM_{bk_code}',
                name=name,
                change_pct=float(rec.get('f3', 0) or 0),
                net_flow=net_flow,
                amount=amount,
                rank_perf=i,   # 按涨跌幅排序即排名
                rank_flow=0,   # 后续再按资金流排序时填充
                source='eastmoney',
                timestamp=datetime.now().isoformat(),
            ))

        # 3. 按资金流排序填充 rank_flow
        by_flow = sorted(sectors, key=lambda s: s.net_flow, reverse=True)
        for rank, sec in enumerate(by_flow, 1):
            sec.rank_flow = rank

        # 4. 写缓存
        try:
            cache_data = [{
                'bk_code': s.bk_code, 'name': s.name,
                'change_pct': s.change_pct, 'net_flow': s.net_flow,
                'amount': s.amount, 'rank_perf': s.rank_perf,
                'rank_flow': s.rank_flow, 'source': s.source,
                'timestamp': s.timestamp,
            } for s in sectors]
            _SECTOR_RANKINGS_CACHE.write_text(
                json.dumps(cache_data, ensure_ascii=False, indent=2),
                encoding='utf-8',
            )
            logger.info("[EM] 板块排名缓存写入完成，共 %d 条", len(sectors))
        except Exception as e:
            logger.warning("[EM] 缓存写入失败: %s", e)

        return sectors[:limit]

    def fetch_sector_constituents(
        self,
        bk_code: str,
        limit: int = 20,
    ) -> List[SectorConstituentData]:
        """
        获取指定板块的成分股（含实时行情，按涨跌幅排序）。

        Args:
            bk_code: 板块代码
              - 东方财富格式: 'BK0716'（不含前缀）
              - 新浪格式: 'SINA_GNhwqc' → 自动转换
              - 自动处理：自动加 'b:' 前缀（东方财富格式要求）

        Returns:
            List[SectorConstituentData]，按涨跌幅降序
        """
        # 1. 标准化 bk_code 为东方财富格式
        em_code = bk_code
        if bk_code.startswith('SINA_'):
            # 新浪格式 'SINA_GNhwqc' → 提取 'GNhwqc' 后缀
            em_code = bk_code.split('_', 1)[1]
        elif not bk_code.startswith('EM_'):
            # 已经是去掉前缀的纯代码
            pass
        else:
            em_code = bk_code[3:]  # 去掉 'EM_' 前缀

        # 东方财富 fs 参数格式: b:BK0716
        fs_param = f'b:{em_code}'

        # 2. 请求
        raw = _get(fs_param)
        if raw is None:
            logger.warning("[EM] fetch_sector_constituents(%s) 网络失败，返回空", bk_code)
            return []

        diff = raw.get('data', {}) or {}
        records = diff.get('diff', [])
        if not isinstance(records, list):
            records = []

        constituents: List[SectorConstituentData] = []
        for rec in records:
            symbol = str(rec.get('f12', ''))
            name = str(rec.get('f14', ''))
            if not symbol or not name:
                continue

            # 转换 symbol 为标准格式（只需要 symbol 本身）
            std_sym = normalize_to_sina(symbol)

            constituents.append(SectorConstituentData(
                symbol=std_sym,
                name=name,
                price=float(rec.get('f2', 0) or 0),
                change_pct=float(rec.get('f3', 0) or 0),
                amount=_parse_amount(rec.get('f20', 0)),
                volume=float(rec.get('f6', 0) or 0),
                source='eastmoney',
            ))

        # 3. 按涨跌幅降序
        constituents.sort(key=lambda c: c.change_pct, reverse=True)
        return constituents[:limit]

    def fetch_sector_by_keyword(self, keyword: str) -> Optional[SectorData]:
        """
        通过关键词搜索板块（模糊匹配板块名称）。

        用于：把 '华为汽车' → 找到 EM_BK0716 这类板块代码。

        Returns:
            第一个匹配的 SectorData，或 None
        """
        all_sectors = self.fetch_sector_rankings(limit=500)
        kw = keyword.lower()
        for sec in all_sectors:
            if kw in sec.name.lower():
                return sec
        return None
