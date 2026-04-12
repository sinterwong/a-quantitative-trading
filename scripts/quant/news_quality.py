"""
news_quality.py — 新闻质量评分
================================
对每条新闻标题进行多维度质量评估，过滤噪声信号。

评分维度：
  1. 确定性（+）：具体数字/公司/时间 → 加分
  2. 含糊指示词（-）：有望/或将/可能/知情人士 → 扣分
  3. 来源权威性（+）：证监会/国务院/交易所官网 → 加分
  4. 情感方向（±）：利好/利空 关键词 → 加减分
  5. 紧迫性（+）：涨停/复牌/重大合作 → 加分

最终输出：quality_score 0.0~1.0 + filtered_title
用法：
  from news_quality import score_news_item
  item['quality'] = score_news_item(item['title'])
"""

import re
from typing import Dict, Optional

# ─── 含糊指示词（减分项）──────────────────────────────
# 出现在标题中，降低新闻确定性 → 扣分权重
VAGUE_PHRASES = [
    (r'有望',        -0.20),
    (r'或将',        -0.20),
    (r'可能',        -0.15),
    (r'将要',        -0.10),
    (r'预计',        -0.10),
    (r'预期',        -0.10),
    (r'接近',        -0.10),
    (r'知情人士',    -0.15),
    (r'据.*透露',    -0.12),
    (r'传.*称',      -0.15),
    (r'疑似',        -0.12),
    (r'疑似.*为',    -0.15),
    (r'未.*确认',    -0.10),
    (r'市场.*认为',  -0.08),
    (r'机构.*表示',  -0.08),
    (r'或因',        -0.15),
    (r'消息人士',    -0.15),
    (r'接近.*人士',  -0.10),
    (r'不排除',      -0.10),
    (r'有待.*证实',  -0.10),
    (r'所谓',        -0.08),
    (r'传闻',        -0.05),   # 单独"传闻"字样
    (r'炒作',        -0.08),
    (r'疑似',        -0.12),
]

# ─── 确定性信号词（加分项）────────────────────────────
# 具体、可信、有行动意义
CONCRETE_SIGNALS = [
    (r'涨停',         0.20),
    (r'跌停',         0.20),
    (r'净利润.*亿',   0.15),
    (r'\d+\.?\d*%',  0.12),   # 具体数字+百分号
    (r'\d+亿',        0.12),   # 具体金额
    (r'\d+万',        0.08),
    (r'签署|签订',     0.15),
    (r'订单.*亿',     0.18),
    (r'战略合作',      0.15),
    (r'重大突破',      0.18),
    (r'首发|上市',    0.15),
    (r'全球.*首款',   0.20),
    (r'首创',          0.15),
    (r'独家',          0.10),
    (r'首发',          0.15),
    (r'规模.*亿',     0.12),
    (r'投资.*亿',     0.15),
    (r'回购',          0.12),
    (r'增持',          0.12),
    (r'分红',          0.10),
    (r'复牌',          0.15),
    (r'停牌',         -0.10),   # 停牌本身偏中性，算中性事件
    (r'收购',          0.18),
    (r'并购',          0.18),
    (r'重组',          0.15),
    (r'更名',          0.05),
    (r'摘帽',          0.15),
    (r'ST',           -0.05),   # ST类谨慎
    (r'退市',         -0.20),
]

# ─── 利好/利空情感词───────────────────────────────
POSITIVE_SENTIMENT = [
    '涨停', '大涨', '飙升', '爆发', '突破', '创新高', '强劲',
    '业绩增长', '净利润增长', '营收增长', '超预期', '大单',
    '中标', '签约', '战略合作', '突破', '首款', '首创',
    '订单激增', '市场份额提升', '获批', '新品发布',
    '净流入', '增持', '回购', '分红', '提振',
]

NEGATIVE_SENTIMENT = [
    '跌停', '大跌', '暴跌', '亏损', '净利下滑', '营收下降',
    '召回', '调查', '涉嫌', '违规', '处罚', '警示函',
    '减持', '清仓', '业绩变脸', '商誉减值', '诉讼',
    '终止', '取消', '推迟', '延期', '产能过剩',
]

