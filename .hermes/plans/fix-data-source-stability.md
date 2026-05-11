# fix/data-source-stability 分支开发计划

## 背景

akshare 1.18.60 实测结果（2026-05-11）：

| 数据类型 | 当前来源 | 状态 |
|---------|---------|------|
| PE/PB | `stock_a_indicator_lg` | ❌ 函数不存在（已从 akshare 移除） |
| PE/PB | `stock_financial_abstract` | ⚠️ 有 EPS/ROE，无 PE/PB 列 |
| ROE/EPS | `stock_financial_abstract` | ✅ 正常 |
| 营收增速 | `stock_financial_abstract` | ✅ 正常 |
| PMI | `macro_china_pmi_monthly` | ❌ 函数名已改为 `macro_china_pmi` |
| M2 | `macro_china_money_supply_bal` | ❌ 函数名已改为 `macro_china_money_supply` |
| 社融 | `macro_china_shrzgm` | ✅ 正常 |
| 北向资金 | Eastmoney KAMT | ⚠️ 实时接口偶发 net=0 |
| 板块数据 | Eastmoney push2 | ⚠️ 封禁高发，健康度自动降权 |

---

## 核心设计原则（来自上一轮数据层重构）

> "整个系统对外网数据的**唯一出口**。所有 provider 平级，通过 capability 矩阵 + 健康度评分动态路由，可合并数据（Quote/Fundamentals）做字段级互补合并。"

所有数据访问必须通过 `get_gateway()` 进出，业务层不直接调用 provider，不自己拼装"历史序列 + 实时快照"。

---

## 最终目标

- 宏观时序（PMI/M2）：`gateway.macro()` 返回非空数据
- 基本面快照：`gateway.fundamentals()` 返回含 PE/PB/ROE 的完整 `Fundamentals` dataclass
- 业务层分析接口：只调用 `get_gateway()`，不直接碰 `FundamentalDataManager`

---

## 任务分解

### Task 1 — 修正 AkshareProvider 宏观数据函数名（P1）

**文件**: `core/data_gateway/providers/akshare.py`

**现状**: `_fetch_pmi()` 调用 `ak.macro_china_pmi_monthly()`（不存在），`_fetch_m2()` 调用 `ak.macro_china_money_supply_bal()`（不存在）

**改动**:

1. `_fetch_pmi()` 内改为调用 `ak.macro_china_pmi()`
2. `_fetch_m2()` 内改为调用 `ak.macro_china_money_supply()`
3. 两个函数的 `_normalize()` 列名匹配同步更新：
   - PMI：日期列 `'月份'`，数值列 `'制造业-指数'`
   - M2：日期列 `'月份'`，数值列 `'货币和准货币(M2)-同比增长'`

**验证**:
```bash
~/softwares/miniconda3/envs/quant-trading/bin/python -c "
from core.data_gateway import get_gateway
gw = get_gateway()
pmi = gw.macro('PMI')
m2 = gw.macro('M2')
print('PMI:', 'OK' if not pmi.empty else 'EMPTY', pmi.tail(2).to_dict() if not pmi.empty else '')
print('M2:', 'OK' if not m2.empty else 'EMPTY', m2.tail(2).to_dict() if not m2.empty else '')
"
```

---

### Task 2 — 扩展 AkshareProvider 支持 FUNDAMENTALS 能力（P0）

**文件**: `core/data_gateway/providers/akshare.py`

**现状**: `AkshareProvider` 只声明了 `MACRO` 能力，`fetch_fundamentals()` 继承自 base 返回 None。gateway 的 `fundamentals()` 永远拿不到数据。

**改动**:

1. `declare()` 中加入 `Capability.FUNDAMENTALS` 到 `capabilities` 集合
2. 实现 `fetch_fundamentals(symbol: str) -> Optional[Fundamentals]`：
   - 调用 `ak.stock_financial_abstract(symbol=code)`
   - 从"常用指标"区块取 `基本每股收益` → `eps_ttm`
   - 从"盈利能力"区块取 `净资产收益率(ROE)` → `roe_ttm`
   - 从"常用指标"区块取 `归母净利润` → `profit_ttm`
   - 取最新季报期作为 `timestamp`
   - `pe_ttm` 和 `pb` 暂时留 0（由 gateway 层补充）
3. 在 `capabilities.py` 中确认 `Capability.FUNDAMENTALS` 已存在（已存在）

**验证**:
```bash
~/softwares/miniconda3/envs/quant-trading/bin/python -c "
from core.data_gateway import get_gateway
gw = get_gateway()
f = gw.fundamentals('603611.SH')
print(f'fundamentals: symbol={f.symbol if f else None}')
if f:
    print(f'  eps_ttm={f.eps_ttm}, roe_ttm={f.roe_ttm}, pe_ttm={f.pe_ttm}, pb={f.pb}')
"
```

---

### Task 3 — gateway.fundamentals() 用 quote 数据补充 PE/PB（P0）

**文件**: `core/data_gateway/gateway.py`

**现状**: `fundamentals()` 只做 provider 合并，返回的 `Fundamentals` 里 `pe_ttm=0, pb=0`（因为 AkShare 接口无此字段）。

**改动**: 在 `fundamentals()` 方法中，合并结果返回前做实时补充：

```python
def fundamentals(self, symbol: str) -> Optional[Fundamentals]:
    # ... 现有缓存检查和 _merged_fetch ...

    # 新增：pe_ttm/pb 从实时行情补充（腾讯独家权威字段）
    if merged is not None:
        if merged.pe_ttm <= 0 or merged.pb <= 0:
            quote = self.quote(symbol)
            if quote is not None and quote.pe_ttm > 0:
                merged.pe_ttm = quote.pe_ttm
            if quote is not None and quote.pb > 0:
                merged.pb = quote.pb

        self._cache.set(cache_key, merged, _DEFAULT_TTL[Capability.FUNDAMENTALS])
        self._last_provenance[cache_key] = prov
    return merged
```

