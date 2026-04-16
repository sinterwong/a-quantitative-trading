# -*- coding: utf-8 -*-
"""
fund_flow.py — 主力资金流服务
==========================

已验证可用接口（2026-04-16）：
  ✅ ak.stock_fund_flow_individual('5日排行') — 同花顺，返回全市场5191只股票的资金流排名
  ✅ ak.stock_market_fund_flow() — AkShare，两市大盘主力净流入（单位：元）

接口说明：
  stock_fund_flow_individual: 同花顺资金流排名
    参数: symbol='5日排行'（或'实时'/'3日排行'/'10日排行'/'20日排行'）
    返回: 全市场所有股票的排名列表，按资金流入净额排序
    列名: 序号, 股票代码, 股票简称, 最新价, 阶段涨跌幅, 连续换手率, 资金流入净额
    特点: 免费无需token，直接返回；但是排名列表，需按股票代码筛选

  stock_market_fund_flow: 两市资金流汇总
    返回: 上证/深证指数收盘价/涨跌幅 + 主力净流入/占比（单位：元）

⚠️ 注意：
  stock_individual_fund_flow(stock) 返回的是沪深300指数数据，不是个股数据！
  要获取个股资金流，必须用同花顺的 stock_fund_flow_individual 全市场排名后筛选。
"""

import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime
from threading import RLock
from typing import Any, Dict, List, Optional

import pandas as pd

# 禁用代理
for _key in list(os.environ.keys()):
    if 'proxy' in _key.lower():
        del os.environ[_key]

try:
    import akshare as ak
    AKSHARE_AVAILABLE = True
except ImportError:
    AKSHARE_AVAILABLE = False

logger = logging.getLogger('fund_flow')


# ─── 数据类型 ────────────────────────────────────────────────────────────────


@dataclass
class StockFundFlow:
    """
    个股资金流快照（来自同花顺全市场排名数据）。

    字段说明（akshare stock_fund_flow_individual 返回）：
      - code: 股票代码，如 '600900'
      - name: 股票简称
      - date: 数据日期（排名数据通常为当日）
      - close: 最新价（元）
      - change_pct: 阶段涨跌幅（如 '0.68%'，需解析）
      - turnover_rate: 连续换手率（如 '1.33%'）
      - main_net: 资金流入净额（元，正=流入，负=流出）
    """
    code: str
    name: str
    date: str           # YYYY-MM-DD
    close: float        # 最新价（元）
    change_pct: float  # 阶段涨跌幅（%）
    turnover_rate: float  # 连续换手率（%）
    main_net: float     # 资金流入净额（元）

    def to_dict(self) -> Dict:
        return {
            'code': self.code,
            'name': self.name,
            'date': self.date,
            'close': self.close,
            'change_pct': self.change_pct,
            'turnover_rate': self.turnover_rate,
            'main_net': round(self.main_net, 2),  # 元
            'main_net_yi': round(self.main_net / 1e8, 2),  # 亿元
        }


# ─── 同花顺资金流排名字段映射 ─────────────────────────────────────────────

# 同花顺返回列名（中文字段）
_THS_COLUMNS = [
    '序号', '股票代码', '股票简称', '最新价', '阶段涨跌幅', '连续换手率', '资金流入净额'
]

_THS_RENAME = {
    '股票代码': 'code',
    '股票简称': 'name',
    '最新价': 'close',
    '阶段涨跌幅': 'change_pct',
    '连续换手率': 'turnover_rate',
    '资金流入净额': 'main_net',
}


def _parse_ths_fund_flow(df: pd.DataFrame) -> List[StockFundFlow]:
    """
    解析同花顺资金流排名 DataFrame → List[StockFundFlow]。

    处理：
      1. 识别并重命名资金流列
      2. 解析百分比字符串（'0.68%' → 0.68）
      3. 解析资金净额字符串（'-6.72亿' → -6.72e8 元）
      4. 按股票代码建索引
    """
    if df is None or df.empty:
        return []

    # 重命名
    df = df.rename(columns=_THS_RENAME)

    # 过滤必要列
    required = ['code', 'name', 'main_net']
    missing = [c for c in required if c not in df.columns]
    if missing:
        logger.warning("[FundFlow] 同花顺数据缺少列 %s，可用: %s", missing, list(df.columns))
        return []

    records = []
    today = datetime.now().strftime('%Y-%m-%d')

    for _, row in df.iterrows():
        code = str(row.get('code', '')).strip()
        if not code or code == 'nan':
            continue

        # 解析涨跌幅
        change_pct_raw = row.get('change_pct', 0)
        if isinstance(change_pct_raw, str):
            change_pct = float(change_pct_raw.replace('%', '').strip())
        else:
            change_pct = float(change_pct_raw or 0)

        # 解析换手率
        turnover_raw = row.get('turnover_rate', 0)
        if isinstance(turnover_raw, str):
            turnover_rate = float(turnover_raw.replace('%', '').strip())
        else:
            turnover_rate = float(turnover_raw or 0)

        # 解析资金净额
        main_net_raw = row.get('main_net', 0)
        main_net = _parse_money_string(str(main_net_raw)) if main_net_raw else 0.0

        # 最新价
        close_raw = row.get('close')
        try:
            close = float(close_raw) if close_raw not in (None, '', '-') else 0.0
        except (ValueError, TypeError):
            close = 0.0

        records.append(StockFundFlow(
            code=code,
            name=str(row.get('name', '')).strip(),
            date=today,
            close=close,
            change_pct=change_pct,
            turnover_rate=turnover_rate,
            main_net=main_net,
        ))

    return records