# ─── 来源权威性权重───────────────────────────────
OFFICIAL_SOURCE_BONUS = {
    '证监会': 0.15,
    '国务院': 0.15,
    '财政部': 0.15,
    '发改委': 0.12,
    '工信部': 0.12,
    '央行':   0.12,
    '银保监会': 0.12,
    '交易所': 0.10,
    '上交所': 0.10,
    '深交所': 0.10,
    '中证报': 0.08,
    '上证报': 0.08,
    '证券时报': 0.08,
    '中国基金报': 0.08,
    '经济参考报': 0.08,
    '人民日报': 0.10,
    '新华社': 0.10,
}

# ─── 主评分函数───────────────────────────────

def score_news_item(title: str) -> float:
    """
    对单条新闻标题评分（0.0 ~ 1.0）。

    基础分 0.5，各维度在此基础上加减。
    """
    if not title:
        return 0.0

    score = 0.5
    t = title

    # ── 1. 含糊指示词（减分）────────────────────
    for pattern, weight in VAGUE_PHRASES:
        if re.search(pattern, t):
            score += weight  # weight 本身为负

    # ── 2. 确定性信号（加分）────────────────────
    for pattern, weight in CONCRETE_SIGNALS:
        if re.search(pattern, t):
            score += weight

    # ── 3. 来源权威性（加分）────────────────────
    for source, bonus in OFFICIAL_SOURCE_BONUS.items():
        if source in t:
            score += bonus
            break  # 不叠加，取最高来源

    # ── 4. 情感方向（小幅调整）──────────────────
    pos_matches = sum(1 for w in POSITIVE_SENTIMENT if w in t)
    neg_matches = sum(1 for w in NEGATIVE_SENTIMENT if w in t)
    if pos_matches > neg_matches:
        score += 0.05 * (pos_matches - neg_matches)
    elif neg_matches > pos_matches:
        score += -0.05 * (neg_matches - pos_matches)

    # ── 5. 标题长度（过长/过短都可疑）──────────────
    # 太短可能是单一符号词，太长可能是标题党
    word_count = len(t)
    if 8 <= word_count <= 40:
        score += 0.03   # 正常长度小幅加分
    elif word_count < 5:
        score -= 0.10   # 太短，信息量不足
    elif word_count > 60:
        score -= 0.05   # 过长，可能标题党

    # ── 6. 数字密度（数字越多越具体）──────────────
    number_count = len(re.findall(r'\d+\.?\d*%?', t))
    if number_count >= 2:
        score += 0.08
    elif number_count == 1:
        score += 0.04

    return max(0.0, min(1.0, round(score, 3)))


def score_and_filter_news(news_list: list[Dict],
                           min_quality: float = 0.35,
                           top_n: int = 20) -> list[Dict]:
    """
    对新闻列表批量评分，过滤低质量，返回排序结果。

    Args:
        news_list: [{title, time, hot_value, url}, ...]
        min_quality: 最低质量阈值，低于此丢弃
        top_n: 最多返回条数

    Returns:
        [{title, time, hot_value, url, quality, quality_grade}, ...]
        quality_grade: 'A' / 'B' / 'C' / 'D'
    """
    scored = []
    for item in news_list:
        title = item.get('title', '')
        quality = score_news_item(title)
        quality_grade = (
            'A' if quality >= 0.65
            else 'B' if quality >= 0.50
            else 'C' if quality >= 0.35
            else 'D'
        )
        scored.append({
            **item,
            'quality': quality,
            'quality_grade': quality_grade,
        })

    # 过滤并按质量降序
    filtered = [n for n in scored if n['quality'] >= min_quality]
    filtered.sort(key=lambda x: x['quality'], reverse=True)
    return filtered[:top_n]


def quality_grade_label(grade: str) -> str:
    """质量等级说明"""
    return {
        'A': '强信号（具体+权威+无含糊词）',
        'B': '正常信号（有一定信息量）',
        'C': '低质量（含糊或信息不足）',
        'D': '噪声（高度含糊或不可靠）',
    }.get(grade, '')