**原理**: 腾讯 `quote.pe_ttm` 和 `quote.pb` 是 A 股最权威来源，且 `TencentProvider` 已声明 `field_authority > 1.0`。gateway 内部调用 `self.quote()` 不出栈，不破坏 single exit。

**验证**:
```bash
curl -s -X POST http://127.0.0.1:5555/analysis/stock/a \
  -H "Content-Type: application/json" \
  -d '{"symbol":"603611.SH"}' | python -c "
import sys,json
d=json.load(sys.stdin)
f=d.get('fundamentals',{}); w=d.get('warnings',[])
print(f'pe_ttm={f.get(\"pe_ttm\")}, pb={f.get(\"pb\")}, roe={f.get(\"roe_ttm\")}')
print(f'warnings={w}')
"
```

预期：`pe_ttm > 0`，`pb > 0`，`fundamentals_unavailable` 警告消失。

---

### Task 4 — 业务层统一走 gateway（P0）

**文件**: `backend/services/single_stock_analysis.py`

**现状**: `analyze_a_share()` 自己调 `FundamentalDataManager.get_fundamentals(sym)`，绕开了 gateway。

**改动**: 替换为通过 gateway 获取基本面快照：

```python
from core.data_gateway import get_gateway
gw = get_gateway()
fund = gw.fundamentals(sym)
if fund and (fund.eps_ttm > 0 or fund.roe_ttm > 0):
    report.fundamentals = {
        'pe_ttm': fund.pe_ttm,
        'pb': fund.pb,
        'roe_ttm': fund.roe_ttm,
        'eps_ttm': fund.eps_ttm,
        'revenue_yoy': fund.revenue_yoy,
        'profit_yoy': fund.profit_yoy,
        'as_of_date': fund.timestamp.date() if fund.timestamp else None,
    }
else:
    report.warnings.append('fundamentals_unavailable')
```

**注**: `FundamentalDataManager` 保留用于因子层的**历史时间序列**（季度 → 日频前向填充）。业务快照层统一走 gateway。

**验证**: 同 Task 3。

---

### Task 5 — 北向资金兜底逻辑加固（P2）

**文件**: `core/data_gateway/providers/eastmoney.py`

**现状**: `_fetch_kamt_realtime()` 偶发返回 `net=0`，业务无法区分"真的零成交"和"接口异常"。

**改动**: 在 `_fetch_kamt_realtime()` 中，当 `net_north_yi == 0` 时，检查 `hk2sh.get("amount", 0)` 是否同时为 0，两者皆零视为异常数据，返回 `None` 让 `_fetch_kamt_daily()` 日总结接口接管。

```python
# _fetch_kamt_realtime() 末尾
if net_north_yi == 0 and (n2s.get("amount", 0) == 0):
    return None  # 异常，交给日总结备用
```

**验证**:
```bash
~/softwares/miniconda3/envs/quant-trading/bin/python -c "
from core.data_gateway import get_gateway
gw = get_gateway()
nf = gw.north_flow()
print(f'net={nf.net_north_yi} direction={nf.direction} stale={nf.stale}')
"
```

---

### Task 6 — 集成回归验证（贯穿全程）

**每次 Task 完成后运行**:

```bash
# 1. 宏观 PMI/M2
~/softwares/miniconda3/envs/quant-trading/bin/python -c "
from core.data_gateway import get_gateway
gw = get_gateway()
print('PMI:', 'OK' if not gw.macro('PMI').empty else 'EMPTY')
print('M2:', 'OK' if not gw.macro('M2').empty else 'EMPTY')
"

# 2. 基本面快照（603611）
curl -s -X POST http://127.0.0.1:5555/analysis/stock/a \
  -H "Content-Type: application/json" \
  -d '{"symbol":"603611.SH"}' | python -c "
import sys,json
d=json.load(sys.stdin)
f=d.get('fundamentals',{}); w=d.get('warnings',[])
print(f'pe_ttm={f.get(\"pe_ttm\")}, pb={f.get(\"pb\")}, roe={f.get(\"roe_ttm\")}')
print(f'warnings={w}')
"

# 3. 北向资金
~/softwares/miniconda3/envs/quant-trading/bin/python -c "
from core.data_gateway import get_gateway
gw = get_gateway()
nf = gw.north_flow()
print(f'north_flow: net={nf.net_north_yi} dir={nf.direction}')
"

# 4. 全量 data_gateway 测试
cd /home/sinter/workspace/a-quantitative-trading
~/softwares/miniconda3/envs/quant-trading/bin/python -m pytest tests/test_data_gateway/ -q
```

**通过标准**:
- Task 1：PMI 非空，M2 非空
- Task 2：`gateway.fundamentals('603611.SH')` 返回非 None，`eps_ttm > 0`
- Task 3：603611 接口返回 `pe_ttm > 0`，`pb > 0`
- Task 4：603611 接口无 `fundamentals_unavailable` 警告
- Task 5：北向资金 `net != 0` 或 `stale=True` + 日总结备用
- Task 6：data_gateway 测试全部通过

---

## 暂不处理（后续迭代）

- `stock_a_indicator_lg` 替代接口探索（东方财富财报 API reportName 试错）
- 因子层历史财务时间序列的 gateway 化（`FundamentalDataManager` 暂时保留）
- 行业对比 sector 数据替代
- 港股/美股基本面

---

## 顺序

```
Task 1（独立）→ Task 2（Task 2 的前提）→ Task 3（Task 4 的前提）→ Task 4 → Task 5（独立）→ Task 6
```

Task 1 和 Task 5 互不依赖，可并行。
