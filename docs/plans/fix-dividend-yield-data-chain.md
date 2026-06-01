# 股息率数据链路修复实施计划

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** 修复 A 股股息率(dividend_yield)在 5 个数据链路层级的失效问题，使 `backend.services.fundamentals.fetch_fundamentals("600809.SH")` 能返回与"2024 年报分红 ÷ 实时股价"一致的 TTM 股息率（汾酒实测 ~5.1%，而非腾讯失真的 0.60%）。

**Architecture:** 不引入新数据源；按"合并优先级反转"→"Provider 字段补全"→"权威重整"三层递进修复。Layer 4（合并优先级）独立最小改动；Layer 1 增强 Provider 字段覆盖；Layer 2 降权威防误导。Layer 3 在项目专用 conda 环境下已正常工作（原诊断误判）→ 改为"添加缓存预热 + 离线降级注释"。

**Tech Stack:** Python 3.11.15 · pandas 3.0.2 · akshare 1.18.60 · baostock 0.0.99 · pytest · dataclass(MERGE_FIELDS 路由)

---

## ⚠️ 重要环境澄清（2026-06-01 复测）

| 环境 | Python | akshare | baostock | 600809 股息率 |
|---|---|---|---|---|
| `~/.hermes/hermes-agent/venv/`（初始诊断时） | 3.11.15 | ✅ 1.18.60 | ❌ **未装** | 0.6（Layer 3 失败）|
| `~/softwares/miniconda3/envs/quant-trading/`（**项目专用环境**）| 3.11.15 | ✅ 1.18.60 | ✅ **0.0.99** | **4.75%（正确）** |

**结论:**
- 项目部署环境是 `quant-trading` conda env（有 baostock），Layer 3 实际**没问题**
- 原诊断误用 hermes-agent venv，导致 Layer 3 误判
- **删除 Task 3 中"新增 akshare 分红 provider"**（不必要，baostock 已是更权威源）
- **保留 Task 3 改造为"缓存预热"**（提升冷启动性能）
- Layer 4 合并优先级反转 **仍然是核心 bug 修复**（即使有 baostock，腾讯失真值仍有被并入的隐患）

---

## 背景：5 层数据链路现状（已诊断）

| Layer | 文件 | 现状 | 真实值 vs 系统值 |
|---|---|---|---|
| 1. A 股 akshare `_fetch_a_share_fundamentals` | `core/data_gateway/providers/akshare.py:290` | `dividend_yield=0.0` 写死 | 系统 0.0 / 真实 ~5.1% |
| 2. 腾讯 88-field 字段 56 | `core/data_gateway/providers/tencent.py:149,241` | 返回 0.60（"动态股息率"）权威 1.2 | 系统 0.60 / 失真 |
| 3. Baostock TTM 补算 | `core/data_gateway/gateway.py:966-992` | quant-trading 环境下 **正常**（4.75%）；其他环境缺 baostock 时失败 | 4.75% / ✅ 实际可用 |
| 4. 后端合并优先级 | `backend/services/fundamentals.py:65` | `f_dy if f_dy else q.dividend_yield`，前者为 0 时回退到腾讯失真值 | **致命 bug** |
| 5. analyze_stock 层 | `core/use_cases/analyze_stock/*` | 有 TTM 估算但没回填 | 估算 ~1.3% / 不覆盖 |

**诊断脚本输出（已实测）：**
```
Tencent:       dividend_yield = 0.6     ← 失真
Fundamentals:  dividend_yield = 0.0     ← 链路全断
gw.dividend(): 0 records                ← baostock 不可用
Backend svc:   dividend_yield = 0.6     ← 回退到腾讯
```

---

## 修复总览

按**收益/成本**排序四个修复点 + 一个回归测试任务：

| # | 任务 | 修复层 | 收益 | 估时 |
|---|---|---|---|---|
| 1 | 反转后端合并优先级 + 加 0 标记 | Layer 4 | 立即生效 | 10 min |
| 2 | 补 akshare `_fetch_a_share_fundamentals` 从 `stock_zh_a_spot_em` 取股息率 | Layer 1 | 补齐 A 股字段 | 20 min |
| 3 | 缓存预热：分析时按需调 dividend() 避免冷启动 | Layer 3 增强 | 性能优化 | 20 min |
| 4 | 降低腾讯 `dividend_yield` 字段权威 | Layer 2 | 防再次误导 | 5 min |
| 5 | 回归测试：验证 600809.SH dividend_yield | 全链路 | 防回归 | 20 min |

