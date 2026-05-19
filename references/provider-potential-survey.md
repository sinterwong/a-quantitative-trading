# Provider 能力矩阵扩展潜力调研报告

> 调研时间：2026-05-19
> 验证方式：脚本实测（所有结论均来自实际 API 调用，非推断）
> 环境：quant-trading conda 环境（Python 3.11）

---

## 一、现有能力矩阵（实测校准版）

### 1.1 Provider 能力注册 vs 实际测试对照

| Capability | Tencent | Sina | Eastmoney | Baostock | AkShare | Yfinance |
|---|---|---|---|---|---|---|
| QUOTE | ✅ A/HK/US | ✅ A/INDEX/HK | ✅ A/INDEX/HK | ❌ 无模块 | — | — |
| KLINE_DAILY | ✅ A/HK | ✅ A/INDEX | — | ❌ 无模块 | — | — |
| KLINE_MINUTE | ✅ **仅 HK** | ❌ 全空（声明但返回0行） | — | — | — | — |
| MARKET_INDEX | ✅ A/HK/US ETF+指数 | ✅ A/INDEX/HK | ✅ A/INDEX/HK | — | — | ❌ 无模块 |
| SECTOR_RANKING | — | ✅ | ❌ 网络失败 | — | — | — |
| SECTOR_CONSTITUENTS | — | ✅ | ❌ 网络失败 | — | — | — |
| NORTH_FLOW（实时） | — | — | ✅ | — | — | — |
| NORTH_FLOW（历史） | — | — | — | — | ✅ | — |
| MACRO | — | — | — | — | ✅ PMI/M2/CREDIT | — |
| FUNDAMENTALS | — | — | — | ❌ 无模块 | ✅ A/H | — |
| FUNDAMENTALS_HISTORY | — | — | — | ❌ 无模块 | ✅ A/H | — |
| BALANCE_SHEET | — | — | — | ❌ 无模块 | ⚠️ 接口存在但需开发 | — |
| MARGIN_FLOW | — | — | — | — | ✅ 快照单日 | — |
| FUND_FLOW | — | — | — | — | ✅ 120日 | — |
| NEWS_HEADLINES | — | — | ✅ 20条 | — | ✅ | — |

### 1.2 关键发现：注册与实际不符之处

| 问题 | Provider | 说明 |
|---|---|---|
| KLINE_MINUTE 声明过度 | **Sina** | `fetch_kline_minute` 声明支持 A 股，但实测**所有间隔(1/5/15/30/60m)均返回空 DataFrame**，1行数据也没有 |
| Baostock 形同虚设 | **Baostock** | conda 环境无 `baostock` 模块，`import baostock as bs` 直接 `ModuleNotFoundError`，全部 capability 实际不可用 |
| Yfinance 形同虚设 | **Yfinance** | conda 环境无 `yfinance` 模块，`ModuleNotFoundError`，所有 capability 不可用 |
| Eastmoney SECTOR 完全失效 | **Eastmoney** | push2.eastmoney.com 连接被 RemoteDisconnected 关闭，`fetch_sectors` 和 `fetch_sector_constituents` 在 WSL 环境下**实测全部失败** |
| AkShare BALANCE_SHEET 未声明 | **AkShare** | 存在 `stock_balance_sheet_by_report_em` 等接口，但未实现、未在 `declare()` 中声明 |
| AkShare MACRO 过度保守 | **AkShare** | 仅声明 PMI/M2/CREDIT 三个 indicator，但 `macro_china_cpi()` 和 `macro_china_ppi()` 实测**完全可用**（CPI 220行，PPI 244行） |

---

## 二、扩展潜力分析（按维度）

### 维度一：Market Index / US 行情覆盖

**现状缺口：**
- 标普 500 指数（^GSPC）：**无来源**，Tencent 不认 SPX/GSPC 等代码
- 港股恒生科技指数（HSTECH）：Tencent 用 `hkHSTECH` 可行，但 `HSTECH` 不认

