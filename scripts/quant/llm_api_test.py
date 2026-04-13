"""
scripts/quant/llm_api_test.py
=============================
真实 API 连通性测试（读取 .env，发实际请求）。

用法：
  python scripts/quant/llm_api_test.py
"""

import os
import sys

# ── 加载 .env ──────────────────────────────────────────────────────────
_dotenv_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env')
if os.path.exists(_dotenv_path):
    with open(_dotenv_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

# ── 路径设置 ──────────────────────────────────────────────────────────
_quant_repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _quant_repo)

from backend.services.llm import LLMService
from backend.services.llm.providers import MiniMaxProvider


def test_provider_init():
    print("[1] Provider 初始化...")
    prov = MiniMaxProvider()
    print(f"  model={prov.model}")
    print(f"  is_available={prov.is_available}")
    assert prov.is_available, "API key 未配置"
    print("  OK")
    return prov


def test_llm_service(prov):
    print("\n[2] LLMService 初始化...")
    llm = LLMService(prov, cache_dir=".llm_cache")
    print(f"  provider={llm.provider.name}")
    print(f"  is_available={llm.is_available}")
    assert llm.is_available
    print("  OK")
    return llm


def test_news_sentiment(llm):
    print("\n[3] 新闻情感分析（同步）...")
    result = llm.analyze_news(
        "央行宣布下调存款准备金率0.5个百分点，释放长期资金约1万亿元",
        timeout=15,
    )
    print(f"  sentiment   = {result.sentiment}")
    print(f"  confidence  = {result.confidence}")
    print(f"  sectors     = {result.impact_sectors}")
    print(f"  already_mved= {result.price_already_moved}")
    print(f"  summary     = {result.summary}")
    assert result.sentiment in ('bullish', 'bearish', 'neutral'), "Invalid sentiment"
    assert 0.0 <= result.confidence <= 1.0, "Confidence out of range"
    print("  OK")
    return result


def test_cache(llm):
    print("\n[4] 缓存测试...")
    text = "证监会发布《上市公司信息披露管理办法》"
    r1 = llm.analyze_news(text, timeout=15)
    r2 = llm.analyze_news(text, timeout=15)
    assert r2.from_cache, "Second call should hit cache"
    print(f"  first call  : cache={r1.from_cache}")
    print(f"  second call : cache={r2.from_cache}")
    print("  OK")


def test_policy_analysis(llm):
    print("\n[5] 政策解读...")
    result = llm.analyze_policy(
        "国务院办公厅发布《关于进一步优化营商环境的意见》，"
        "提出推进注册制改革、简化行政审批、完善市场监管等多项措施",
        timeout=20,
    )
    print(f"  sentiment   = {result.sentiment}")
    print(f"  policy_type = {result.policy_type}")
    print(f"  sectors     = {result.affected_sectors}")
    print(f"  timeline    = {result.implementation_timeline}")
    print(f"  impact      = {result.market_impact_score}")
    print(f"  key_signal  = {result.key_signal}")
    assert result.sentiment in ('bullish', 'bearish', 'neutral')
    print("  OK")


def test_batch_news(llm):
    print("\n[6] 批量新闻分析...")
    news_list = [
        {"title": "宁德时代发布一季度财报，净利润同比增长超200%"},
        {"title": "央行表态货币政策保持稳健，不搞大水漫灌"},
        {"title": "多地出台楼市调控新政，房价过快上涨势头得到遏制"},
    ]
    results = llm.batch_news(news_list, text_field='title', max_concurrency=2)
    for r in results:
        sr = r['sentiment_result']
        print(f"  [{sr.sentiment:8s}] {r['title'][:30]} conf={sr.confidence:.2f}")
    print("  OK")


def main():
    print("=" * 60)
    print("  MiniMax API Real Call Test")
    print("=" * 60)

    try:
        prov = test_provider_init()
        llm = test_llm_service(prov)
        test_news_sentiment(llm)
        test_cache(llm)
        test_policy_analysis(llm)
        test_batch_news(llm)

        print("\n" + "=" * 60)
        print("  All tests passed!")
        print("=" * 60)

    except Exception as e:
        print(f"\n  FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
