# scripts/ 核心量化脚本复盘报告

> 复盘时间：2026-05-15
> 复盘范围：morning_runner.py / afternoon_report.py / dynamic_selector.py
> 重点：核心逻辑、过时硬编码、数据层对齐、升级潜力

---

## 一、scripts/morning_runner.py — 早盘流程全链路

### 1.1 核心逻辑与算法（分步骤）

**Step 0：动态选股**
- 调用 `DynamicStockSelector()` 执行五维度综合评分选股
- 流程：`fetch_market_news(30)` → `fetch_sectors()` → `calc_all_scores()` → `get_stock_with_context(n)`
- 默认选取 Top 5 候选标的，返回 `{symbol, name, reason, score}` 结构

**Step 1：同步 Watchlist 到 Backend**
- 清空旧 watchlist（逐一 DELETE `/watchlist/{sym}`）
- 逐个 POST `/watchlist/add` 写入新标的
- 默认 5% 预警阈值（`alert_pct: 5.0`）
- 目的：供 `IntradayMonitor` 09:31 第一轮扫描时读取

**Step 2：读取市场环境（Regime）**
- 从 `scripts/quant/regime_detector.py` 获取缓存的 regime 结果
- 四种环境：BULL / BEAR / VOLATILE / CALM
- 读取参数：`rsi_buy`, `rsi_sell`, `atr_threshold`, `atr_ratio`
- **仅用于早报展示**，不再用于下单决策

**Step 3：记录开盘 daily_meta**
- 获取持仓 + 现金 + 总权益
- POST `/portfolio/daily` 写入开盘快照（含 regime、候选数、持仓数、权益等 notes）

**Step 4：生成早报 + 飞书推送**
- 调用 `morning_report.build_report()` 生成结构化早报
- 降级兜底：build_report 失败时自动生成简化版文本
- 飞书推送：通过 tenant_access_token → 发 text 消息到 chat_id
- SSL 验证关闭（`CERT_NONE`）

### 1.2 潜在过时逻辑 / 硬编码参数

| 问题 | 位置 | 详情 |
|------|------|------|
| **硬编码端口** | `BASE_URL = 'http://127.0.0.1:5555'` | Backend 端口写死，无法配置化 |
| **硬编码选股数量** | `fetch_selected_stocks(n=5)` | 默认5只，未从配置读取 |
| **硬编码预警阈值** | `alert_pct: 5.0` | watchlist 预警百分比写死 |
| **硬编码 Regime 默认值** | `rsi_buy=25, rsi_sell=65, atr_threshold=0.85` | 失败时的 CALM 默认值 |
| **nav 固定写 1.0** | `log_opening_state` 中 `'nav': 1.0` | 日初净值固定为1.0，无实际意义 |
| **SSL 完全关闭** | `ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE` | 安全隐患 |
| **飞书 token 硬编码 URL** | `https://open.feishu.cn/open-apis/...` | 无法切换为其他 IM |
| **手动 proxy 清除** | `for _k in ...: del os.environ[_k]` | 入侵式环境变量修改，全局副作用 |
| **sys.path 手动拼接** | `sys.path.insert(0, ...)` | 依赖运行路径，易在部署时断裂 |

### 1.3 与数据层对齐分析

| 维度 | 对齐状态 | 说明 |
|------|----------|------|
| **DynamicStockSelector** | ✅ 已对齐 | 已通过 `core.data_gateway.get_gateway()` 获取板块数据 |
| **Regime Detector** | ⚠️ 部分对齐 | 使用 AkShare (`stock_zh_index_daily`)，未走 DataGateway 统一入口 |
| **Watchlist API** | ✅ 对齐 | 通过 HTTP REST API 与 Backend 交互，接口稳定 |
| **Portfolio API** | ✅ 对齐 | 同上 |
| **Baostock / BalanceSheet** | ❌ 未涉及 | morning_runner 不涉及基本面/资产负债表数据 |

### 1.4 升级潜力点

1. **配置外部化**：将 BASE_URL、选股数量、alert_pct、飞书凭据等抽取到统一配置（.env 或 config.yaml）
2. **飞书 SDK 替换**：用 `feishu-python-sdk` 替代手写 HTTP 请求，支持富文本/卡片消息
3. **早报内容增强**：接入 Baostock 基本面数据，增加候选标的的 PE/PB/ROE 信息
4. **并行化 Step 0-2**：选股、watchlist 同步、regime 检测三者可并行执行
5. **错误恢复机制**：选股失败时可 fallback 到前一日 watchlist，而非发送空报告
6. **日志标准化**：替换 `_log` 为统一的 `logging.getLogger()` 标准用法（已在用但手动级别过滤冗余）