---

## Task 1: 修复 Layer 4 合并优先级（最高优先级）

**Objective:** 修改 `backend/services/fundamentals.py:65`，使合并顺序为 "缓存 > 全 A 股快照 > 腾讯"，并在 `Fundamentals.dividend_yield=0` 时显式标 `dividend_yield_unavailable=True`，避免无声回退到失真值。

**Files:**
- Modify: `backend/services/fundamentals.py:51-65`
- Test: `tests/backend/test_fundamentals_service.py` (新建)

### Step 1.1: 写失败测试

**文件:** `tests/backend/test_fundamentals_service.py`

```python
"""验证 fetch_fundamentals 在 dividend_yield 链路全断时，不会回退到腾讯失真值。"""
from unittest.mock import MagicMock, patch
import pytest


def test_fetch_fundamentals_does_not_fallback_to_tencent_when_fundamentals_zero():
    """当 Fundamentals.dividend_yield=0（链路全断），应明确返回 None/0 标记，
    而不是无声回退到腾讯 88-field 的'动态股息率'值。

    背景: 山西汾酒 600809.SH 案例，腾讯返回 0.60（失真），真实 TTM 应为 ~5.1%。
    修复前: 返回 0.60（误导）；修复后: 返回 0.0 并标记 unavailable。
    """
    from backend.services.fundamentals import fetch_fundamentals

    mock_quote = MagicMock()
    mock_quote.is_valid = True
    mock_quote.name = "山西汾酒"
    mock_quote.pe_ttm = 14.19
    mock_quote.pb = 3.46
    mock_quote.dividend_yield = 0.60  # 腾讯失真
    mock_quote.market_cap = 1552.0
    mock_quote.price = 127.22

    mock_fundamentals = MagicMock()
    mock_fundamentals.dividend_yield = 0.0  # akshare 写死 / baostock 失败
    mock_fundamentals.revenue_yoy = -9.68
    mock_fundamentals.profit_yoy = -19.03
    mock_fundamentals.roe_ttm = 12.57
    mock_fundamentals.eps_ttm = 4.41
    mock_fundamentals.ocf_to_profit = 1.53
    mock_fundamentals.industry = "白酒"
    mock_fundamentals.sector = "消费"

    with patch("core.data_gateway.get_gateway") as mock_gw:
        mock_gw.return_value.quote.return_value = mock_quote
        mock_gw.return_value.fundamentals.return_value = mock_fundamentals

        result = fetch_fundamentals("600809.SH")

    # 关键断言：不再回退到腾讯失真值
    assert result["dividend_yield"] == 0.0, (
        f"Expected 0.0 (链路全断) but got {result['dividend_yield']} "
        f"(可能回退到腾讯 88-field 失真值)"
    )
    # 必须有不可用标记
    assert result.get("dividend_yield_unavailable") is True


def test_fetch_fundamentals_uses_fundamentals_when_positive():
    """正常路径: Fundamentals.dividend_yield > 0 时优先用。"""
    from backend.services.fundamentals import fetch_fundamentals

    mock_quote = MagicMock()
    mock_quote.is_valid = True
    mock_quote.name = "测试股"
    mock_quote.pe_ttm = 10.0
    mock_quote.pb = 1.5
    mock_quote.dividend_yield = 0.02
    mock_quote.market_cap = 100.0
    mock_quote.price = 10.0

    mock_fundamentals = MagicMock()
    mock_fundamentals.dividend_yield = 5.1  # 真实 TTM
    mock_fundamentals.revenue_yoy = 0.0
    mock_fundamentals.profit_yoy = 0.0
    mock_fundamentals.roe_ttm = 0.0
    mock_fundamentals.eps_ttm = 1.0
    mock_fundamentals.ocf_to_profit = 0.0
    mock_fundamentals.industry = ""
    mock_fundamentals.sector = ""

    with patch("core.data_gateway.get_gateway") as mock_gw:
        mock_gw.return_value.quote.return_value = mock_quote
        mock_gw.return_value.fundamentals.return_value = mock_fundamentals

        result = fetch_fundamentals("test.SH")

    assert result["dividend_yield"] == 5.1
    assert result.get("dividend_yield_unavailable") is False
```

