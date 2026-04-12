"""
虚拟实盘配置 - Sinter专属股票池
=====================================
资金：20,000 RMB | 10层仓位 | 最多5只

选股标准（价值投资风格）：
1. 有真实盈利支撑（不炒概念）
2. 行业龙头或细分前三
3. 政策实质受益（不追空喊话）
4. 机构持仓背书

仓位设计：
- 每层 ≈ 2000元（1/10仓位）
- 单只股票 ≤ 2层（4000元，20%）
- 总仓位 ≤ 8层（16000元，80%）

覆盖方向：
- 新能源（宁德时代）- 实质政策落地
- 创新药（恒瑞医药）- 医保谈判实质受益
- 半导体（中芯国际）- 国产替代实质
- 公用事业（长江电力）- 稳定股息
- 消费（茅台）- 品牌护城河
"""

import os
import json

# ============================================================
# 全局参数加载（params.json）
# ============================================================

THIS = os.path.abspath(__file__)
QUANT_DIR = os.path.dirname(THIS)
_PARAMS_PATH = os.path.join(QUANT_DIR, '..', '..', 'params.json')

try:
    with open(_PARAMS_PATH, 'r', encoding='utf-8') as f:
        PARAMS = json.load(f)
except Exception:
    PARAMS = {}


def get_default_params(source: str) -> dict:
    """
    获取信号源默认参数，来源: params.json
    用法:
        from config_stock_pool import get_default_params
        rsi_defaults = get_default_params('RSI')
        # {'period': 21, 'oversold': 35, 'overbought': 65, ...}
    """
    defaults = PARAMS.get('defaults', {})
    return defaults.get(source, {})


def get_portfolio_config() -> dict:
    """获取组合配置（资金、风控），来源: params.json"""
    return {
        **PARAMS.get('portfolio', {}),
        **PARAMS.get('risk', {}),
    }


# ============================================================
# 股票池
# ============================================================

PORTFOLIO = {
    'capital': 20000,
    'max_positions': 5,
    'max_layers_per_stock': 2,
    'layers': 10,
    'layer_size': 2000,
    'strategy': 'RSI+Inst',
    'risk': {
        'max_position_pct': 0.20,
        'max_drawdown_limit': 0.30,
        'commission': 0.0003,
        'stamp_tax': 0.001,
        'slippage': 0.0005,
    },
    'stocks': {
        '600900.SH': {
            'name': '长江电力',
            'sector': '公用事业',
            'layers': 2,
            'strategy_override': {
                'rsi_buy': 35,
                'rsi_sell': 70,
                'stop_loss': 0.08,
                'take_profit': 0.20,
            },
            'selection_reason': '稳定高股息+来水周期+政策受益（电力改革）',
        },
        '300750.SZ': {
            'name': '宁德时代',
            'sector': '新能源',
            'layers': 2,
            'strategy_override': {
                'rsi_buy': 35,
                'rsi_sell': 65,
                'stop_loss': 0.05,
                'take_profit': 0.30,
            },
            'selection_reason': '全球份额第一+麒麟电池量产+政策实质落地',
        },
        '600276.SH': {
            'name': '恒瑞医药',
            'sector': '创新药',
            'layers': 2,
            'strategy_override': {
                'rsi_buy': 35,
                'rsi_sell': 70,
                'stop_loss': 0.10,
                'take_profit': 0.20,
            },
            'selection_reason': '创新药龙头+医保谈判受益+研发投入持续',
        },
        '688981.SH': {
            'name': '中芯国际',
            'sector': '半导体',
            'layers': 2,
            'strategy_override': {
                'rsi_buy': 35,
                'rsi_sell': 70,
                'stop_loss': 0.10,
                'take_profit': 0.25,
            },
            'selection_reason': '国产替代实质+成熟制程扩产+政策补贴落地',
        },
        '600519.SH': {
            'name': '贵州茅台',
            'sector': '高端消费',
            'layers': 2,
            'strategy_override': {
                'rsi_buy': 35,
                'rsi_sell': 75,
                'stop_loss': 0.08,
                'take_profit': 0.30,
            },
            'selection_reason': '品牌护城河+批条效应+高分红率',
        },
    },
}

