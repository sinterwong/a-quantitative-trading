# Baostock 全量能力启用 — 开发任务清单

> 分支：`feature/baostock-full-capacity`
> 目标：将 Baostock 从"K线 + 基本面快照"扩展为覆盖 A股数据需求 90% 的主数据源
> 依据：Baostock API 文档 (api.baostock.com/mainContent?file=pythonAPI.md)

---

## 背景：为什么是 Baostock

| 特性 | Baostock | AkShare（对比） |
|------|-----------|----------------|
| 认证 | **无需 Token**，直接 login() | 需要 token，RemoteDisconnected 频发 |
| 估值日频序列 | **peTTM/pbMRQ/psTTM/pcfNcfTTM** 全有 | 无（PE/PB 历史序列是系统最大缺口） |
| 财报频率 | 2007年至今，季频 6 大表完整 | 部分字段缺失 |
| 稳定性 | 高（会话锁保护） | 频繁断连 |
| 覆盖范围 | A股 + 指数 | A股 + 港股 + 美股 + … |

**结论：Baostock 是 A 股的"隐形金矿"，系统当前对它的使用不足 30%。**

---

## 任务总览

| 优先级 | 任务数 | 预计改动范围 |
|--------|--------|-------------|
| P0 | 4 | 改 1~3 行代码即可完成 |
| P1 | 4 | 新增 Provider 方法 + Schema 字段 |
| P2 | 3 | 新增 Capability + 独立路由方法 |
| P3 | 2 | 新功能探索 |

---

## P0 — 零架构改动，立刻可上线（改完即验证）

### T0-1：K线 fields 加入估值指标（`peTTM/pbMRQ/psTTM/pcfNcfTTM`）

**当前问题**：系统 K 线数据不包含估值字段，`Quote` schema 虽然有 `pe_ttm/pb` 字段，但 Baostock 没有填充。

**改动位置**：`core/data_gateway/providers/baostock.py` — `fetch_kline_daily()` 方法，第 220 行 fields 字符串

```python
# 当前
"date,open,high,low,close,volume,amount"
# 改为
"date,open,high,low,close,volume,amount,peTTM,pbMRQ,psTTM,pcfNcfTTM"
```

**验证**：调用 `gw.fetch_kline_daily("600809.SH")`，检查新增 4 列 non-null 率 > 90%。

**风险**：低。字段是增量添加，向后兼容。

---

### T0-2：基本快照填充 `dividend_yield`（从 `query_dividend_data` 计算）

**当前问题**：`Fundamentals.dividend_yield` 始终为 0.0，Baostock 有完整除权除息数据可计算。

**改动位置**：`core/data_gateway/providers/baostock.py` — `fetch_fundamentals()` 方法

**计算逻辑**：
```
dividend_yield = 最近一年每股税前股利 / 当前股价 × 100
```

Baostock `query_dividend_data` 返回 `dividCashPsBeforeTax`（每股股利税前）和 `dividOperateDate`（除权除息日），取最近 12 个月累计。

**验证**：600809.SH（山西汾酒）dividend_yield 应在 2%~5% 范围（非零）。

---

### T0-3：`fetch_fundamentals` 补全 `npMargin` / `gpMargin`（净利率/毛利率）

**当前问题**：`Fundamentals` 有 `roe_ttm/eps_ttm`，但缺少 `销售净利率` 和 `销售毛利率`，这是盈利能力核心指标。

**改动位置**：`core/data_gateway/providers/baostock.py` — `fetch_fundamentals()` 中的 `Fundamentals()` 构造

**新增字段**：添加到 `Fundamentals` dataclass：
```python
net_margin: float = 0.0   # 销售净利率 %（npMargin）
gross_margin: float = 0.0  # 销售毛利率 %（gpMargin）
```

**来源**：`query_profit_data` 的 `npMargin`（小数，转%）、`gpMargin`（小数，转%）。

**验证**：600809.SH 毛利率应在 70%+（白酒行业），净利率应在 25%+。

