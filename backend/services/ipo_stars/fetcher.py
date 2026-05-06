"""
fetcher.py — IPO Stars 数据获取抽象层
====================================
定义港股 IPO 数据的获取接口，后续逐个接入具体数据源。

数据源规划：
    - HKEX 官网 IPO 日历（HTML 爬取）
    - 券商 API（富途 / 耀才 / 辉立）获取实时认购数据
    - 东方财富 / AKShare 获取新闻与情绪
    - 招股书 PDF 解析（Pre-IPO 成本、基石名单）
"""

import logging
from typing import List, Dict, Optional

logger = logging.getLogger('ipo_stars.fetcher')


class IPODataFetcher:
    """
    港股 IPO 数据获取器（抽象层）。

    所有方法返回标准化 dict，具体数据源对接在子类或后续迭代中实现。
    当前版本抛出 NotImplementedError 以标记待接入点。
    """

    def fetch_upcoming_ipos(self) -> List[Dict]:
        """
        获取即将上市 / 招股中的 IPO 列表。

        Returns:
            [
                {
                    'code': '09696',
                    'name': 'XXX',
                    'status': 'subscripting',
                    'listing_date': '2026-05-15',
                    'offer_price_low': 10.0,
                    'offer_price_high': 12.0,
                    ...
                },
            ]

        数据源候选：HKEX IPO 日历、AKShare hk_ipo
        """
        raise NotImplementedError("待接入 HKEX / AKShare 数据源")

    def fetch_prospectus(self, code: str) -> Dict:
        """
        获取招股书关键数据。

        Returns:
            {
                'code': '09696',
                'name': 'XXX',
                'issue_size': 10.5,         # 亿港元
                'offer_price_low': 10.0,
                'offer_price_high': 12.0,
                'sponsor': '中金公司',
                'stabilizer': '摩根士丹利',
                'cornerstone_names': 'GIC,Temasek,高瓴',
                'cornerstone_pct': 0.55,
                'industry': '人工智能',
                'pre_ipo_cost': 8.0,        # 最后一轮融资单价
                'highlights': '...',        # 业务亮点摘要（供 LLM 分析）
            }

        数据源候选：HKEX 招股书 PDF 解析
        """
        raise NotImplementedError("待接入招股书数据源")

    def fetch_subscription_data(self, code: str) -> Dict:
        """
        获取实时认购数据。

        Returns:
            {
                'public_offer_multiple': 150.5,  # 公开发售超购倍数
                'margin_multiple': 80.0,         # 综合孖展倍数
                'margin_by_broker': {            # 各券商孖展
                    '富途': 90.0,
                    '耀才': 75.0,
                    '辉立': 70.0,
                },
                'clawback_pct': 0.50,            # 回拨比例
                'updated_at': '2026-05-10T14:30:00',
            }

        数据源候选：券商 API（富途 OpenAPI / 耀才 / 辉立）
        """
        raise NotImplementedError("待接入券商 API")

    def fetch_market_context(self) -> Dict:
        """
        获取大盘环境数据。

        Returns:
            {
                'hstech_close': 4500.0,         # 恒生科技指数收盘
                'hstech_bias_5d': 0.03,         # 5日乖离率
                'hsi_vix': 18.5,                # 恒指波动率
                'sector_ipo_performance': [      # 同行业近3只新股首日表现
                    {'code': '09695', 'name': 'YYY', 'first_day_return': 0.15},
                ],
            }

        数据源候选：AKShare / 东方财富 / Tencent Finance
        """
        raise NotImplementedError("待接入行情数据源")

    def fetch_stabilizer_history(self, stabilizer: str) -> Dict:
        """
        获取稳价人历史战绩。

        Returns:
            {
                'stabilizer': '中金公司',
                'total_projects': 10,
                'win_count': 7,                  # 首日收涨
                'loss_count': 3,                 # 首日破发
                'win_rate': 0.70,
                'avg_first_day_return': 0.12,
                'protection_style': '强硬',       # 强硬 | 温和 | 放任
                'recent_projects': [
                    {'code': '09690', 'name': 'AAA', 'first_day_return': 0.08},
                ],
            }

        数据源候选：历史 IPO 数据库（需自建）
        """
        raise NotImplementedError("待接入历史 IPO 数据")

    def fetch_dark_pool_estimate(self, code: str) -> Dict:
        """
        获取暗盘交易预测数据。

        Returns:
            {
                'estimated_dark_price': 12.5,    # 暗盘预估价
                'estimated_volume': 1000000,     # 暗盘预估成交量
                'source': 'broker_aggregate',
            }

        数据源候选：券商暗盘数据聚合
        """
        raise NotImplementedError("待接入暗盘数据源")
