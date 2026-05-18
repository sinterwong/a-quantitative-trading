# 架构

## 进程模型

入口在 `quant_app/main.py`,启动时按 `--mode` 装配:

| mode | 装配 |
|---|---|
| `all`(默认) | Flask API + Scheduler + IntradayMonitor + StrategyRunner |
| `api` | 只起 HTTP API |
| `worker` | 只起 Scheduler / Monitor / Runner |

`backend/main.py` 是 56 行 shim,转发 `quant_app` 的符号以兼容旧调用。

OS 级单实例锁在 `core/single_instance.py`(`fcntl.flock` + PID 文件),
同一机器同时跑两个 mode=all 会被拒。

## 分层

```
┌─────────────────────────────────────────────────────────────┐
│ UI (Streamlit)         ui/pages/*.py                        │
│ REST API (Flask)       backend/api.py                       │
│ Scheduled jobs         quant_app/run_worker.py              │
│ Research CLI           scripts/quant/*_cli.py               │
└──────────────────────────┬──────────────────────────────────┘
                           │ 全部通过 use_cases 调业务
┌──────────────────────────▼──────────────────────────────────┐
│ Use Cases              core/use_cases/                      │
│   Request/Response 是 dataclass,所有业务实现单点          │
├─────────────────────────────────────────────────────────────┤
│ Domain                 core/factors/  core/strategies/      │
│                        core/regime.py  core/risk_engine.py  │
│                        core/portfolio_optimizer.py  ...     │
├─────────────────────────────────────────────────────────────┤
│ Operations             Scheduler (quant_app/run_worker.py)  │
│                        IntradayMonitor (backend/services/intraday/) │
│                        Alerts (backend/services/channels/)  │
├─────────────────────────────────────────────────────────────┤
│ Persistence            data/state.db (SQLite)               │
│                        data/*.parquet (历史 K 线 / 情感)   │
├─────────────────────────────────────────────────────────────┤
│ Config                 config/trading.yaml + .env           │
├─────────────────────────────────────────────────────────────┤
│ Data Gateway           core/data_gateway/                   │
│   全系统对外网数据的唯一出口                              │
└─────────────────────────────────────────────────────────────┘
```

## Use Case 层

`core/use_cases/` 下每个文件对应一个业务用例。约定:

- 输入是 `Request` dataclass,输出是 `Response` dataclass(或 `Report`)
- 异常统一抛 `UseCaseError(message, code='...')`
- API endpoint / Streamlit page / CLI / Scheduler 都调同一组函数

当前已建的 use case:

| 模块 | 说明 |
|---|---|
| `analyze_stock/` | 单股票综合分析(A 股 + 港股 dispatch) |
| `backtest.py` | 单标的回测 |
| `compose_portfolio.py` | 组合优化建议 |
| `daily_analysis.py` | 日终选股(DynamicStockSelector → signals + JSON) |
| `intraday_signals.py` | 盘中信号生成(IntradayMonitor 调用) |
| `morning_workflow.py` | 盘前选股 + 早报 |
| `pairs_trading_signal.py` | 配对交易信号 |
| `performance_summary.py` | 收益/绩效汇总 |
| `risk_snapshot.py` | 风控快照 |
| `sector_rotation_signal.py` | 行业轮动信号 |
| `system_health.py` | 系统健康度评级 |

## Data Gateway

`core/data_gateway/` 是对外网的唯一出口。其它模块要数据只能调
`from core.data_gateway import get_gateway`。

### 内部组件

```
DataGateway
  ├─ HealthTracker     滑窗评分,按 (provider × capability) 排序
  ├─ CircuitBreaker    失败累计触发硬开关
  └─ MemoryCache       TTL 按数据类型分(详见缓存策略)

Provider 注册表
  ├─ TencentProvider     qt.gtimg.cn / web.ifzq.gtimg.cn  (主选,字段最全)
  ├─ SinaProvider        hq.sinajs.cn  (实时行情备选)
  ├─ EastmoneyProvider   push2.eastmoney.com  (板块/北向/全市场快讯)
  ├─ BaostockProvider    api.baostock.com  (A 股基本面 + 日 K)
  ├─ AkShareProvider     akshare  (宏观/基本面/融资融券/资金流,兜底)
  └─ YFinanceProvider    yfinance  (美股/港股指数兜底)
```

### Capability 矩阵