def _parse_money_string(s: str) -> float:
    """
    解析资金字符串 → 元。

    Examples:
      '6.72亿'   → 6.72e8
      '-6.72亿'  → -6.72e8
      '1234万'   → 1234e4
      '-1234万'  → -1234e4
      '1234'    → 1234.0
      '-1234'   → -1234.0
    """
    s = str(s).strip().replace(' ', '')
    if not s or s in ('-', 'nan'):
        return 0.0
    negative = s.startswith('-')
    s = s.lstrip('-')
    try:
        if '亿' in s:
            return float(s.replace('亿', '')) * 1e8 * (-1 if negative else 1)
        elif '万' in s:
            return float(s.replace('万', '')) * 1e4 * (-1 if negative else 1)
        elif '万' in s:
            return float(s.replace('万', '')) * 1e4 * (-1 if negative else 1)
        else:
            return float(s) * (-1 if negative else 1)
    except (ValueError, TypeError):
        return 0.0


# ─── 线程安全缓存 ────────────────────────────────────────────────────────

class _FundFlowCache:
    """资金流专用缓存（线程安全，TTL）"""
    _lock = RLock()
    _store: Dict[str, Dict[str, Any]] = {}

    @classmethod
    def get(cls, key: str) -> Optional[Any]:
        with cls._lock:
            entry = cls._store.get(key)
            if entry and time.monotonic() < entry['expires_at']:
                return entry['value']
            return None

    @classmethod
    def set(cls, key: str, value: Any, ttl: float = 600):
        with cls._lock:
            cls._store[key] = {'value': value, 'expires_at': time.monotonic() + ttl}

    @classmethod
    def get_or_fetch(cls, key: str, fetch_fn, ttl: float = 600) -> Any:
        cached = cls.get(key)
        if cached is not None:
            return cached
        value = fetch_fn()
        if value is not None:
            cls.set(key, value, ttl)
        return value


# ─── 主力资金流服务 ────────────────────────────────────────────────────────