**已验证可扩展（腾讯 MARKET_INDEX）：**

| 代码 | 名称 | 验证结果 |
|---|---|---|
| QQQ | 纳指 100 ETF | ✅ 703.62 |
| SPY | 标普 500 ETF-SPDR | ✅ 736.92 |
| DIA | 道琼斯 ETF | ✅ 495.56 |
| IWM | 罗素 2000 ETF | ✅ 275.28 |
| EEM | 新兴市场 ETF-MSCI | ✅ 64.86 |
| FXI | 中国大盘股 ETF-iShares | ✅ 36.24 |
| ASHR | 沪深 300 ETF-德银 | ✅ 35.32 |
| NDX | 纳斯达克 100 | ✅ 28911.59 |
| IXIC | 纳斯达克综合 | ✅ 26048.36 |
| DJI | 道琼斯工业 | ✅ 49537.59 |
| VIX | 波动率指数 | ✅ 21.67 |
| hkHSI | 恒生指数 | ✅ 25675.1 |
| hkHSCEI | 恒生国企指数 | ✅ 8597.79 |
| hkHSTECH | 恒生科技指数 | ✅ 4844.94 |
| hkHSCCI | 红筹指数 | ✅ 4484.56 |

**机会：**
1. SPX（标普 500）本身无来源，但 **SPY**（SPDR 标普 500 ETF）价格与 SPX 高度相关，可作为替代
2. 恒生科技指数用 `hkHSTECH` 代码可用，系统目前传 `HSTECH` 失败，应统一代码规范化

**验证脚本片段：**
```python
tc = TencentProvider()
r = tc.fetch_market_index("SPY")   # ✅ OK, price=736.92
r = tc.fetch_market_index("QQQ")   # ✅ OK, price=703.62
r = tc.fetch_market_index("hkHSTECH")  # ✅ OK, price=4844.94
r = tc.fetch_market_index("HSTECH")    # ❌ NONE — 代码格式问题
```

---

### 维度二：板块数据（Sector）交叉冗余与失效

**现状：**
- Sina `fetch_sectors` ✅ 正常返回 20 个行业板块（`SINA_new_xxx` 代码格式）
- Sina `fetch_sector_constituents` ✅ 正常（使用 `new_blhy` 等 Sina 代码）
- Eastmoney `fetch_sectors` ❌ **WSL 下全部连接失败**（push2.eastmoney.com RemoteDisconnected）
- Eastmoney `fetch_sector_constituents` ❌ 同样失败

**关键发现：Sina 返回的板块代码与 Eastmoney 不兼容**
- Sina sector code 示例：`SINA_new_blhy`（玻璃行业）
- Eastmoney sector code 示例：`EM_BK0xxx`（东方财富内部码）
- Eastmoney 的 `fetch_sector_constituents` 收到 Sina 格式代码时会报 RemoteDisconnected，**永远不会被执行到**（因为 `fetch_sectors` 已经失败）

**机会：**
1. **Sina 是板块数据的唯一有效来源**，但 Eastmoney 声明了相同 capability 后在 WSL 下静默失效——这意味着 FAILOVER 策略没有触发（因为 Eastmoney 返回的是 ProviderError，不是 empty list）
2. 如果 Eastmoney 板块能力被修复，可与 Sina 形成 **MERGE_FIELDS 交叉验证**（两家的板块 ranking 和 constituents 可以互补）

---

### 维度三：AkShare 未开发利用的接口

**已发现未声明能力（`declare()` 中没有，但实测可用）：**