| Capability | Tencent | Sina | Eastmoney | Baostock | AkShare | Yfinance |
|---|---|---|---|---|---|---|
| QUOTE | A/HK/INDEX/US | A/HK/INDEX | A/HK/INDEX | — | — | — |
| KLINE_DAILY | A/HK/INDEX | A | — | A | — | US/GLOBAL |
| KLINE_MINUTE | HK | A | — | — | — | — |
| MARKET_INDEX | A/HK/INDEX/US | A/INDEX | A/HK/INDEX | — | — | US/GLOBAL |
| FUNDAMENTALS | — | — | — | A | GLOBAL | — |
| FUNDAMENTALS_HISTORY | — | — | — | A | GLOBAL | — |
| BALANCE_SHEET | — | — | — | A | — | — |
| SECTOR_RANKING | — | A(备) | A(主,含资金流) | — | — | — |
| SECTOR_CONSTITUENTS | — | — | A | — | — | — |
| NORTH_FLOW | — | — | A | — | GLOBAL | — |
| MARGIN_FLOW | — | — | — | — | GLOBAL(单日快照, start 参数被忽略) | — |
| FUND_FLOW | — | — | — | — | GLOBAL | — |
| NEWS_HEADLINES | — | — | GLOBAL(全市场快讯, symbol 被忽略) | — | — | — |
| MACRO | — | — | — | — | GLOBAL | — |

### 选源策略

G4 后由 `capabilities.ROUTING_POLICY` 集中声明，`DataGateway._route()`
查表分派到对应底层原语：

| 策略 | 适用场景 | 底层原语 |
|---|---|---|
| `FAILOVER` | 单点快照 / list 类（sectors / market_index / macro / margin / news_headlines）| `_sequential_fetch` 按健康度逐个尝试，首个成功返回 |
| `MERGE_FIELDS` | dataclass 多源字段互补（quote / fundamentals / balance_sheet）| `_merged_fetch` 并发 top-K，按 `provider_health × field_authority` 字段级胜出 |
| `MERGE_FRAMES` | 时序 DataFrame 列级互补（kline / north_flow_history / fund_flow / fundamentals_history）| `_merged_history_fetch` 行索引并集 + 列级 score 胜出 |
| `MERGE_LISTS` | 多源 list 归一去重（news_headlines）| `_merged_list_fetch` 并发拉所有源 → 归一标题 dedupe（去 "【...】"/末尾"。"/全角空格转半角）→ 按 ts 倒序、缺 ts 排末尾 |

新增数据类型时在 `ROUTING_POLICY` 加一行 `(Capability, fetch_*) → CapabilityPolicy(strategy, skip_fields, ffill)` 即可；
未登记的 (cap, fn) 调用 `_route` 直接 KeyError，杜绝静默走默认分支。

### 字段权威权重

| Provider | 字段 | 权重 |
|---|---|---|
| Tencent | `pe_ttm / pb / market_cap / float_cap / high_52w / low_52w` | 1.3 |
| Tencent | `turnover_rate / amplitude / limit_up / limit_down` | 1.2 |
| Sina | `bid1_price / bid1_vol / ask1_price / ask1_vol` | 1.2 |

### 缓存 TTL

| 数据类型 | TTL |
|---|---|
| 实时行情 Quote | 30s |
| 基本面 | 60s |
| 板块排名 / 成分 | 60s |
| 北向资金 / 指数 | 60s |
| 日 K | 300s |
| 分钟 K | 60s |
| 宏观 | 24h |
| 基本面历史时序 | 24h |
| 融资融券 / 资金流 | 4h |
| 新闻标题 | 30min |

### 调用示例

```python
from core.data_gateway import get_gateway

gw = get_gateway()
gw.quote('600519.SH')                           # 实时行情(字段级多源合并)
gw.kline('600519.SH', interval='daily', days=120)  # 日K(G1 列级合并)
gw.kline('00700.HK', interval='5m', limit=100)  # 分钟 K(仅 HK)
gw.market_index('sh000001')
gw.sectors(limit=50)
gw.north_flow()
gw.macro('PMI')                                  # MacroIndicator.PMI
gw.fundamentals('600519.SH')
gw.fundamentals_history('600519.SH')              # 时序(G1+G3 全量缓存)
gw.profile('600519.SH')                          # G2 聚合信息包，一次拿到所有切片
```

### Sprint 1 重构(2026-05 落地)