### Step 1.2: 跑测试确认失败

```bash
cd /home/sinter/workspace/a-quantitative-trading
pytest tests/backend/test_fundamentals_service.py -v
```

**预期:** `test_fetch_fundamentals_does_not_fallback_to_tencent_when_fundamentals_zero` FAIL（`AssertionError: Expected 0.0 but got 0.6`）。第二个测试可能也 FAIL 因为字段结构还没加 `dividend_yield_unavailable`。

### Step 1.3: 改实现

**文件:** `backend/services/fundamentals.py:51-65`，替换为：

```python
        # 股息率合并优先级（修复山西汾酒 0.60% vs 5.1% 失真案例）:
        #   1. Fundamentals(已含 _calc_ttm_dividend_yield 兜底) — 唯一可信源
        #   2. **不再回退到腾讯 88-field 字段 56**(那是腾讯自算的"动态股息率"，
        #      与 A 股 TTM 标准口径不一致，对高分红的价值股会严重失真)
        #   3. 显式标记 unavailable，让上游决定是否使用兜底（如历史均值）
        f_dy = 0.0
        if f is not None:
            _raw = getattr(f, 'dividend_yield', 0.0)
            if isinstance(_raw, (int, float)):
                f_dy = float(_raw)

        # 保留 q.dividend_yield 仅供参考(对外可观测的 raw 字段)，
        # 但不作为主合并值，避免静默失真。
        q_dy_raw = 0.0
        if q is not None:
            _q_raw = getattr(q, 'dividend_yield', 0.0)
            if isinstance(_q_raw, (int, float)):
                q_dy_raw = float(_q_raw)

        # 构建返回数据
        result = {
            # 基础字段
            'symbol': symbol,
            'name': q.name,
            'pe': q.pe_ttm,
            'pb': q.pb,
            'dividend_yield': f_dy,                          # ← 关键:不再回退到 q.dividend_yield
            'dividend_yield_unavailable': f_dy <= 0,         # ← 显式标记
            'dividend_yield_tencent_raw': q_dy_raw,          # ← 保留 raw 供调试
            'market_cap': q.market_cap,  # 亿元
            'price': q.price,

            # 扩展财务指标
            'revenue_yoy': f.revenue_yoy if f else 0.0,
            'profit_yoy': f.profit_yoy if f else 0.0,
            'roe_ttm': f.roe_ttm if f else 0.0,
            'eps_ttm': f.eps_ttm if f else 0.0,
            'ocf_to_profit': f.ocf_to_profit if f else 0.0,
            'industry': f.industry if f else '',
            'sector': f.sector if f else '',
        }
```

### Step 1.4: 跑测试确认通过

```bash
pytest tests/backend/test_fundamentals_service.py -v
```

**预期:** 2 passed

### Step 1.5: 提交

```bash
git add backend/services/fundamentals.py tests/backend/test_fundamentals_service.py
git commit -m "fix(fundamentals): 修复 dividend_yield 回退到腾讯失真值的 bug

合并优先级反转: Fundamentals(TTM 兜底) > 不再回退到腾讯 88-field 字段 56
新增 dividend_yield_unavailable 显式标记 + dividend_yield_tencent_raw 调试字段
参考山西汾酒 600809.SH 案例: 腾讯 0.60% vs 真实 5.1%"
```

---

## Task 2: 补 Layer 1 A 股 akshare 股息率字段

**Objective:** 修改 `core/data_gateway/providers/akshare.py:_fetch_a_share_fundamentals`，从 `ak.stock_zh_a_spot_em()` 拉取全 A 股快照，提取对应代码的"股息率"列写入 `Fundamentals.dividend_yield`。该接口单次返回全 A 股 ~5000 行，命中性能可接受。

**Files:**
- Modify: `core/data_gateway/providers/akshare.py:223-296`
- Test: `tests/test_data_gateway/test_provider_akshare_dividend.py` (新建)

### Step 2.1: 写失败测试

**文件:** `tests/test_data_gateway/test_provider_akshare_dividend.py`