---

### T0-4：`FundamentalsHistory` 补全 `ps_ttm` / `pcf_ncf_ttm` 字段

**当前问题**：`FundamentalsHistory` 只有 5 列（AkShare 的 roe_ttm/eps_ttm/…） + 4 列（Baostock balance），缺少 Baostock K 线中有的 `psTTM` 和 `pcfNcfTTM`。

**改动位置**：
1. `core/data_gateway/schemas.py` — `FundamentalsHistory` dataclass 新增字段
2. `core/data_gateway/providers/baostock.py` — `fetch_fundamentals_history()` 新增 `ps_ttm` / `pcf_ncf_ttm` 来源（从 K 线 peTTM 反推或直接取字段）

**说明**：`psTTM` = 总市值/营收TTM，`pcfNcfTTM` = 总市值/经营现金流TTM，可从 `query_profit_data` 的 `MBRevenue` + K 线 `psTTM/pcfNcfTTM` 合并得到。

---

## P1 — Schema 增量扩展（改 Schema + Provider 方法）

### T1-1：启用 `query_dupont_data`（杜邦分析）→ 新增 `DupontMetrics` schema

**当前问题**：ROE 拆解完全缺失，无法做杜邦分析选股。

**改动**：

1. `schemas.py` 新增 `DupontMetrics` dataclass：
   ```python
   @dataclass
   class DupontMetrics:
       symbol: str = ""
       roe: float = 0.0           # dupontROE
       asset_turn: float = 0.0    # dupontAssetTurn（总资产周转率）
       net_margin: float = 0.0     # dupontNitogr（净利率）
       equity_multiplier: float = 0.0  # dupontAssetStoEquity（权益乘数）
       tax_burden: float = 0.0    # dupontTaxBurden（税负）
       int_burden: float = 0.0    # dupontIntburden（利息负担）
       ebit_to_revenue: float = 0.0  # dupontEbittogr
       timestamp: datetime = field(default_factory=datetime.now)
   ```

2. `baostock.py` 新增 `fetch_dupont_metrics(self, symbol) -> DupontMetrics` 方法，调用 `query_dupont_data`，取最新一期。

**验证**：600809.SH ROE 应与 `Fundamentals.roe_ttm` 一致（约 30%+），且三项乘积（净利率 × 资产周转率 × 权益乘数）≈ ROE。

---

### T1-2：启用 `query_operation_data`（运营能力）→ 新增 `OperationMetrics` schema

**当前问题**：存货周转、应收账款周转等运营能力指标完全缺失。

**改动**：

1. `schemas.py` 新增 `OperationMetrics` dataclass：
   ```python
   @dataclass
   class OperationMetrics:
       symbol: str = ""
       nr_turn: float = 0.0        # 应收账款周转率（次）
       nr_turn_days: float = 0.0  # 应收账款周转天数
       inv_turn: float = 0.0       # 存货周转率（次）
       inv_turn_days: float = 0.0  # 存货周转天数
       ca_turn: float = 0.0        # 流动资产周转率
       asset_turn: float = 0.0     # 总资产周转率
       timestamp: datetime = field(default_factory=datetime.now)
   ```

2. `baostock.py` 新增 `fetch_operation_metrics(self, symbol) -> OperationMetrics` 方法。

**验证**：白酒行业 `inv_turn` 应较低（存货周转慢），`nr_turn_days` 应较短。

---

### T1-3：启用 `query_growth_data` 全部字段 → 扩展 `Fundamentals` 和 `FundamentalsHistory`

**当前问题**：`YOYEquity`（净资产同比）、`YOYPNI`（母公司净利润同比）未使用。

**改动**：`Fundamentals` 新增字段：
```python
equity_yoy: float = 0.0    # 净资产同比 %（YOYEquity）
pni_yoy: float = 0.0       # 归属母公司净利润同比 %（YOYPNI）
```

同时在 `FundamentalsHistory` 中添加时间序列版本。

