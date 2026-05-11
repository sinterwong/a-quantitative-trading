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

## 最终目标

基本面因子（PEPercentile/ROEMomentum/ShareholderConcentration）不再全零；PMI/M2 宏观时序可查询。

---

## 任务分解

### Task 1 — 修正 AkshareProvider 宏观数据函数名（P1）

**文件**: `core/data_gateway/providers/akshare.py`

**改动**:

1. `_fetch_pmi()` 内调用 `ak.macro_china_pmi()`（原 `macro_china_pmi_monthly`）
2. `_fetch_m2()` 内调用 `ak.macro_china_money_supply()`（原 `macro_china_money_supply_bal`）
3. 两个函数的 `_normalize()` 列名匹配逻辑同步更新：
   - PMI：列名 `'月份'` + `'制造业-指数'`
   - M2：列名 `'月份'` + `'货币和准货币(M2)-同比增长'`

**验证**: Task 完成后运行：
```bash
cd /home/sinter/workspace/a-quantitative-trading
~/softwares/miniconda3/envs/quant-trading/bin/python -c "
from core.data_gateway import get_gateway
gw = get_gateway()
pmi = gw.macro('PMI')
m2 = gw.macro('M2')
print(f'PMI: {pmi.tail(3).to_string() if not pmi.empty else \"EMPTY\"}')
print(f'M2: {m2.tail(3).to_string() if not m2.empty else \"EMPTY\"}')
"
```
预期：两者均非空，最后3行有数据。

---

### Task 2 — PE/PB 从腾讯实时行情实时计算（P0）

**文件**: `core/fundamental_data.py`

**改动**: `FundamentalDataManager.get_fundamentals()` 在缓存命中后，合并腾讯实时行情中的 `pe_ttm` 和 `pb`：

1. `_fetch()` 末尾：若 `stock_financial_abstract` 返回有效 EPS 数据，则追加 `pe_ttm=None`/`pb=None` 占位列（留待实时补充）
2. 新增 `get_fundamentals_with_realtime()` 方法或修改 `get_fundamentals()` 签名：
   - 接收可选参数 `realtime_quote: Optional[Quote]`
   - 若传入且 `realtime_quote.pe_ttm > 0`，用实时 PE 覆盖 `pe_ttm` 列最新值
   - 若传入且 `realtime_quote.pb > 0`，用实时 PB 覆盖 `pb` 列最新值
3. `single_stock_analysis.py` 中调用处同步传入 `dl.get_realtime(sym)` 结果

**原理**: `pe_ttm = price / eps_ttm`，腾讯已有实时 price 和 eps_ttm。PB 腾讯直接给。

**验证**: Task 完成后运行：
```bash
cd /home/sinter/workspace/a-quantitative-trading
curl -s -X POST http://127.0.0.1:5555/analysis/stock/a \
  -H "Content-Type: application/json" \
  -d '{"symbol":"603611.SH"}' | python -c "
import sys,json
d=json.load(sys.stdin)
f=d.get('fundamentals',{})
w=d.get('warnings',[])
print(f\"pe_ttm={f.get('pe_ttm')} pb={f.get('pb')} roe={f.get('roe_ttm')}\")
print(f\"warnings={w}\")
print(f\"factor_pipeline factors_ok={d.get('factor_pipeline',{}).get('factors_ok')}\")
"
```
预期：`pe_ttm` 和 `pb` 有实际值（非 null/0），`fundamentals_unavailable` warning 消失。

---

### Task 3 — 北向资金兜底逻辑加固（P2）

**文件**: `core/data_gateway/providers/eastmoney.py`

**改动**: `_fetch_kamt_realtime()` 返回 `net=0` 时增加判别逻辑：

1. 当 `net_north_yi == 0` 时，额外检查 `hk2sh.get("amount",0)` 是否也为 0
2. 若两者同时为 0，视作数据异常，不使用实时数据，直接返回 `None` 让 `_fetch_kamt_daily()` 接管
3. 日总结接口 `_fetch_kamt_daily()` 失败时，才抛 ProviderError

**验证**: Task 完成后运行：
```bash
cd /home/sinter/workspace/a-quantitative-trading
~/softwares/miniconda3/envs/quant-trading/bin/python -c "
from core.data_gateway import get_gateway
gw = get_gateway()
nf = gw.north_flow()
print(f'net={nf.net_north_yi} direction={nf.direction} stale={nf.stale}')
"
```
预期：`net != 0`（交易日有正常数值）或 `stale=True` + 从备用日数据获取。

---

### Task 4 — 集成测试 + 回归验证（贯穿）

**验证命令**（每次 Task 完成 后执行）：

```bash
# 1. 宏观数据
~/softwares/miniconda3/envs/quant-trading/bin/python -c "
from core.data_gateway import get_gateway
gw = get_gateway()
print('PMI:', 'OK' if not gw.macro('PMI').empty else 'EMPTY')
print('M2:', 'OK' if not gw.macro('M2').empty else 'EMPTY')
"

# 2. 基本面接口（603611）
curl -s -X POST http://127.0.0.1:5555/analysis/stock/a \
  -H "Content-Type: application/json" \
  -d '{"symbol":"603611.SH"}' | python -c "
import sys,json; d=json.load(sys.stdin)
f=d.get('fundamentals',{}); w=d.get('warnings',[])
print(f\"pe_ttm={f.get('pe_ttm')} pb={f.get('pb')} roe={f.get('roe_ttm')}\")
print(f\"warnings={w}\")
"

# 3. 北向资金
~/softwares/miniconda3/envs/quant-trading/bin/python -c "
from core.data_gateway import get_gateway
gw = get_gateway()
nf = gw.north_flow()
print(f'north_flow: net={nf.net_north_yi} dir={nf.direction}')
"

# 4. 全量测试
cd /home/sinter/workspace/a-quantitative-trading
~/softwares/miniconda3/envs/quant-trading/bin/python -m pytest tests/test_data_gateway/ -q
```

**通过标准**：
- Task 1：`PMI` 和 `M2` 非空
- Task 2：603611 基本面警告消失，`pe_ttm`/`pb` 有正数
- Task 3：`north_flow` 有有效数值（非 `stale=True` 且 `net!=0`，或走日总结备用）
- Task 4：data_gateway 测试 210/210 通过

---

## 暂不处理（后续迭代）

- `stock_a_indicator_lg` 替代接口探索（东方财富财报 API reportName 试错）
- 行业对比 sector 数据（Eastmoney 封禁时无完美替代，保持现状）
- 港股/美股基本面（需要独立数据源对接）

---

## 顺序

```
Task 1 → Task 3 → Task 2 → Task 4
```

Task 1 和 Task 3 为独立文件改动，可并行开发。Task 2 依赖 Task 4 的数据层基础建设改动最少但最关键。