```python
"""验证 akshare _fetch_a_share_fundamentals 能从 stock_zh_a_spot_em 补全 dividend_yield。"""
from unittest.mock import MagicMock, patch
import pandas as pd
import pytest


@pytest.fixture
def mock_spot_em():
    """模拟 akshare.stock_zh_a_spot_em() 全 A 股快照。"""
    return pd.DataFrame({
        "代码": ["600809", "000001", "600519"],
        "名称": ["山西汾酒", "平安银行", "贵州茅台"],
        "最新价": [127.22, 12.5, 1680.0],
        "股息率": [5.12, 3.20, 1.45],  # 关键字段: %
    })


def test_a_share_fundamentals_includes_dividend_yield_from_spot_em(mock_spot_em):
    """_fetch_a_share_fundamentals 应从 stock_zh_a_spot_em 补全 dividend_yield。"""
    from core.data_gateway.providers.akshare import AkshareProvider

    provider = AkshareProvider()
    mock_ak = MagicMock()
    # financial_abstract 返回最新季报
    mock_ak.stock_financial_abstract.return_value = pd.DataFrame({
        "选项": ["常用指标", "常用指标", "成长能力", "成长能力"],
        "指标": ["基本每股收益", "归母净利润", "营业总收入增长率", "归属母公司净利润增长率"],
        "20250331": [4.41, 100.0, -9.68, -19.03],
    })
    mock_ak.stock_zh_a_spot_em.return_value = mock_spot_em

    with patch.object(provider, "_is_hk_symbol", return_value=False):
        with patch("core.data_gateway.providers.akshare.pd.Timestamp") as mock_ts:
            result = provider._fetch_a_share_fundamentals("600809.SH", mock_ak)

    # 关键断言: dividend_yield 不再是写死的 0.0
    assert result is not None
    assert result.dividend_yield == 5.12, (
        f"Expected 5.12 (from stock_zh_a_spot_em) but got {result.dividend_yield}"
    )


def test_a_share_fundamentals_handles_spot_em_failure(mock_spot_em):
    """stock_zh_a_spot_em 失败时优雅降级到 0.0（不抛异常）。"""
    from core.data_gateway.providers.akshare import AkshareProvider

    provider = AkshareProvider()
    mock_ak = MagicMock()
    mock_ak.stock_financial_abstract.return_value = pd.DataFrame({
        "选项": ["常用指标"], "指标": ["基本每股收益"], "20250331": [4.41],
    })
    mock_ak.stock_zh_a_spot_em.side_effect = Exception("network error")

    with patch.object(provider, "_is_hk_symbol", return_value=False):
        result = provider._fetch_a_share_fundamentals("600809.SH", mock_ak)

    # 降级但不崩
    assert result is not None
    assert result.dividend_yield == 0.0
```

### Step 2.2: 跑测试确认失败

```bash
cd /home/sinter/workspace/a-quantitative-trading
pytest tests/test_data_gateway/test_provider_akshare_dividend.py -v
```

**预期:** `test_a_share_fundamentals_includes_dividend_yield_from_spot_em` FAIL（`assert 0.0 == 5.12`），因为当前实现是写死 0.0。

### Step 2.3: 改实现

**文件:** `core/data_gateway/providers/akshare.py`，在 `_fetch_a_share_fundamentals` 中**插入新的辅助函数**和**修改 dividend_yield 写入**：

在 `_fetch_a_share_fundamentals` 函数体（line 248 之前）插入：

```python
        def get_dividend_yield_from_spot(code: str) -> float:
            """从 stock_zh_a_spot_em 全 A 股快照取股息率(%)。失败返回 0.0。"""
            try:
                spot = ak.stock_zh_a_spot_em()
                if spot is None or spot.empty:
                    return 0.0
                # 列名兼容: akshare 不同版本可能是"股息率"或"股息率(%)"
                col = next((c for c in spot.columns if "股息率" in c), None)
                if col is None:
                    return 0.0
                row = spot[spot["代码"].astype(str) == code]
                if row.empty:
                    return 0.0
                val = row.iloc[0][col]
                f = float(val)
                return f if f == f else 0.0  # NaN check
            except Exception:
                return 0.0
```

在 `return Fundamentals(...)`（line 275-296）中**替换** `dividend_yield=0.0,` 为：

```python
            dividend_yield=get_dividend_yield_from_spot(code),  # W1-1 补全:从全 A 股快照取股息率
```

### Step 2.4: 跑测试确认通过

```bash
pytest tests/test_data_gateway/test_provider_akshare_dividend.py -v
```

**预期:** 2 passed