---

### T1-4：`FundamentalsHistory` 扩展：从季频 4 列 → 季频 12+ 列

**当前问题**：历史序列只有 9 列，Baostock 可以提供 20+ 字段。

**目标**：将以下字段加入 `FundamentalsHistory` 输出 DataFrame：

| 来源表 | 字段 | 字段名（英文） |
|--------|------|---------------|
| profit_data | 销售毛利率 | gross_margin |
| profit_data | 销售净利率 | net_margin |
| profit_data | 主营收入 | revenue |
| profit_data | 净利润 | net_profit |
| balance_data | 现金比率 | cash_ratio |
| balance_data | 负债同比 | liability_yoy |
| cashflow_data | CFO/营收 | cfo_to_revenue |
| cashflow_data | CFO/总营收 | cfo_to_gross_revenue |
| operation_data | 存货周转天数 | inv_turn_days |
| operation_data | 应收账款周转天数 | nr_turn_days |
| dupont_data | 权益乘数 | equity_multiplier |
| dupont_data | 税负 | tax_burden |

**Schema 改动**：`FundamentalsHistory` dataclass 新增上述字段。
**Provider 改动**：`baostock.py` 的 `fetch_fundamentals_history()` 批量拉取 6 张表并全部归一化。

---

## P2 — 新增 Capability（需要改架构，但不能算大改动）

### T2-1：新增 `DIVIDEND` Capability + `DividendRecord` schema

**当前问题**：除权除息数据没有独立接口，只能在 `fetch_fundamentals` 里附带。

**新增**：

1. `capabilities.py`：`Capability.DIVIDEND = "dividend"`
2. `schemas.py`：`DividendRecord` dataclass
   ```python
   @dataclass
   class DividendRecord:
       symbol: str = ""
       plan_announce_date: str = ""   # 分红预案公告日
       operate_date: str = ""         # 除权除息日
       pay_date: str = ""             # 派息日
       stock_market_date: str = ""    # 红股上市交易日
       cash_per_share: float = 0.0    # 每股税前股利
       stock_per_share: float = 0.0   # 每股送股
       reserve_to_stock: float = 0.0  # 每股转增股
   ```
3. `baostock.py`：`fetch_dividend(self, symbol, year)` → 调用 `query_dividend_data`
4. `gateway.py`：新增 `get_dividend(symbol, year)` 路由方法

**用途**：计算历史股息率、复权价格调整、选股因子（高股息策略）。

---

### T2-2：新增 `INDUSTRY_CLASSIFICATION` Capability

**当前问题**：`Fundamentals.industry` 已有一级行业字段，但没有独立接口，且只有 Baostock 支持申万行业分类。

**新增**：

1. `capabilities.py`：`Capability.INDUSTRY_CLASSIFICATION = "industry_classification"`
2. `schemas.py`：`IndustryClassification` dataclass
   ```python
   @dataclass
   class IndustryClassification:
       symbol: str = ""
       code_name: str = ""
       industry: str = ""       # 申万一级行业名（如"白酒"）
       classification: str = ""  # 分类来源（如"申万一级行业"）
       update_date: str = ""     # 更新日期
   ```
3. `baostock.py`：`fetch_industry_classification(self, symbol) -> IndustryClassification`
4. `gateway.py`：`get_industry_classification(symbol)` 路由方法

**用途**：行业轮动策略、板块选股。

---

### T2-3：新增 `INDEX_CONSTITUENT` Capability（成分股权pect/）

**当前问题**：选股池无法从系统内获取，需要手动维护。

**新增**：

1. `capabilities.py`：`Capability.INDEX_CONSTITUENT = "index_constituent"`
2. `schemas.py`：`IndexConstituent` dataclass
   ```python
   @dataclass
   class IndexConstituent:
       index_code: str = ""    # 指数代码（sz50/hs300/zz500）
       symbol: str = ""        # 成分股代码
       code_name: str = ""     # 成分股名称
       update_date: str = ""    # 更新日期
   ```