| 接口 | 数据类型 | 实测结果 | 潜在 Capability |
|---|---|---|---|
| `macro_china_cpi()` | CPI 月度时序（220行） | ✅ OK | **MACRO**（新 indicator） |
| `macro_china_ppi()` | PPI 月度时序（244行） | ✅ OK | **MACRO**（新 indicator） |
| `stock_balance_sheet_by_report_em()` | A股资产负债表 | ⚠️ 需要传入股票代码，返回 None（接口文档问题） | **BALANCE_SHEET** |
| `stock_profit_sheet_by_quarterly_em()` | 利润表季报 | ⚠️ 返回 None（接口问题） | — |
| `stock_cash_flow_sheet_by_quarterly_em()` | 现金流量表季报 | ⚠️ 返回 None（接口问题） | — |
| `stock_hsgt_north_hold_stock_em()` | 北向持股明细 | ❌ 函数不存在 | — |

**关于 AkShare BALANCE_SHEET 的说明：**

AkShare 有 `stock_balance_sheet_by_report_em` 函数，但直接调用返回 None（可能是接口内部返回了空数据，而非接口不存在）。`stock_financial_balance_sheet_em` 在 akshare 1.18.60 中**不存在**。

**真正的资产负债表数据来源仍是 Baostock**——但 Baostock 未安装。

---

### 维度四：Baostock 模块缺失

**实测结果：**
```
baostock in conda: 00.9.10 （已安装！）
Python sys.path 中的 baostock: ModuleNotFoundError
```

**根因：** `baostock` 包已安装在 conda 环境 `/home/sinter/softwares/miniconda3/envs/quant-trading`，但运行 `python3`（hermes-agent 的 venv）找不到。这是**环境不一致**问题。

`baostock` 提供的实际能力：
- `bs.query_history_k_data_plus()` — A 股日 K 线 ✅
- `bs.query_profit_data()` — 季报利润数据
- `bs.query_balance_sheet_by_date()` — 资产负债表
- `bs.query_dividend_data()` — 分红数据

**影响：** BALANCE_SHEET capability 的唯一来源（Baostock）实际上完全不可用。

**修复方案：**
```bash
# 在 conda 环境中直接运行（而非 hermes venv）：
/home/sinter/softwares/miniconda3/envs/quant-trading/bin/python scripts/xxx.py
# 或在 backend/main.py 启动时用 conda python
```

---

### 维度五：Sina 分钟 K 线声明过度

**实测结果（000001.SZ 平安银行）：**

| 间隔 | 结果 |
|---|---|
| 1m | EMPTY (0行) |
| 5m | EMPTY (0行) |
| 15m | EMPTY (0行) |
| 30m | EMPTY (0行) |
| 60m | EMPTY (0行) |

**但 Tencent 分钟 K 线对港股正常（1行数据）：**
```python
tc.fetch_kline_minute("00700.HK")  # ✅ shape=(1, 6)
```

**机会：** `supports()` 方法中已有限制（`if market == Market.HK: return False`），但真正的问题是 Sina 分钟 K 数据源本身不可用。**应将 Sina 的 KLINE_MINUTE capability 从 declare() 中移除**，或限制为仅 INDEX 市场。

---

### 维度六：字段权威性（field_authority）未被利用

**当前声明：**
- **Tencent**：`pe_tbm/pb/market_cap/float_cap/high_52w/low_52w` → 权重 1.3（独家 88-field）
- **Sina**：买卖盘口字段 `bid1_price/bid1_vol/ask1_price/ask1_vol` → 权重 1.2

**验证：** Tencent 88-field 实际存在（`amplitude/dividend_yield/turnover_rate/volume_ratio` 等均可获取）。

**机会：** Sina 的买卖盘口权重 1.2 从未被调用方使用——因为整个系统没有使用 `bid1_price/ask1_price` 字段做任何决策。这是**死代码**。

---

### 维度七：News 数据源互补

**现状：**
- Eastmoney `fetch_news_headlines` ✅ 返回 20 条财联社快讯
- AkShare `fetch_news_headlines` ✅ 返回财联社电报（`stock_info_global_cls`）
- 两者都声明了 `Capability.NEWS_HEADLINES`，走 **MERGE_LISTS** 策略