### Step 2.5: 跑现有 akshare 测试防回归

```bash
pytest tests/test_data_gateway/test_provider_yfinance_akshare.py -v
```

**预期:** 全部 passed（若失败说明接口契约变了，需要回看）

### Step 2.6: 提交

```bash
git add core/data_gateway/providers/akshare.py tests/test_data_gateway/test_provider_akshare_dividend.py
git commit -m "fix(akshare): A 股路径从 stock_zh_a_spot_em 补全 dividend_yield 字段

原实现 dividend_yield=0.0 写死，因 stock_financial_abstract 不含此字段。
新增 get_dividend_yield_from_spot 辅助函数，从全 A 股快照取股息率(%)。
失败时优雅降级到 0.0，不影响主流程。
参考: 山西汾酒 600809.SH dividend_yield=5.12%"
```

---

## Task 3: Layer 3 缓存预热（替换原 akshare 分红兜底方案）

**Objective:** 原计划"新增 akshare 分红 provider 兜底"在确认 quant-trading 环境下 baostock 工作正常后已**不必要**。改为：① 在 `gw.fundamentals()` 触发 dividend_yield 兜底时，自动预热 `gw.dividend()` 缓存；② 增强错误日志，baostock 不可用时给出明确提示。

**Files:**
- Modify: `core/data_gateway/gateway.py:940-952`（在 dividend_yield 兜底分支加缓存预热）
- Test: `tests/test_data_gateway/test_gateway_dividend_cache.py` (新建)

### Step 3.1: 写失败测试

**文件:** `tests/test_data_gateway/test_gateway_dividend_cache.py`

```python
"""验证 gw.fundamentals() 触发 dividend_yield 兜底时，自动预热 dividend() 缓存，
避免分析时冷启动延迟。"""
from unittest.mock import MagicMock, patch
import pytest


def test_fundamentals_warms_dividend_cache_on_fallback():
    """当 fundamentals.dividend_yield<=0 触发兜底时，应先调一次 dividend() 预热缓存。"""
    from core.data_gateway import get_gateway

    gw = get_gateway()

    # 强制走兜底路径：f.dividend_yield=0
    f = MagicMock()
    f.dividend_yield = 0.0
    f.pe_ttm = 14.0
    f.eps_ttm = 4.0
    f.roe_ttm = 12.0
    f.revenue_yoy = -9.0
    f.profit_yoy = -19.0
    f.ocf_to_profit = 1.5
    f.industry = ""
    f.sector = ""

    with patch.object(gw, "_route", return_value=(f, {})):
        with patch.object(gw, "quote", return_value=MagicMock(price=127.0)):
            with patch.object(gw, "dividend") as mock_div:
                mock_div.return_value = []
                with patch.object(gw, "_calc_ttm_dividend_yield", return_value=0.0):
                    try:
                        gw.fundamentals("600809.SH")
                    except Exception:
                        pass

                # 关键: 即便 _calc_ttm_dividend_yield 失败，dividend() 也被调用过（即预热了缓存）
                assert mock_div.called, "dividend() 应被调用以预热缓存"


def test_fundamentals_logs_warning_when_baostock_missing():
    """baostock 不可用导致兜底失败时，应 logger.warning 而非静默 pass。"""
    from core.data_gateway import get_gateway
    import logging

    gw = get_gateway()
    f = MagicMock()
    f.dividend_yield = 0.0
    f.pe_ttm = 14.0
    f.eps_ttm = 4.0

    with patch.object(gw, "_route", return_value=(f, {})):
        with patch.object(gw, "quote", return_value=MagicMock(price=127.0)):
            with patch.object(gw, "dividend", side_effect=Exception("No module named 'baostock'")):
                with patch("core.data_gateway.gateway.logger") as mock_log:
                    try:
                        gw.fundamentals("600809.SH")
                    except Exception:
                        pass

                    # 应有 warning 记录 baostock 不可用
                    warning_calls = [
                        call for call in mock_log.warning.call_args_list
                        if "baostock" in str(call).lower() or "dividend" in str(call).lower()
                    ]
                    assert len(warning_calls) > 0, (
                        "应 logger.warning 提示 baostock 不可用，便于运维定位"
                    )
```

### Step 3.2: 跑测试确认失败

```bash
cd /home/sinter/workspace/a-quantitative-trading
pytest tests/test_data_gateway/test_gateway_dividend_cache.py -v
```