---

## 二、scripts/afternoon_report.py — 收盘晚报全链路

### 2.1 核心逻辑与算法（分步骤）

**Step 1：获取组合快照**
- GET `/portfolio/summary` → 持仓、现金、总权益、浮动盈亏、已实现盈亏
- GET `/trades` → 筛选今日成交（按 `executed_at` 前缀匹配日期）
- GET `/signals` → 筛选今日信号（按 `emitted_at` 前缀匹配日期）

**Step 2：计算日收益率**
- 从 `/portfolio/daily` 获取日初净值（通过 `trade_date` 匹配 today）
- 降级策略：找不到 today meta 时，取最近一条 meta 的 equity 作为日初
- 计算：`daily_pnl = closing_equity - opening_equity`
- `daily_return_pct = daily_pnl / opening_equity * 100`

**Step 3：记录收盘 daily_meta**
- POST `/portfolio/daily` 写入收盘数据
- nav 计算：`closing_equity / 100000`（**硬编码净值基值**）
- notes 包含：unrealized、realized、daily_ret

**Step 4：生成收盘晚报**
- 结构化文本包含：
  - 总权益 + 今日收益（金额 + 百分比）
  - 已实现/浮动盈亏分拆
  - 持仓明细（股票、股数、成本、最新价、浮盈百分比）
  - 今日成交明细（方向、股票、股数、价格、盈亏）
  - 今日信号（最近5个）
- 推送飞书（同 morning_runner 的飞书逻辑）

### 2.2 潜在过时逻辑 / 硬编码参数

| 问题 | 位置 | 详情 |
|------|------|------|
| **净值基值硬编码** | `nav = equity / 100000` | 假设初始资产10万，实际应从配置或历史获取 |
| **日期匹配粗糙** | `str(t.get('executed_at', '')).startswith(today)` | 字符串前缀匹配，跨时区/格式不一致可能漏数据 |
| **信号截断** | `today_sigs[-5:]` | 硬编码只展示最近5个信号 |
| **BASE_URL 硬编码** | `http://127.0.0.1:5555` | 同 morning_runner |
| **SSL 关闭** | `CERT_NONE` | 同 morning_runner |
| **飞书推送代码重复** | `feishu_push()` 函数 | 与 morning_runner 中的飞书推送逻辑完全重复 |
| **proxy 环境变量清除** | 同 morning_runner | 全局副作用 |
| **无大盘指数对比** | — | 晚报未包含沪深300/创业板涨幅对比 |

### 2.3 与数据层对齐分析

| 维度 | 对齐状态 | 说明 |
|------|----------|------|
| **Portfolio API** | ✅ 对齐 | 通过 HTTP API 获取持仓/成交/信号 |
| **Baostock / BalanceSheet** | ❌ 未涉及 | 收盘报告不使用基本面数据 |
| **DataGateway** | ❌ 未涉及 | 未通过 DataGateway 获取任何市场数据 |
| **大盘指数数据** | ❌ 缺失 | 未获取当日大盘指数涨跌幅用于对比 |

### 2.4 升级潜力点

1. **净值基值配置化**：从 daily_meta 历史或配置文件读取初始净值基值
2. **飞书代码抽取**：将飞书推送逻辑抽取为独立模块 `core/notification/feishu.py`，三个脚本共用
3. **加入 Benchmark 对比**：通过 DataGateway 获取沪深300/创业板当日涨幅，计算超额收益
4. **加入净值曲线数据**：利用 daily_meta 历史数据生成最近N日净值趋势
5. **成交盈亏计算**：当成交数据包含 buy/sell 配对时，计算已实现单笔盈亏
6. **卡片消息升级**：飞书推送改为交互式卡片（Interactive Card），支持一键查看持仓详情
7. **定时触发**：集成 APScheduler 或 cron，15:00 自动触发而非手动运行

---

## 三、scripts/dynamic_selector.py — 动态选股核心（DynamicStockSelector）

### 3.1 核心逻辑与算法（分步骤）

#### 架构概览

五维度加权评分体系，对**板块级别**评分后，从 Top 板块中抽取成分股：

```
总分 = 新闻(15%) + 行情(35%) + 资金(25%) + 技术(15%) + 一致性(10%) + 情绪加成
```

