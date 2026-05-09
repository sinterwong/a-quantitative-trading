"""
backend/services/sector_comparison.py — 行业板块横向对比

给定行业名称或股票列表，对比同行业个股的估值水平（市盈率、市净率）。
数据来源：腾讯 qt.gtimg.cn 批量行情（一次查询多只股票）。

用法：
    compare_sector(sector="白酒", base_symbol="603369.SH")
    compare_symbols(symbols=["603369.SH","000858.SZ","600519.SH","600809.SH"], base_symbol="603369.SH")
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger('backend.sector_comparison')

# ─── 主流行业成分股映射 ─────────────────────────────────────────────────────
# key: 行业名（与用户输入匹配）  value: A股代码列表（带.SH/.SZ后缀）
# 仅覆盖主流板块，未覆盖的板块用户可通过 symbols 参数自定义传入

SECTOR_STOCKS: Dict[str, List[str]] = {
    "白酒": [
        "000858.SZ",   # 五粮液
        "600519.SH",   # 贵州茅台
        "600809.SH",   # 山西汾酒
        "603369.SH",   # 今世缘
        "000596.SZ",   # 古井贡酒
        "002304.SZ",   # 洋河股份
        "000568.SZ",   # 泸州老窖
        "603589.SH",   # 口子窖
        "603919.SH",   # 金徽酒
        "600197.SH",   # 伊力特
    ],
    "银行": [
        "600000.SH",   # 浦发银行
        "600016.SH",   # 民生银行
        "600036.SH",   # 招商银行
        "601166.SH",   # 兴业银行
        "601288.SH",   # 农业银行
        "601398.SH",   # 工商银行
        "601939.SH",   # 建设银行
        "601818.SH",   # 光大银行
        "601009.SH",   # 南京银行
        "600015.SH",   # 华夏银行
        "601328.SH",   # 交通银行
        "601658.SH",   # 邮储银行
        "601838.SH",   # 成都银行
        "600919.SH",   # 江苏银行
        "600926.SH",   # 杭州银行
    ],
    "房地产": [
        "000002.SZ",   # 万科A
        "600048.SH",   # 保利发展
        "600383.SH",   # 金地集团
        "600606.SH",   # 绿地控股
        "001979.SZ",   # 招商蛇口
        "601155.SH",   # 新城控股
        "600340.SH",   # 华夏幸福
        "000402.SZ",   # 金融街
        "000671.SZ",   # 阳光城
        "002146.SZ",   # 荣盛发展
    ],
    "医药": [
        "600196.SH",   # 复星医药
        "600276.SH",   # 恒瑞医药
        "000538.SZ",   # 云南白药
        "603259.SH",   # 药明康德
        "002821.SZ",   # 凯莱英
        "000963.SZ",   # 华东医药
        "600436.SH",   # 片仔癀
        "000661.SZ",   # 长春高新
        "300760.SZ",   # 迈瑞医疗
        "688180.SH",   # 君实生物
    ],
    "电力设备": [
        "600406.SH",   # 国电南瑞
        "601012.SH",   # 隆基绿能
        "600900.SH",   # 长江电力
        "600905.SH",   # 三峡能源
        "002594.SZ",   # 比亚迪（汽车+电力设备）
        "600089.SH",   # 特变电工
        "601615.SH",   # 明阳智能
        "002459.SZ",   # 晶澳科技
    ],
    "电子": [
        "000100.SZ",   # TCL科技
        "600183.SH",   # 生益科技
        "603501.SH",   # 韦尔股份
        "603986.SH",   # 兆易创新
        "002241.SZ",   # 歌尔股份
        "000725.SZ",   # 京东方A
        "600745.SH",   # 闻泰科技
        "002456.SZ",   # 欧菲光
        "300474.SZ",   # 景嘉微
        "688008.SH",   # 澜起科技
    ],
    "计算机": [
        "000063.SZ",   # 中兴通讯
        "000066.SZ",   # 中国长城
        "000938.SZ",   # 紫光股份
        "600570.SH",   # 恒生电子
        "600588.SH",   # 用友网络
        "002410.SZ",   # 广联达
        "002230.SZ",   # 科大讯飞
        "300033.SZ",   # 同花顺
        "603019.SH",   # 中科曙光
        "688111.SH",   # 金山办公
    ],
    "国防军工": [
        "600760.SH",   # 中航沈飞
        "600150.SH",   # 中国船舶
        "601989.SH",   # 中国重工
        "000768.SZ",   # 中航西飞
        "600316.SH",   # 洪都航空
        "600038.SH",   # 中直股份
        "002013.SZ",   # 中航机电
        "600879.SH",   # 航天电子
        "601698.SH",   # 中国卫通
        "688185.SH",   # 康希通信
    ],
    "食品饮料": [
        "000858.SZ",   # 五粮液
        "600519.SH",   # 贵州茅台
        "600809.SH",   # 山西汾酒
        "603288.SH",   # 海天味业
        "603589.SH",   # 口子窖
        "000895.SZ",   # 双汇发展
        "000876.SZ",   # 新希望
        "600887.SH",   # 伊利股份
        "002507.SZ",   # 涪陵榨菜
        "600600.SH",   # 青岛啤酒
    ],
    "非银金融": [
        "600030.SH",   # 中信证券
        "600109.SH",   # 国金证券
        "600837.SH",   # 海通证券
        "601066.SH",   # 中信建投
        "601088.SH",   # 中国神华（煤炭+金融）
        "601211.SH",   # 国泰君安
        "601318.SH",   # 中国平安
        "601336.SH",   # 新华保险
        "601601.SH",   # 中国太保
        "601628.SH",   # 中国人寿
        "601688.SH",   # 华泰证券
        "601818.SH",   # 光大银行（非银部分）
        "601878.SH",   # 浙商证券
        "600918.SH",   # 中泰证券
    ],
    "煤炭": [
        "601088.SH",   # 中国神华
        "601225.SH",   # 陕西煤业
        "600188.SH",   # 兖矿能源
        "601001.SH",   # 晋控煤业
        "600971.SH",   # 恒源煤电
        "600395.SH",   # 盘江股份
        "000983.SZ",   # 山西焦煤
        "600508.SH",   # 上海能源
        "600971.SH",   # 恒源煤电
    ],
    "有色金属": [
        "603799.SH",   # 华友钴业
        "600456.SH",   # 宝钛股份
        "601618.SH",   # 中国中冶
        "600547.SH",   # 山东黄金
        "600489.SH",   # 中金黄金
        "601899.SH",   # 紫金矿业
        "000807.SZ",   # 云铝股份
        "002460.SZ",   # 赣锋锂业
        "002466.SZ",   # 天齐锂业
        "600111.SH",   # 北方稀土
    ],
    "化工": [
        "600160.SH",   # 巨化股份
        "600176.SH",   # 中国巨石
        "600309.SH",   # 万华化学
        "601216.SH",   # 君正集团
        "002064.SZ",   # 华峰化学
        "000301.SZ",   # 东方盛虹
        "600989.SH",   # 宝丰能源
        "600409.SH",   # 三友化工
    ],
    "建筑": [
        "601186.SH",   # 中国铁建
        "601668.SH",   # 中国建筑
        "600170.SH",   # 上海建工
        "600502.SH",   # 安徽建工
        "002051.SZ",   # 中工国际
        "601390.SH",   # 中国中铁
        "601800.SH",   # 中国交建
        "601618.SH",   # 中国中冶
    ],
    "交通运输": [
        "600009.SH",   # 上海机场
        "601006.SH",   # 大秦铁路
        "601018.SH",   # 宁波港
        "601021.SH",   # 春秋航空
        "601111.SH",   # 中国国航
        "601127.SH",   # 赛力斯（汽车+交通）
        "601238.SH",   # 广汽集团
        "600115.SH",   # 东方航空
        "600221.SH",   # 海航控股
        "601333.SH",   # 广深铁路
    ],
}


# ─── 数据获取 ────────────────────────────────────────────────────────────────


def _fetch_batch_tencent(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """
    腾讯 qt.gtimg.cn 批量行情，一次查询最多50只股票。
    Returns: { "sh600519": { "name": "...", "pe": 30.5, "pb": 4.2, ... }, ... }
    """
    if not symbols:
        return {}

    # 腾讯批量格式：逗号分隔，不超过50只
    batch = symbols[:50]
    # 标准化为腾讯格式：000858.SZ → sz000858，600519.SH → sh600519
    normalized = []
    for s in batch:
        s = s.strip().upper()
        if s.endswith('.SZ'):
            normalized.append(f'sz{s[:6]}')
        elif s.endswith('.SH'):
            normalized.append(f'sh{s[:6]}')
        elif len(s) == 6 and s.isdigit():
            # 纯数字，按开头判断深沪
            normalized.append(f'sz{s}' if s.startswith(('0','3')) else f'sh{s}')
        else:
            normalized.append(s)
    joined = ','.join(normalized)
    url = f'https://qt.gtimg.cn/q={joined}'

    try:
        import urllib.request
        req = urllib.request.Request(
            url,
            headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                'Referer': 'https://finance.qq.com',
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode('gbk', errors='replace')
    except Exception as exc:
        logger.warning('腾讯批量行情请求失败: %s', exc)
        return {}

    result: Dict[str, Dict[str, Any]] = {}

    for line in raw.splitlines():
        line = line.strip()
        if not line or '=' not in line:
            continue
        try:
            key, rest = line.split('=', 1)
            key = key.strip().lstrip('v_')
            parts = rest.strip().strip('"').split('~')
            if len(parts) < 10:
                continue

            def sf(val: str, default: float = 0.0) -> float:
                try:
                    s = val.strip()
                    if s in ('', '-', '--'):
                        return default
                    f = float(s)
                    return f if f == f else default  # NaN check
                except (ValueError, TypeError):
                    return default

            pe = sf(parts[39]) if len(parts) > 39 else 0.0
            pb = sf(parts[46]) if len(parts) > 46 else 0.0
            market_cap = sf(parts[45]) if len(parts) > 45 else 0.0  # 亿
            turnover_rate = sf(parts[43]) if len(parts) > 43 else 0.0  # 换手率%

            result[key] = {
                'name': parts[1] if len(parts) > 1 else '',
                'price': sf(parts[3]),
                'pct_change': sf(parts[32]),
                'pe': pe,
                'pb': pb,
                'market_cap': market_cap,
                'turnover_rate': turnover_rate,
            }
        except Exception as exc:
            logger.debug('解析行失败 %s: %s', line[:50], exc)
            continue

    return result


def _compute_percentile(value: float, values: List[float]) -> Optional[float]:
    """计算 value 在 values 列表中的百分位（0~100）。"""
    if not values or value == 0.0:
        return None
    count = sum(1 for v in values if v > 0 and v <= value)
    total = sum(1 for v in values if v > 0)
    if total == 0:
        return None
    return round(count / total * 100, 1)


# ─── 核心函数 ────────────────────────────────────────────────────────────────


@dataclass
class SectorComparisonResult:
    sector_name: str
    stock_count: int
    stocks: List[Dict[str, Any]] = field(default_factory=list)
    avg_pe: Optional[float] = None
    avg_pb: Optional[float] = None
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'sector_name': self.sector_name,
            'stock_count': self.stock_count,
            'avg_pe': self.avg_pe,
            'avg_pb': self.avg_pb,
            'stocks': self.stocks,
            'warnings': self.warnings,
        }


def compare_sector(sector: str, base_symbol: Optional[str] = None) -> SectorComparisonResult:
    """
    根据行业名称做板块横向对比。

    Args:
        sector: 行业名称，如 "白酒"、"银行"、"医药"
        base_symbol: 可选，基准股票代码（如 "603369.SH"），结果中标记该股票

    Returns:
        SectorComparisonResult

    Raises:
        ValueError: 未知行业

    用法：
        result = compare_sector("白酒", "603369.SH")
    """
    sector = sector.strip()
    stocks = SECTOR_STOCKS.get(sector)
    if stocks is None:
        raise ValueError(f'未知行业: {sector!r}，支持的行业: {list(SECTOR_STOCKS.keys())}')

    return _compare_stocks(stocks, sector, base_symbol)


def compare_symbols(symbols: List[str], sector_name: str = "自定义",
                    base_symbol: Optional[str] = None) -> SectorComparisonResult:
    """
    根据股票代码列表做横向对比（不依赖预定义行业）。

    Args:
        symbols: A股代码列表，如 ["603369.SH", "000858.SZ", "600519.SH"]
        sector_name: 板块名称（用于展示），如 "白酒"、"自定义"
        base_symbol: 可选，基准股票代码

    用法：
        result = compare_symbols(["603369.SH","000858.SZ","600519.SH"], "白酒", "603369.SH")
    """
    if not symbols:
        raise ValueError('symbols 不能为空')
    # 去重保持顺序
    unique = list(dict.fromkeys(symbols))
    return _compare_stocks(unique, sector_name, base_symbol)


def _compare_stocks(symbols: List[str], sector_name: str,
                    base_symbol: Optional[str] = None) -> SectorComparisonResult:
    """内部函数：对给定股票列表做横向对比。"""
    # 标准化 base_symbol
    base_key = None
    if base_symbol:
        b = base_symbol.strip().upper()
        if b.endswith('.SH'):
            base_key = f'sh{b[:6]}'
        elif b.endswith('.SZ'):
            base_key = f'sz{b[:6]}'

    quotes = _fetch_batch_tencent(symbols)

    if not quotes:
        result = SectorComparisonResult(sector_name=sector_name, stock_count=len(symbols))
        result.warnings.append('腾讯批量行情请求失败，请检查网络或股票代码')
        return result

    # 构建个股结果
    stocks_out: List[Dict[str, Any]] = []
    all_pe: List[float] = []
    all_pb: List[float] = []
    fetched_count = 0

    for sym in symbols:
        # 标准化为腾讯 key
        s = sym.strip().upper()
        if s.endswith('.SH'):
            key = f'sh{s[:6]}'
        elif s.endswith('.SZ'):
            key = f'sz{s[:6]}'
        else:
            continue

        q = quotes.get(key)
        if q is None or q.get('price', 0) == 0:
            continue

        fetched_count += 1
        pe = q['pe']
        pb = q['pb']

        if pe > 0:
            all_pe.append(pe)
        if pb > 0:
            all_pb.append(pb)

        entry = {
            'symbol': sym,
            'name': q['name'],
            'price': q['price'],
            'pct_change': q['pct_change'],
            'pe': pe if pe > 0 else None,
            'pb': pb if pb > 0 else None,
            'market_cap': q['market_cap'],
            'is_base': key == base_key,
        }
        stocks_out.append(entry)

    # 计算行业均值
    avg_pe = round(sum(all_pe) / len(all_pe), 2) if all_pe else None
    avg_pb = round(sum(all_pb) / len(all_pb), 2) if all_pb else None

    # 计算百分位
    for s in stocks_out:
        pe = s.get('pe')
        pb = s.get('pb')
        s['pe_percentile'] = _compute_percentile(pe, all_pe)
        s['pb_percentile'] = _compute_percentile(pb, all_pb)

    result = SectorComparisonResult(
        sector_name=sector_name,
        stock_count=len(stocks_out),
        stocks=stocks_out,
        avg_pe=avg_pe,
        avg_pb=avg_pb,
    )
    if fetched_count == 0:
        result.warnings.append('未能获取任何股票数据')

    return result