**预期:** 第一个测试可能 PASS（因为现状下调 dividend() 的逻辑已存在），第二个测试 FAIL（因为现状用 `pass` 静默吞掉异常）。

### Step 3.3: 改实现

**文件:** `core/data_gateway/gateway.py:940-952`，替换兜底分支：

```python
            # 股息率补充：当合并结果为 0 时，从分红记录计算 TTM 股息率
            if merged.dividend_yield <= 0:
                try:
                    # 预热 dividend() 缓存: 后续分析可能复用
                    records = self.dividend(symbol)
                    price = self._resolve_price(symbol, merged)
                    if price > 0 and records:
                        ttm_div = self._calc_ttm_dividend_yield(symbol, price)
                        if ttm_div > 0:
                            merged.dividend_yield = ttm_div
                            prov["dividend_yield"] = "dividend_records"
                    elif price > 0 and not records:
                        # dividend() 返回空，说明 baostock 不可用或该股无分红
                        logger.warning(
                            "dividend_yield 兜底失败: %s 无分红记录"
                            "(可能是 baostock 未安装，或该股票从未分红)",
                            symbol,
                        )
                except Exception as exc:
                    logger.warning(
                        "dividend_yield 兜底失败: %s, exc=%s "
                        "(检查 baostock 库是否安装: pip install baostock)",
                        symbol, exc,
                    )
```

并确认 gateway.py 顶部有 `import logging` 和 `logger = logging.getLogger(...)`（应已有）。

### Step 3.4: 跑测试确认通过

```bash
pytest tests/test_data_gateway/test_gateway_dividend_cache.py -v
```

**预期:** 2 passed

### Step 3.5: 端到端验证（确认 quant-trading 环境 Layer 3 工作正常）

```bash
export PATH="/home/sinter/softwares/miniconda3/envs/quant-trading/bin:$PATH"
cd /home/sinter/workspace/a-quantitative-trading
python -c "
from core.data_gateway import get_gateway
gw = get_gateway()
records = gw.dividend('600809.SH')
print(f'dividend records: {len(records)}')
for r in records[:3]:
    print(f'  {r.operate_date.date()} cash={r.cash_per_share:.4f}元')
"
```

**预期:** 4 条记录（与今天复测一致），即 Layer 3 已在项目环境下正常工作。

### Step 3.6: 提交

```bash
git add core/data_gateway/gateway.py tests/test_data_gateway/test_gateway_dividend_cache.py
git commit -m "feat(gateway): dividend_yield 兜底时预热缓存 + 失败时显式 warning

原实现: dividend_yield<=0 触发兜底时静默 pass，baostock 不可用时无任何提示。
修复:
  1. 调 self.dividend() 提前预热缓存(若已存在则直接返回)
  2. 失败时 logger.warning 提示 baostock 状态或股票无分红
参考: 山西汾酒 600809.SH 案例，环境误诊已修正"
```

---

## Task 4: 降低 Layer 2 腾讯 dividend_yield 字段权威

**Objective:** 修改 `core/data_gateway/providers/tencent.py:241`，将 `dividend_yield` 权威从 1.2 降到 0.5（低于 baostock 1.0 和 akshare 0.8），并加注释说明该字段为"动态股息率"不可与 TTM 口径混用。

**Files:**
- Modify: `core/data_gateway/providers/tencent.py:241`
- Test: 现有 quote_authority 测试不应回归（无需新写）

### Step 4.1: 改实现

**文件:** `core/data_gateway/providers/tencent.py:237-246`，在 `"dividend_yield": 1.2,` 那一行改为：

```python
        quote_authority = {
            "pe_ttm": 1.3, "pb": 1.3, "market_cap": 1.3, "float_cap": 1.3,
            "high_52w": 1.3, "low_52w": 1.3, "turnover_rate": 1.2,
            "amplitude": 1.2, "limit_up": 1.2, "limit_down": 1.2,
            "volume_ratio": 1.2,
            # 股息率: 腾讯 88-field 字段 56 是"动态股息率"(腾讯自算口径)，
            # 与 A 股 TTM 真实股息率(=近12月分红/股价)不一致。
            # 权威降到 0.5，低于 akshare/baostock，避免在 MERGE_FIELDS 中覆盖真实值。
            "dividend_yield": 0.5,
            # bid1/ask1 由 88-field 同样返回，但声明权威低于 Sina(1.2)，
            # 让 Sina 主、腾讯备：Sina 不可用时 MERGE_FIELDS 能自动降级。
            "bid1_price": 0.9, "bid1_vol": 0.9,
            "ask1_price": 0.9, "ask1_vol": 0.9,
        }
```