- **G8 TieredCache**: L1 内存 + L2 ParquetDiskCache，进程重启不丢，
  跨进程共享。受益能力(白名单)：KLINE_DAILY / FUNDAMENTALS_HISTORY /
  BALANCE_SHEET / MARGIN_FLOW / FUND_FLOW / NORTH_FLOW / MACRO。
  路径：`data/cache/data_gateway/`（`TRADING_DATA_GATEWAY_CACHE_DIR` 覆盖）。
- **G3 时序缓存全量化**: 缓存键去掉 start/end/days/limit 等切片参数，
  内部存"已知最长时序"，出口处切片。同 symbol 不同窗口共享同一缓存。
- **G1 时序数据列级合并**: `_merged_history_fetch` 通用 helper，
  并发拉多源、行索引并集 + 列级 score 胜出。kline / fund_flow /
  north_flow_history / fundamentals_history 统一走它。
- **G2 StockProfile 聚合视图**: `gw.profile(symbol)` 一次并发触发
  quote / fundamentals / balance_sheet / margin / fund_flow /
  headlines / macro 全部切片，组装 `StockProfile`（含 completeness +
  provenance）。任意切片失败不阻塞，由独立 executor 避免与
  `self._executor` 嵌套提交死锁。

### Sprint 2 重构(2026-05 落地)

- **G4 CapabilityPolicy 路由元数据**: 把硬编码在各 gw.* 方法里的
  `_sequential_fetch / _merged_fetch / _merged_history_fetch` 分派
  + `skip_fields` + `ffill` 抽到 `ROUTING_POLICY` 声明表，
  `DataGateway._route()` 单点查表分派。副作用：FAILOVER 现在也把源名
  写入 `_last_provenance[key] = {"_provider": name}`，G2 的 margin /
  news 的 best-effort 注释作废。
- **G5 news 多源归一去重**: 新增 `schemas.NewsItem(title, timestamp,
  source, content)`，`base.fetch_news_headlines` 升级到 `List[NewsItem]`。
  EM 解析 showtime 写入 timestamp；AkshareProvider 新增 NEWS_HEADLINES
  capability，通过 `ak.stock_info_global_cls` 拉财联社电报作第二源。
  gateway `_merged_list_fetch` 并发拉所有候选源，按归一标题
  （去 "【...】"/末尾"。"/全角空格转半角）去重 + 按 timestamp 倒序排序；
  缺 ts 条目排末尾。`gw.news_headlines(symbol, n) -> List[str]` 公开
  签名不变，内部投影 NewsItem.title。
- 详细路线图见 `docs/TODO.md`。

## 因子流水线

`core/pipeline_factory.py:build_pipeline()` 构造 `DynamicWeightPipeline`,
供 `StrategyRunner` 和回测共用。

### 默认因子

技术(`core/factors/price_momentum.py`、`core/factors/technical.py`、`core/strategies/macd_trend.py`):

| 因子 | 默认权重 |
|---|---|
| RSIFactor | 0.20 |
| MACDTrendFactor | 0.20 |
| BollingerFactor | 0.15 |
| ATRFactor | 0.10 |

基本面(`core/factors/fundamental.py`):

| 因子 | 默认权重 |
|---|---|
| PEPercentileFactor | 0.10 |
| ROEMomentumFactor | 0.10 |
| RevenueGrowthFactor | 0.05 |
| CashFlowQualityFactor | 0.05 |

宏观(`core/factors/macro.py`):

| 因子 | 默认权重 |
|---|---|
| PMIFactor | 0.05 |
| M2GrowthFactor | 0.05 |

### 动态权重

`DynamicWeightPipeline` 每 21 个交易日按 63 天滚动 IC 重新分配权重。
连续 3 次 IC<0 的因子自动清零,IC 转正后以 50% 等权重复活。

## 策略运行

`StrategyRunner`(`core/strategy_runner.py`)每 5 分钟运行一次:

1. 取标的列表(持仓 ∪ watchlist)
2. 调 pipeline 得 `combined_score`
3. `core/regime.py:get_regime()` 检测市场状态(`CALM / BULL / BEAR / VOLATILE`)
4. 输出 `BUY / SELL / HOLD`

`IntradayMonitor`(`backend/services/intraday_monitor.py` + 5 个 Mixin 子模块)
在此基础上做 RSI 二次确认 + 风控过滤 + 模拟下单 + 飞书推送。

