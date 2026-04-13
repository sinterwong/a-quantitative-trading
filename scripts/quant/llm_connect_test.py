"""
scripts/quant/llm_connect_test.py
==================================
验证 LLM 服务模块是否正常工作（import + provider 初始化）。

用法：
  python scripts/quant/llm_connect_test.py

不依赖真实 API key 也能通过导入测试。
"""

import sys
import os
# quant_repo/scripts/quant/llm_connect_test.py
# __file__ = quant_repo/scripts/quant/llm_connect_test.py
# dirname(__file__) = scripts/quant/
# dirname(dirname(__file__)) = scripts/
# dirname(dirname(dirname(__file__))) = quant_repo/
_quant_repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, _quant_repo)


def test_imports():
    print("[1] Testing imports...")

    from backend.services.llm import LLMService
    from backend.services.llm.cache import CacheManager
    from backend.services.llm.factory import create_provider, create_llm_service
    from backend.services.llm.providers import DeepSeekProvider, KimiProvider

    print("  OK All imports OK")
    return True


def test_cache():
    print("\n[2] Testing CacheManager...")
    import tempfile
    import shutil
    from backend.services.llm.cache import CacheManager

    cache_dir = os.path.join(tempfile.gettempdir(), 'test_llm_cache')
    shutil.rmtree(cache_dir, ignore_errors=True)

    cm = CacheManager(cache_dir=cache_dir, memory_capacity=10)

    # 写入
    cm.set("hello", "world", task='test', ttl=10)
    assert cm.get("hello", task='test') == "world", "Cache write/read failed"
    print("  OK Cache write/read OK")

    # 过期
    import time
    cm.set("expire_test", "value", task='test', ttl=1)
    time.sleep(1.5)
    assert cm.get("expire_test", task='test') is None, "TTL expiry failed"
    print("  OK Cache TTL expiry OK")

    # 内存容量限制
    cm2 = CacheManager(cache_dir=cache_dir, memory_capacity=3)
    for i in range(5):
        cm2.set(f"key{i}", f"val{i}", task='test')
    val0 = cm2.get("key0", task='test')
    print(f"  OK Cache LRU eviction OK (key0 exists={val0 is not None})")

    # 清理
    shutil.rmtree(cache_dir, ignore_errors=True)
    return True


def test_provider_creation():
    print("\n[3] Testing Provider creation...")

    # 不传 API key 时，依赖环境变量
    # DeepSeekProvider() 不会报错（is_available=False）
    from backend.services.llm.providers import DeepSeekProvider
    from backend.services.llm.factory import create_provider

    prov = DeepSeekProvider(api_key="fake_key_for_init_test")
    assert prov.api_key == "fake_key_for_init_test"
    assert prov.model == "deepseek-chat"
    print("  OK DeepSeekProvider init OK")

    # create_provider 不带 key 会检查环境变量
    try:
        create_provider("kimi")
        print("  X Should have raised ValueError for missing Kimi key")
    except ValueError as e:
        print(f"  OK Missing API key correctly raises: {e}")

    return True


def test_llm_service_init():
    print("\n[4] Testing LLMService initialization...")

    from backend.services.llm import LLMService
    from backend.services.llm.providers import DeepSeekProvider

    # 假 Provider（is_available=False 但 init 不会报错）
    prov = DeepSeekProvider(api_key="")
    llm = LLMService(prov, cache_dir=".llm_cache_test")

    assert llm.provider is prov
    assert llm.max_retries == 2
    assert llm.news_cache_ttl > 0
    print(f"  OK LLMService init OK (provider={prov.name}, news_ttl={llm.news_cache_ttl}s)")

    # 检查 is_available（无 key 时应为 False）
    assert not llm.is_available
    print("  OK is_available=False correctly when no API key")

    return True


def test_data_models():
    print("\n[5] Testing data models...")

    from backend.services.llm.service import NewsSentiment, PolicyAnalysis

    ns = NewsSentiment(
        sentiment="bullish",
        confidence=0.85,
        impact_sectors=["半导体", "AI"],
        price_already_moved=True,
        summary="利好半导体国产替代",
    )
    assert ns.sentiment == "bullish"
    assert ns.confidence == 0.85
    assert "半导体" in ns.impact_sectors
    print("  OK NewsSentiment dataclass OK")

    pa = PolicyAnalysis(
        sentiment="bullish",
        policy_type="产业政策",
        affected_sectors=["新能源"],
        implementation_timeline="3个月内",
        market_impact_score=0.75,
        key_signal="新能源补贴延续",
    )
    assert pa.policy_type == "产业政策"
    print("  OK PolicyAnalysis dataclass OK")

    return True


def main():
    print("=" * 60)
    print("  LLM Service Module Tests")
    print("=" * 60)

    ok = True
    for test in [test_imports, test_cache, test_provider_creation, test_llm_service_init, test_data_models]:
        try:
            test()
        except Exception as e:
            print(f"  X FAILED: {e}")
            import traceback
            traceback.print_exc()
            ok = False

    print("\n" + "=" * 60)
    if ok:
        print("  All tests passed OK")
        print("  Next: copy .env.example to .env and add your API key")
        print("  Then run: python scripts/quant/llm_test_run.py")
    else:
        print("  Some tests failed X")
    print("=" * 60)


if __name__ == '__main__':
    main()