3. `baostock.py`：
   - `fetch_sz50_stocks(self) -> List[IndexConstituent]`
   - `fetch_hs300_stocks(self) -> List[IndexConstituent]`
   - `fetch_zz500_stocks(self) -> List[IndexConstituent]`
4. `gateway.py`：`get_index_constituents(index_code)` 路由方法

**用途**：构建选股池（沪深300成分股、中证500成分股）、指数增强策略。

---

### T2-4：新增 `TRADE_CALENDAR` Capability

**当前问题**：交易日判断依赖外部库，系统内无独立接口。

**新增**：

1. `capabilities.py`：`Capability.TRADE_CALENDAR = "trade_calendar"`
2. `schemas.py`：`TradeCalendarEntry` dataclass
   ```python
   @dataclass
   class TradeCalendarEntry:
       date: str = ""          # 日期 YYYY-MM-DD
       is_trading_day: bool = False
   ```
3. `baostock.py`：`fetch_trade_calendar(self, start_date, end_date) -> List[TradeCalendarEntry]`
4. `gateway.py`：`get_trade_calendar(start_date, end_date)` 路由方法

**用途**：定时任务判断交易日、避免非交易日请求数据。

---

## P3 — 锦上添花（探索性功能）

### T3-1：启用分钟 K 线（5/15/30/60 分钟）

**改动**：`fetch_kline_minute()` 方法（当前为空实现），复用 `query_history_k_data_plus` 但 `frequency="5/15/30/60"`。

**注意**：分钟线数据量大，需要考虑缓存策略和请求频率限制。

---

### T3-2：启用 `query_performance_express_report`（业绩快报）和 `query_forecast_report`（业绩预告）

**数据说明**：
- 业绩快报：每年 1 月集中发布，提供全年实际数（快报非审计）
- 业绩预告：提前披露业绩方向（略增/略减/首亏/扭亏等）

**用途**：在财报正式发布前预判业绩方向，对短线交易有参考价值。

---

## 附录：参考代码模板

### 新增 Capability 模板（`capabilities.py`）

```python
# 在 Capability 枚举中添加
DIVIDEND = "dividend"
INDUSTRY_CLASSIFICATION = "industry_classification"
INDEX_CONSTITUENT = "index_constituent"
TRADE_CALENDAR = "trade_calendar"

# 在 _METHOD_MAP 中添加
(Capability.DIVIDEND, "fetch_dividend"): CapabilityPolicy(...),
(Capability.INDUSTRY_CLASSIFICATION, "fetch_industry_classification"): CapabilityPolicy(...),
(Capability.INDEX_CONSTITUENT, "fetch_index_constituents"): CapabilityPolicy(...),
(Capability.TRADE_CALENDAR, "fetch_trade_calendar"): CapabilityPolicy(...),
```

### 新增 Schema 模板（`schemas.py`）

```python
@dataclass
class DividendRecord:
    symbol: str = ""
    plan_announce_date: str = ""
    operate_date: str = ""
    pay_date: str = ""
    stock_market_date: str = ""
    cash_per_share: float = 0.0
    stock_per_share: float = 0.0
    reserve_to_stock: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
```

### Baostock 查询模板

```python
rs = session._bs.query_dividend_data(bs_code, year="2024", yearType="operate")
data = []
while (rs.error_code == '0') & rs.next():
    data.append(rs.get_row_data())
df = pd.DataFrame(data, columns=rs.fields)
```

---

## 任务完成标准

- [ ] 每完成一个 T-任务，提交一个独立 commit（中文 commit message）
- [ ] 每个 Capability 新增，运行全量单元测试（`pytest tests/ -x -q`）
- [ ] 每条数据路径（AkShare ↔ Baostock）用真实股票（如 600809.SH）验证输出 non-null
- [ ] 文档同步更新（ARCHITECTURE.md 的能力矩阵图同步更新）