IntradayMonitor 内部按职责拆 5 个文件(`backend/services/intraday/`):

| 模块 | 职责 |
|---|---|
| `data.py` | 行情/选股/参数数据拉取 |
| `signaling.py` | 主循环 `_check_and_push` + use case 调用 |
| `risk.py` | 仓位裁剪、ExitEngine、组合熔断 |
| `execution.py` | 智能路由、模拟下单、交易模式切换 |
| `alerts.py` | 飞书推送、LLM 终极审核、可观测性日志 |

5 个 Mixin 通过 `IntradayMonitor(DataMixin, SignalingMixin, RiskMixin,
ExecutionMixin, AlertsMixin)` 组合,共享状态在父类 `__init__` 中
集中声明,跨线程访问受 `_state_lock`(RLock)保护。

## 风控

`core/risk_engine.py` 三层:`PreTrade`(下单前)、`InTrade`(持仓中)、
`PostTrade`(收盘后)。CVaR + 蒙特卡洛压力测试在 `core/risk_engine.py`
的 `MonteCarloStressTest`,默认 10000 次。

## 持久化

| 数据 | 文件 | 说明 |
|---|---|---|
| 组合 / 订单 / 信号 / 审计 | `data/state.db` | SQLite,`core/state_db.state_db_path()` 解析,三级回退(env > canonical > legacy `backend/services/portfolio.db`) |
| 历史 K 线 / 情感缓存 | `data/*.parquet` | Parquet 时序 |
| Walk-Forward 结果 | `wf_results.db` | 暂未合入 state.db |

## 配置

- 主配置:`config/trading.yaml`(从 `trading.yaml.example` 复制)
- Secrets:`.env`(`quant_app/main.py` 启动时 `setdefault` 加载,既有 env 优先)
- 优先级:env > YAML > 代码默认值

启动时 `core.config.warn_legacy_configs()` 扫描遗留 JSON
(`params.json` / `live_params.json` / `trading_mode.json`)并打 DeprecationWarning。

## LLM Provider

`core/llm_provider.py` 是 use case 层与 LLM 实现之间的服务定位器。
backend 首次 `import backend.services.llm` 时自动注册工厂,
use case 通过 `core.llm_provider.create_provider()` 取 provider,
不直接依赖 backend。

支持的 provider(`backend/services/llm/providers/`):MiniMax / DeepSeek / Kimi。
通过 `LLM_PROVIDER` 环境变量选择,默认 minimax。

## OpenAPI

`backend/openapi.json` 由 `scripts/generate_openapi.py` 从
`backend/api.py:url_map` 自动生成,提交进 git。

- 本地修改路由后重跑 `python scripts/generate_openapi.py`。
- CI 运行 `--check` 校验,未同步则红。
- `/docs` 端点直接读这份文件,不在运行时再生成。

## 鉴权

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `TRADING_API_KEY` | 空 | 设置后非公共端点必须带 `X-API-Key` |
| `TRADING_API_REQUIRE_LOCALHOST` | `0` | `1` 时取消本机回环豁免 |
| `TRADING_API_RATE_LIMIT_PER_MIN` | `0` | per-IP 每分钟上限,0 = 关闭 |

公共端点(无需鉴权):`/health` / `/docs` / `/metrics` / `OPTIONS`。

生产部署务必同时设置 `TRADING_API_KEY` 与 `TRADING_API_REQUIRE_LOCALHOST=1`,
否则同机进程可绕过鉴权。

## 每日时间线

Scheduler 时间表(`quant_app/run_worker.py`):

| 时间 | 任务 |
|---|---|
| 09:30 | `morning_runner`:选股 → watchlist → RSI 信号 → 模拟下单 → 飞书早报 |
| 09:31 | IntradayMonitor 启动 5 分钟轮询 |
| 15:00 | `afternoon_report`:收盘晚报(持仓快照 + 收益 → 飞书) |
| 15:10 | `/analysis/run`:日终 DynamicStockSelector 选股 |
| 15:30 | `daily_risk_report`:CVaR + 蒙特卡洛压力测试 |
| 15:45 | `daily_tca`:TCA 反馈闭环 |
| 16:00 | `daily_ops_report`:每日运营报告 → 飞书 |

非交易日(周末 / 节假日)全部跳过。触发窗口 ±60 秒,同一任务每日只触发一次。