class FundFlowService:
    """
    主力资金流服务（整合多个可用接口）。

    已验证可用接口：
      ✅ stock_fund_flow_individual('5日排行') — 同花顺，全市场排名，5191只股票
         * 列: 股票代码/股票简称/最新价/阶段涨跌幅/连续换手率/资金流入净额
         * 可按 stock_code 筛选获取个股数据
         * 缓存: 10分钟 TTL
      ✅ stock_market_fund_flow() — AkShare，两市汇总大盘资金流
         * 返回: 上证/深证指数收盘/涨跌幅 + 主力净流入/占比

    不可用/待验证：
      ⚠️ stock_individual_fund_flow_rank — eastmoney push2接口，代理问题
      ⚠️ stock_main_fund_flow — eastmoney，需要正确的股票列表参数
      ❌ stock_individual_fund_flow(stock) — 返回沪深300，非个股
    """

    def __init__(self):
        if not AKSHARE_AVAILABLE:
            raise ImportError("[FundFlowService] AkShare 未安装: pip install akshare")

    # ── 个股资金流（来自同花顺全市场排名）────────────────────────────

    def get_stock_fund_flow(self, stock_code: str, period: str = '5日排行') -> Optional[StockFundFlow]:
        """
        获取个股资金流数据（来自同花顺全市场排名）。

        Args:
            stock_code: 股票代码，如 '600900'
            period: 时间周期，默认 '5日排行'
                   可选: '实时' / '3日排行' / '5日排行' / '10日排行' / '20日排行'

        Returns:
            StockFundFlow 对象，或 None（未找到或接口失败）

        注意：
            同花顺返回的是全市场排名数据，我们从中筛选对应股票。
            由于是排名列表，无法直接获取某只股票特定日期的历史序列，
            只能获取当前最新的排名数据。
        """
        period_key = period.replace(' ', '')
        cache_key = f"ths_ff_{period_key}"

        # 获取全市场数据（带缓存）
        all_stocks = self._get_ths_all_fund_flow(period_key)
        if all_stocks is None:
            logger.warning("[FundFlow] 无法获取同花顺资金流数据: stock=%s", stock_code)
            return None

        # 按股票代码筛选
        stock_code_clean = stock_code.strip()
        for record in all_stocks:
            if record.code == stock_code_clean:
                logger.debug("[FundFlow] 找到 %s 同花顺资金流: %.2f亿",
                           stock_code, record.main_net / 1e8)
                return record

        logger.warning("[FundFlow] 同花顺数据中未找到 %s", stock_code)
        return None

    def get_top_fund_flow_stocks(self, period: str = '5日排行', top_n: int = 20) -> List[StockFundFlow]:
        """
        获取资金流入最多的 TOP N 只股票。

        Args:
            period: 时间周期，默认 '5日排行'
            top_n: 返回数量，默认20

        Returns:
            List[StockFundFlow]，按 main_net 降序（流入最多在前）
        """
        period_key = period.replace(' ', '')
        cache_key = f"ths_ff_{period_key}"
        all_stocks = self._get_ths_all_fund_flow(period_key)
        if all_stocks is None:
            return []

        # 按 main_net 降序
        sorted_stocks = sorted(all_stocks, key=lambda r: r.main_net, reverse=True)
        return sorted_stocks[:top_n]

    def _get_ths_all_fund_flow(self, period_key: str) -> Optional[List[StockFundFlow]]:
        """获取同花顺全市场资金流排名（内部缓存）"""
        cache_key = f"ths_ff_{period_key}"

        def fetch():
            logger.info("[FundFlow] 从同花顺获取全市场资金流: period=%s", period_key)
            period_map = {
                '实时': '实时',
                '3日排行': '3日排行',
                '5日排行': '5日排行',
                '10日排行': '10日排行',
                '20日排行': '20日排行',
            }
            period_val = period_map.get(period_key, '5日排行')
            try:
                df = ak.stock_fund_flow_individual(symbol=period_val)
                if df is None or df.empty:
                    return None
                return _parse_ths_fund_flow(df)
            except Exception as e:
                logger.warning("[FundFlow] 同花顺接口失败: %s", e)
                return None

        cached = _FundFlowCache.get(cache_key)
        if cached is not None:
            logger.debug("[FundFlow] 缓存命中: %s", cache_key)
            return cached

        result = fetch()
        if result is not None:
            _FundFlowCache.set(cache_key, result, ttl=600)
        return result

    # ── 大盘资金流（AkShare）────────────────────────────

    def get_market_fund_flow(self) -> Dict:
        """
        获取大盘市场资金流汇总。

        Returns:
            Dict:
              - date: 日期
              - sh_close, sh_change: 上证指数收盘/涨跌幅
              - sz_close, sz_change: 深证成指收盘/涨跌幅
              - main_net: 沪深合计主力净流入（亿元）
              - main_pct: 主力净流入占成交额百分比
        """
        cache_key = "market_ff"
        cached = _FundFlowCache.get(cache_key)
        if cached is not None:
            return cached

        try:
            df = ak.stock_market_fund_flow()
            if df is None or df.empty:
                return {}

            df = df.rename(columns={
                '日期': 'date',
                '上证-收盘价': 'sh_close',
                '上证-涨跌幅': 'sh_change',
                '深证-收盘价': 'sz_close',
                '深证-涨跌幅': 'sz_change',
                '主力净流入-净额': 'main_net',
                '主力净流入-净占比': 'main_pct',
            })

            # 按日期降序
            if 'date' in df.columns:
                df = df.sort_values('date', ascending=False).reset_index(drop=True)

            latest = df.iloc[0]
            result = {
                'date': str(latest.get('date', ''))[:10],
                'sh_close': float(latest.get('sh_close', 0) or 0),
                'sh_change': float(latest.get('sh_change', 0) or 0),
                'sz_close': float(latest.get('sz_close', 0) or 0),
                'sz_change': float(latest.get('sz_change', 0) or 0),
                'main_net': round(float(latest.get('main_net', 0) or 0) / 1e8, 2),  # 亿元
                'main_pct': float(latest.get('main_pct', 0) or 0),  # %
            }
            _FundFlowCache.set(cache_key, result, ttl=1800)
            logger.info("[FundFlow] 大盘资金流: %s", result)
            return result

        except Exception as e:
            logger.warning("[FundFlow] 获取大盘资金流失败: %s", e)
            return {}

    # ── 主力净流入摘要 ──────────────────────────────────────────────

    def get_main_net_summary(self, stock_code: str, period: str = '5日排行') -> Dict:
        """
        获取个股主力净流入摘要（用于选股评分）。

        Returns:
            {
                'code': str,
                'name': str,
                'date': str,
                'main_net': float,   # 元
                'main_net_yi': float,  # 亿元
                'close': float,
                'change_pct': float,  # %
                'turnover_rate': float,
                'signal': str  # strong_inflow/inflow/neutral/outflow/strong_outflow
            }
        """
        flow = self.get_stock_fund_flow(stock_code, period)
        if flow is None:
            return {'code': stock_code, 'signal': 'unknown', 'main_net': 0}

        net_yi = flow.main_net / 1e8
        if net_yi > 5:
            signal = 'strong_inflow'
        elif net_yi > 1:
            signal = 'inflow'
        elif net_yi < -5:
            signal = 'strong_outflow'
        elif net_yi < -1:
            signal = 'outflow'
        else:
            signal = 'neutral'

        return {
            'code': flow.code,
            'name': flow.name,
            'date': flow.date,
            'main_net': round(flow.main_net, 2),
            'main_net_yi': round(net_yi, 2),
            'close': flow.close,
            'change_pct': flow.change_pct,
            'turnover_rate': flow.turnover_rate,
            'signal': signal,
        }