#### Step 1：新闻热度分（15%权重）

**数据源**（三级 fallback）：
1. 文件缓存（30分钟有效）→ `scripts/cache/news.json`
2. 东方财富 `getNPList` API（主数据源）
3. 同花顺 `10jqka.com.cn` 备用

**评分算法**：
- 关键词分类：政策(10) > 业绩(8) > 产品(7) > 资金(6) > 行业(4) > 一般(3) > 传闻(1)
- 热度加成：hot_value > 1000 → ×1.5；> 500 → ×1.2
- 质量过滤：通过 `news_quality.py` 模块（D级丢弃、C级半折、A/B级全价）
- 情绪增强：通过 `NewsSentimentScorer`（延迟单例）获取板块级情绪分数
- 归一化：最大值归一化到 0-100 分

**新闻→板块映射**：通过 `SECTOR_NEWS_KEYWORDS` 字典（15个大类板块、100+关键词）

#### Step 2：板块行情分（35%权重）

**数据源**（两级 fallback）：
1. `core.data_gateway.get_gateway().sectors(limit=100)` → 东方财富板块排名
2. 文件缓存（1小时有效）→ `scripts/cache/sectors.json`

**评分算法**：
- 按涨跌幅排序：`perf_score = (N - rank) / N × 100`（排名越高分越高）
- 按资金净流入排序：`flow_score = (N - rank) / N × 100`
- 结果存储在 `bk_scores[bk_code]` 中

#### Step 3：资金流向分（25%权重）

- 与 Step 2 同源，使用板块数据中的 `f62`（主力净流入）字段
- 同样按排名归一化到 0-100 分

#### Step 4：技术趋势分（15%权重）

**数据获取**：
- 通过 `get_gateway().sector_constituents(bk_code, limit=top_n)` 获取板块成分股
- 实例级缓存 `_constituent_cache` 避免重复请求

**评分算法**（成分股涨跌幅阶梯映射）：
```
涨跌幅 > 3%    → 100分
> 1.5%         → 80分
> 0.5%         → 65分
> 0%           → 55分
> -0.5%        → 45分
> -1.5%        → 30分
> -3%          → 15分
else           → 5分
```
取成分股均值。

#### Step 5：成分股一致性分（10%权重）

**评分逻辑**：
- 获取板块 TOP 3 成分股
- 计算上涨/下跌/平盘家数比例
- 分三种场景：
  - 强势板块（avg_change > 0.5%）：上涨比例 ≥80% → 100分；≥60% → 80分；≥50% → 60分
  - 弱势板块（avg_change < -0.5%）：下跌比例越多一致性越强
  - 震荡板块：看是否齐涨共跌

#### Step 6：环境调制（Regime Modulation）

`_regime_modulate()` 函数根据市场环境调整板块总分：
- **BULL**：动量板块（AI/芯片/军工/机器人等）×1.2
- **BEAR**：防御板块（电力/医药/银行/消费等）×1.2；其他 ×0.85
- **VOLATILE**：所有板块 ×0.80
- **CALM**：不做调整

#### Step 7：最终选股

1. 取 Top N 板块（按 total 分排序）
2. 过滤 total_score < 20 的板块
3. 每板块取 Top 3 成分股
4. 去重，取前 `top_n` 只返回
5. **不做 ETF 兜底**（选不出返回空列表）

### 3.2 潜在过时逻辑 / 硬编码参数

| 问题 | 位置 | 详情 |
|------|------|------|
| **权重硬编码** | `WEIGHT_NEWS=0.15, WEIGHT_SECTOR=0.35...` | 无法动态调整，应支持配置或自适应 |
| **新闻关键词硬编码** | `NEWS_POLICY_KEYWORDS`, `SECTOR_NEWS_KEYWORDS` | 约100+关键词直接写死在代码中 |
| **板块关键词映射不全** | `SECTOR_NEWS_KEYWORDS` | 缺少「低空经济」「人形机器人2026」等2025-2026新热点 |
| **ETF 兜底已移除** | `select_stocks()` | 注释说"不做ETF兜底"，但 `FALLBACK_ETFS` 常量仍在 |
| **技术分阶梯硬编码** | `calc_tech_score_for_bk` | 涨跌幅阈值（3%/1.5%/0.5%）写死 |
| **一致性分仅取3只** | `calc_consistency_score_for_bk` | 注释说"10只"，实际 `top_n=3`，样本太少 |
| **新闻 token 硬编码** | `token=586e590d6c8b07833eb5d2e487e1a77` | 东方财富 API token 写死，可能过期 |
| **自定义日志系统** | `_log()` 函数 | 未使用标准 logging，无日志持久化 |
| **SSL 完全关闭** | `SSL_CTX.verify_mode = ssl.CERT_NONE` | 所有 HTTP 请求禁用 SSL 验证 |
| **缓存目录位置** | `CACHE_DIR = scripts/cache/` | 运行时产物放在 scripts 目录下，应移到 data/cache/ |
| **Regime 仅用于板块调制** | `_regime_modulate` | 未影响个股层面的筛选（如止损、仓位限制） |
| **Baostock/基本面完全缺失** | — | 选股纯靠技术面+消息面，无 PE/PB/ROE 质量过滤 |
| **配对代码重复** | `get()` 和 `get_gbk()` | 两函数逻辑几乎完全重复，仅解码方式不同 |