### Step 4.2: 跑 quote 相关测试防回归

```bash
pytest tests/test_data_gateway/test_provider_tencent.py -v
```

**预期:** 全部 passed

### Step 4.3: 提交

```bash
git add core/data_gateway/providers/tencent.py
git commit -m "fix(tencent): 降低 dividend_yield 字段权威从 1.2 到 0.5

88-field 字段 56 是腾讯自算的'动态股息率'，与 A 股 TTM 真实股息率
(近12月分红/股价) 口径不一致。权威降到 0.5 防止在 MERGE_FIELDS
合并中覆盖 baostock(1.0) 和 akshare(0.8) 的 TTM 真实值。
参考: 山西汾酒 600809.SH 案例，腾讯 0.60% vs 真实 5.1%。"
```

---

## Task 5: 端到端回归测试

**Objective:** 综合验证 4 项修复对 600809.SH（山西汾酒）和对照股 600519.SH（贵州茅台）的影响，确认 dividend_yield 不再回退到腾讯失真值。

**Files:**
- Test: `tests/integration/test_dividend_yield_e2e.py` (新建)

### Step 5.1: 写端到端测试

**文件:** `tests/integration/test_dividend_yield_e2e.py`

```python
"""端到端验证: 修复后 dividend_yield 链路对真实 A 股(600809, 600519)行为正确。"""
import pytest
from core.data_gateway import get_gateway
from backend.services.fundamentals import fetch_fundamentals


def test_shanxi_fenjiu_dividend_yield_not_tencent_fallback():
    """山西汾酒: 修复后 dividend_yield 应来自 akshare 真实快照，
    而不是腾讯失真的 0.60%。

    注: 此测试依赖环境已装 akshare(必需) 和 baostock(可选)。
    若 baostock 不可用，akshare 应是唯一源。
    """
    gw = get_gateway()
    f = gw.fundamentals("600809.SH")

    # 不应是腾讯失真值
    if f is not None:
        q = gw.quote("600809.SH")
        tencent_dy = getattr(q, "dividend_yield", 0.0) if q else 0.0

        # 关键: 后端服务返回的不应等于腾讯失真值
        backend_result = fetch_fundamentals("600809.SH")
        if backend_result is not None:
            backend_dy = backend_result.get("dividend_yield", 0.0)
            # 要么是真实 TTM(>1%)，要么是 0(标记 unavailable)
            # 但不能等于腾讯失真的小数值
            if 0 < tencent_dy < 1.0:  # 腾讯值在小数区间即视为失真
                assert backend_dy == 0.0 or backend_dy > 2.0, (
                    f"backend dividend_yield={backend_dy} equals tencent fallback={tencent_dy}, "
                    f"失真回退 bug 未修复"
                )


def test_backend_service_marks_unavailable_when_zero():
    """当 Fundamentals.dividend_yield=0 时,后端服务必须显式标记 unavailable。"""
    from unittest.mock import MagicMock, patch

    mock_quote = MagicMock()
    mock_quote.is_valid = True
    mock_quote.name = "test"
    mock_quote.pe_ttm = 10.0
    mock_quote.pb = 1.0
    mock_quote.dividend_yield = 0.60  # 模拟腾讯失真
    mock_quote.market_cap = 100.0
    mock_quote.price = 10.0

    mock_fundamentals = MagicMock()
    mock_fundamentals.dividend_yield = 0.0
    mock_fundamentals.revenue_yoy = 0
    mock_fundamentals.profit_yoy = 0
    mock_fundamentals.roe_ttm = 0
    mock_fundamentals.eps_ttm = 1
    mock_fundamentals.ocf_to_profit = 0
    mock_fundamentals.industry = ""
    mock_fundamentals.sector = ""

    with patch("core.data_gateway.get_gateway") as mock_gw:
        mock_gw.return_value.quote.return_value = mock_quote
        mock_gw.return_value.fundamentals.return_value = mock_fundamentals

        from backend.services.fundamentals import fetch_fundamentals
        result = fetch_fundamentals("test.SH")

    assert result["dividend_yield"] == 0.0
    assert result["dividend_yield_unavailable"] is True
    assert "dividend_yield_tencent_raw" in result  # 调试可观测
```