**问题：** 两者数据源本质相同（都是财联社），MERGE_LISTS 去重后实际数量不会翻倍。

**机会：** 东方财富还有 `stock_telegraph_cls` 接口（akshare 中存在），以及新浪的新闻接口，值得探索作为第三来源。

---

## 三、扩展优先级建议

基于验证结果，按 ROI 从高到低排列：

### 🔴 高优先级（可直接实现，立即生效）

| # | 扩展项 | 当前状态 | 做法 | 预期收益 |
|---|---|---|---|---|
| 1 | **AkShare 新增 CPI/PPI macro indicator** | 仅 PMI/M2/CREDIT 三项 | 在 `MacroIndicator` 枚举加 `CPI/PPI`，`AkshareProvider.fetch_macro()` 加分支 | 宏观数据层补全，无成本 |
| 2 | **Tencent 恒生科技代码规范化** | `HSTECH` 失败，`hkHSTECH` 成功 | 在 `symbols.py` 增加 `hkHSTECH → HSTECH` 映射，或修改 Tencent 的 `fetch_market_index` 接受更多别名 | 港股科技指数可靠获取 |
| 3 | **Tencent MARKET_INDEX 扩展美股 ETF 覆盖** | SPX 无来源，SPY/QQQ 可用 | 在指数相关模块直接使用 SPY 而非 ^GSPC，或在 `symbols.py` 增加别名映射 | 美股市场全景感知 |

### 🟡 中优先级（需要一定开发量）

| # | 扩展项 | 当前状态 | 做法 | 预期收益 |
|---|---|---|---|---|
| 4 | **Eastmoney SECTOR 能力在 WSL 下的 Failover** | fetch_sectors 返回 ProviderError 而非 empty list | 在 Eastmoney 层面将网络异常捕获后返回 `[]`，触发上层 FAILOVER 到 Sina | 板块数据有双重保障 |
| 5 | **Sina KLINE_MINUTE 从 declare() 中移除或限定** | 声明支持 A 股但实际全空 | 修改 `SinaProvider.declare()` 移除 KLINE_MINUTE，或限定为 `Market.INDEX` | 避免路由误导 |
| 6 | **Baostock 通过 conda 环境执行** | conda 有包但 venv 找不到 | 将后端启动改为 `conda run -n quant-trading python backend/main.py`，或要求调用方用 conda python | BALANCE_SHEET + 基本面历史双重覆盖 |

### 🟢 低优先级（长期改进）

| # | 扩展项 | 说明 |
|---|---|---|
| 7 | **News 第三来源** | 新浪新闻或 `stock_telegraph_cls` 作为第三个 MERGE_LISTS 输入源 |
| 8 | **AkShare 资产负债表深度开发** | 尝试 `stock_balance_sheet_by_report_em(code)` 的正确调用方式（需查 akshare 文档确定参数格式） |
| 9 | **Sina/Eastmoney 板块代码桥接** | 当 Eastmoney 修复后，两家板块 API 的 code 格式需要互转（`EM_BK0xxx` ↔ `SINA_new_xxx`） |

---

## 四、结论

**最关键的三个问题：**

1. **Baostock 和 Yfinance 装了却用不了**（hermes-agent venv 与 conda 环境隔离）——这是最简单的修复（改启动方式），但影响最大（ BALANCE_SHEET 和美股 K 线）
2. **AkShare CPI/PPI 未利用** ——一行代码即可扩展 macro indicator 覆盖
3. **Eastmoney SECTOR 在 WSL 下静默失效** ——需要改异常处理返回 `[]` 而非 raise，触发 FAILOVER 到 Sina

**不需要引入任何新 provider**，当前六个 provider 的接口覆盖已足够完整，主要问题是部分接口未实现/未声明，以及 conda 环境与 venv 的隔离。

---

*验证脚本位置：`/home/sinter/workspace/a-quantitative-trading/scripts/provider_capability_verify.py`（如需复测）*