### 3.3 与数据层对齐分析

| 维度 | 对齐状态 | 详细说明 |
|------|----------|----------|
| **板块数据（SectorRanking）** | ✅ 已对齐 | 通过 `get_gateway().sectors()` 获取，走 DataGateway 统一入口 |
| **板块成分股（SectorConstituent）** | ✅ 已对齐 | 通过 `get_gateway().sector_constituents()` 获取 |
| **个股实时行情（Quote）** | ✅ 已对齐 | 通过 `get_gateway().quote()` 获取 |
| **Baostock Provider** | ❌ 未使用 | BaostockProvider 声明了 `BALANCE_SHEET` 能力，但 dynamic_selector 完全不使用基本面数据 |
| **BalanceSheet Schema** | ❌ 未使用 | schemas.py 中定义了 `BalanceSheet`（total_asset, debt_to_equity, current_ratio 等），但选股流程未接入 |
| **Fundamentals Schema** | ❌ 未使用 | `Fundamentals`（eps_ttm, roe_ttm, revenue_ttm 等）完全未参与选股 |
| **新闻数据源** | ⚠️ 游离于体系外 | 直接 HTTP 请求东方财富/同花顺，未通过 DataGateway 管理 |
| **文件缓存** | ⚠️ 未统一 | dynamic_selector 自建文件缓存（`scripts/cache/`），与 DataGateway 的 `cache.py` 体系独立 |

### 3.4 升级潜力点

#### A. 基本面维度（高优先级）

**当前缺陷**：五维度中没有任何基本面/财务质量维度，容易选到"消息面好但财务烂"的标的。

**建议增加第六维度：基本面质量分（10%-15%权重）**
```python
# 通过 DataGateway 获取 Baostock 基本面数据
gateway = get_gateway()
fund = gateway.fundamentals(symbol)      # Fundamentals: eps_ttm, roe_ttm
bs = gateway.balance_sheet(symbol)       # BalanceSheet: debt_to_equity, current_ratio

# 质量过滤
quality_score = 0
if bs.debt_to_equity < 60:   quality_score += 30  # 资产负债率合理
if bs.current_ratio > 1.5:   quality_score += 20  # 流动性充足
if fund.roe_ttm > 10:        quality_score += 30  # 盈利能力强
if fund.eps_ttm > 0:         quality_score += 20  # 未亏损
```

#### B. 选股过滤增强

```python
# 质量门槛：排除基本面过差的个股
def _quality_filter(stocks: List[str]) -> List[str]:
    gateway = get_gateway()
    passed = []
    for code in stocks:
        bs = gateway.balance_sheet(code)
        fund = gateway.fundamentals(code)
        # 排除条件
        if bs.debt_to_equity > 80:     continue  # 高负债
        if fund.eps_ttm < 0:           continue  # 亏损
        if fund.roe_ttm < 5:           continue  # 低ROE
        passed.append(code)
    return passed
```

#### C. 架构优化

1. **新闻源接入 DataGateway**：增加 `Capability.NEWS` 能力声明，由 gateway 统一管理新闻源健康度和 fallback
2. **缓存统一**：将文件缓存迁移到 `core/data_gateway/cache.py` 体系
3. **HTTP 客户端替换**：用 `httpx` / `aiohttp` 替代 `urllib.request`，支持异步和连接池
4. **权重自适应**：根据历史回测数据动态调整五维度权重
5. **成分股一致性**：将 `top_n=3` 提升到 `top_n=10`，与注释一致