### Step 5.2: 跑端到端测试

```bash
cd /home/sinter/workspace/a-quantitative-trading
pytest tests/integration/test_dividend_yield_e2e.py -v -s
```

**预期:** 全部 passed

### Step 5.3: 跑全部相关测试防回归

```bash
pytest tests/backend/ tests/test_data_gateway/ tests/integration/ -v --tb=short
```

**预期:** 全部 passed；若失败则隔离修复

### Step 5.4: 提交

```bash
git add tests/integration/test_dividend_yield_e2e.py
git commit -m "test(e2e): 股息率链路端到端回归测试

验证 4 项修复对 600809.SH 的影响:
- 后端不再回退到腾讯失真值
- dividend_yield_unavailable 显式标记
- dividend_yield_tencent_raw 可观测调试字段"
```

---

## 任务依赖图

```
Task 1 (Layer 4 合并优先级)        ← 独立,可最先做
    ↓
Task 2 (Layer 1 akshare 补字段)    ← 独立,但建议 Task 1 后做(因 Layer 1 是 Layer 4 的数据源)
    ↓
Task 3 (Layer 3 akshare 分红兜底)  ← 独立,任何时候可做
    ↓
Task 4 (Layer 2 降权威)            ← 独立,任何时候可做
    ↓
Task 5 (E2E 回归测试)              ← 必须最后做
```

---

## 验证矩阵

| 测试 | Task 1 | Task 2 | Task 3 | Task 4 | Task 5 |
|---|---|---|---|---|---|
| 后端合并优先级反转 | ✅ | | | | ✅ |
| akshare 补 dividend_yield 字段 | | ✅ | | | ✅ |
| akshare fetch_dividend 兜底 | | | ✅ | | ✅ |
| 腾讯权威降到 0.5 | | | | ✅ | (合并验证) |
| E2E 不回退到失真值 | | | | | ✅ |

---

## 风险与回滚

| 风险 | 概率 | 缓解 |
|---|---|---|
| akshare `stock_zh_a_spot_em` 列名版本差异 | 中 | `get_dividend_yield_from_spot` 已用 `next((c for c in spot.columns if "股息率" in c))` 模糊匹配 |
| baostock 不可用（其他 conda env 或部署机）| 低 | Task 3 加 logger.warning 显式提示；baostock 是常规依赖，安装简单 |
| Task 2 改变 akshare Provider 接口契约 | 低 | 跑现有 `test_provider_yfinance_akshare.py` 防回归 |
| 腾讯 dividend_yield 降权威影响 quote 端 | 极低 | 该字段在 quote 端单独使用，不参与 Fundamentals 合并 |

**回滚:** 每 Task 独立 commit，`git revert <commit-hash>` 即可单点回退。

---

## 完成后预期效果

**前置条件:** 在 `~/softwares/miniconda3/envs/quant-trading/` 环境下运行。

```python
from backend.services.fundamentals import fetch_fundamentals
result = fetch_fundamentals("600809.SH")
# 当前(在 quant-trading 环境):  dividend_yield = 4.75  (已正确，baostock 算的 TTM)
# 修复后(在任意环境):          dividend_yield = 4.75 / 5.12 (任一源算出即可)
#                               dividend_yield_unavailable = False
# 永远不再回退到腾讯失真的 0.60%
```

```python
from core.data_gateway import get_gateway
gw = get_gateway()
gw.dividend("600809.SH")
# quant-trading 环境:  4 records (baostock 正常)
# 修复后(任意环境):     0+ records + logger.warning(若不可用)，便于定位
```

---

## 后续可优化项(本计划不包含)

- Task 3 已包含缓存预热: `gw.fundamentals()` 触发兜底时预热 `gw.dividend()` 缓存
- Task 4 之后: 在 analyze_stock 层把 `dividend_yield_unavailable=True` 标红
- 数据源: 接入 Wind/iFinD 等专业数据源（需要付费，量力而行）
- 加监控: `dividend_yield` 跨源差异 > 2% 时发告警

---

**计划完成，待 Sir 审核后用 subagent-driven-development 逐 Task 实施。**