# 政策跟踪标注（按股票）
POLICY_CALENDAR = {
    '600900.SH': ['来水季（Q2/Q3）', '电费改革政策落地', '年度分红方案（3月）'],
    '300750.SZ': ['麒麟电池量产节点', '欧盟碳关税生效', '动力电池白名单核查'],
    '600276.SH': ['医保谈判结果（11月）', '创新药出海FDA获批', '财报季（4月/8月/10月）'],
    '688981.SH': ['成熟制程补贴审批', '设备国产化率通报', '财报季（4月/8月/10月）'],
    '600519.SH': ['茅台酒出货量公告', '年度分红（6月）', '中秋国庆动销数据'],
}

# 消息面关注方向
NEWS_FOCUS = {
    '600900.SH': ['水电来水量', '电费上调', 'ESG政策'],
    '300750.SZ': ['欧盟碳关税', '电动车销量', '电池原材料价格'],
    '600276.SH': ['医保谈判', '创新药出海', '临床数据'],
    '688981.SH': ['设备禁运令', '成熟制程扩产', '国产化率'],
    '600519.SH': ['茅台酒批价', '中秋动销', '居民消费数据'],
}


def get_portfolio():
    return PORTFOLIO


def get_stock_list():
    return list(PORTFOLIO['stocks'].keys())


def get_stock_info(symbol):
    return PORTFOLIO['stocks'].get(symbol, {})


def get_strategy_config(symbol):
    """
    获取个股策略配置
    优先使用 params.json 全局默认值，个股 strategy_override 做覆盖
    """
    # 从 params.json 加载全局默认值
    defaults = get_default_params('RSI')
    base = {
        'rsi_buy': defaults.get('oversold', 35),
        'rsi_sell': defaults.get('overbought', 65),
        'stop_loss': defaults.get('stop_loss', 0.05),
        'take_profit': defaults.get('take_profit', 0.20),
        'period': defaults.get('period', 21),
        'min_hold_days': defaults.get('min_hold_days', 5),
    }
    # 个股覆盖
    info = PORTFOLIO['stocks'].get(symbol, {})
    base.update(info.get('strategy_override', {}))
    return base


def get_policy_calendar(symbol):
    return POLICY_CALENDAR.get(symbol, [])


def get_news_focus(symbol):
    return NEWS_FOCUS.get(symbol, [])


if __name__ == '__main__':
    print("=" * 60)
    print("Sinter 虚拟实盘股票池")
    print("=" * 60)
    print(f"资金: {PORTFOLIO['capital']:,} RMB")
    print(f"层数: {PORTFOLIO['layers']}层 x {PORTFOLIO['layer_size']:,}元/层")
    print(f"最多持仓: {PORTFOLIO['max_positions']}只")
    print()
    print(f"全局默认RSI参数（来自params.json）:")
    rsi_def = get_default_params('RSI')
    print(f"  period={rsi_def.get('period',21)} "
          f"oversold={rsi_def.get('oversold',35)} "
          f"overbought={rsi_def.get('overbought',65)} "
          f"SL={rsi_def.get('stop_loss',0.05):.0%} "
          f"TP={rsi_def.get('take_profit',0.20):.0%}")
    print()

    total_layers = 0
    for symbol, info in PORTFOLIO['stocks'].items():
        layers = info.get('layers', 1)
        layer_value = layers * PORTFOLIO['layer_size']
        total_layers += layers
        alloc_pct = layer_value / PORTFOLIO['capital'] * 100
        cfg = get_strategy_config(symbol)
        print(f"  {symbol} {info['name']}")
        print(f"    持仓: {layers}层 ({layer_value:,}元, {alloc_pct:.0f}%)")
        print(f"    理由: {info['selection_reason']}")
        print(f"    RSI: {cfg['rsi_buy']}/{cfg['rsi_sell']} "
              f"SL={cfg['stop_loss']:.0%} TP={cfg['take_profit']:.0%}")
        print()

    print(f"合计仓位: {total_layers}层 / {PORTFOLIO['layers']}层 "
          f"({total_layers/PORTFOLIO['layers']*100:.0f}%)")
    print(f"预留现金: {PORTFOLIO['layers']-total_layers}层 = "
          f"{(PORTFOLIO['layers']-total_layers)*PORTFOLIO['layer_size']:,}元")