#### D. 新闻关键词更新

```python
# 需要新增的2025-2026热点板块关键词
SECTOR_NEWS_KEYWORDS.update({
    '低空经济': ['低空经济', 'eVTOL', '飞行汽车', '通用航空', '无人机配送'],
    '人形机器人': ['人形机器人', '特斯拉机器人', 'Figure', '具身智能'],
    '量子计算': ['量子计算', '量子通信', '量子芯片'],
    '脑机接口': ['脑机接口', 'BCI', 'Neuralink'],
    '固态电池': ['固态电池', '半固态电池', '全固态'],
    '卫星互联网': ['卫星互联网', '星链', '低轨卫星'],
    'AI Agent': ['AI Agent', '智能体', 'Agentic AI', 'MCP'],
})
```

---

## 四、三脚本共性问题汇总

### 4.1 代码重复

| 重复代码 | 涉及文件 | 建议抽取位置 |
|----------|----------|-------------|
| 飞书推送逻辑（token获取+消息发送） | morning_runner, afternoon_report | `core/notification/feishu.py` |
| Backend API 封装（api_get/api_post/api_delete） | morning_runner, afternoon_report | `core/backend_client.py` |
| Proxy 环境变量清除 | 三个脚本 | 统一入口模块的 `__init__.py` |
| SSL 上下文创建 | 三个脚本 | `core/http_utils.py` |
| .env 加载 | morning_runner, afternoon_report | 统一入口 |

### 4.2 安全风险

1. **SSL 验证完全关闭**（三个脚本均有）：生产环境应启用 SSL
2. **飞书凭据在环境变量**：建议使用 secrets manager 或加密配置
3. **东方财富 token 明文写在代码中**：`token=586e590d6c8b07833eb5d2e487e1a77`

### 4.3 与 DataGateway 整体对齐度

```
┌─────────────────────────┬────────────┬──────────────┬──────────────┐
│ 功能模块                 │ DataGateway │ Baostock    │ BalanceSheet │
├─────────────────────────┼────────────┼──────────────┼──────────────┤
│ 板块排名                 │ ✅ 已用     │ —            │ —            │
│ 板块成分股               │ ✅ 已用     │ —            │ —            │
│ 个股实时行情             │ ✅ 已用     │ —            │ —            │
│ 新闻数据                 │ ❌ 未接入   │ —            │ —            │
│ 基本面数据               │ ❌ 未使用   │ ❌ 未使用    │ ❌ 未使用    │
│ 日K线/技术指标           │ ❌ 未使用   │ ❌ 未使用    │ —            │
│ 市场环境(Regime)         │ ❌ 独立模块 │ 间接(AkShare)│ —            │
│ 持仓管理                 │ HTTP API   │ —            │ —            │
└─────────────────────────┴────────────┴──────────────┴──────────────┘
```

**结论**：DataGateway 的板块数据层已对齐，但 **Baostock 基本面数据（Fundamentals + BalanceSheet）完全未被三个核心脚本使用**。这是最大的数据层 gap——系统已有完整的基础设（Provider + Schema），但业务层未消费。

---

## 五、升级路线图建议

### P0（立即修复）
- [ ] 抽取飞书推送为独立模块，消除三脚本重复
- [ ] 修复 `calc_consistency_score_for_bk` 的 `top_n=3`（注释写10只）
- [ ] 清理 `FALLBACK_ETFS` 死代码

### P1（短期优化，1-2周）
- [ ] 配置外部化（BASE_URL、权重、alert_pct 等）
- [ ] 新闻关键词表配置化（JSON/YAML文件，支持热更新）
- [ ] SSL 验证恢复（至少生产环境）
- [ ] 新闻源接入 DataGateway（增加 Capability.NEWS）

### P2（中期增强，2-4周）
- [ ] dynamic_selector 接入 Baostock 基本面数据（第六维度：质量分）
- [ ] 候选股基本面过滤（ROE > 5%、EPS > 0、负债率 < 80%）
- [ ] afternoon_report 加入 Benchmark 对比（沪深300超额收益）
- [ ] HTTP 客户端现代化（urllib → httpx）

### P3（长期演进）
- [ ] 权重自适应（基于历史回测动态调整）
- [ ] 选股结果回测验证（接入 backtest_cli.py）
- [ ] 全链路异步化（asyncio + httpx）
- [ ] LLM 辅助新闻解读（morning_report.py 已有 LLMService 雏形）
