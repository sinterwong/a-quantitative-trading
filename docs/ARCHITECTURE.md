# 架构

## 进程模型

入口在 `quant_app/main.py`，按 `--mode` 装配：

| mode | 装配 |
|---|---|
| `all`（默认） | Flask API + Scheduler + IntradayMonitor + StrategyRunner |
| `api` | 只起 HTTP API |
| `worker` | 只起 Scheduler / Monitor / Runner |

启动命令：`python -m quant_app.main --mode all`。

进程级单实例锁在 `core/single_instance.py`（`fcntl.flock` + PID 文件）；同机第二个 `mode=all` 会被拒。

进程级 Shutdown 协调器在 `core/lifecycle.py`：注册 SIGTERM / SIGINT handler，所有子系统（Scheduler / IntradayMonitor / 任何 enqueue 点）通过 `get_shutdown().check_or_raise()` 拒绝新工作，已入队任务靠各自的事件循环退出。

## 分层

```
┌─────────────────────────────────────────────────────────────┐
│ UI (Streamlit)         streamlit_app.py + ui/pages/*.py     │
│ REST API (Flask)       backend/api.py + backend/api_routes/ │
│ Scheduled jobs         quant_app/run_worker.py              │
│ Research CLI           scripts/quant/*_cli.py               │
└──────────────────────────┬──────────────────────────────────┘
                           │ 全部通过 use_cases 调业务
┌──────────────────────────▼──────────────────────────────────┐
│ Use Cases              core/use_cases/                      │
│   Request / Response 是 dataclass，业务实现单点            │
├─────────────────────────────────────────────────────────────┤
│ Domain                 core/factors/  core/strategies/      │
│                        core/regime.py  core/risk_engine.py  │
│                        core/portfolio_optimizer.py  ...     │
├─────────────────────────────────────────────────────────────┤
│ Operations             Scheduler (quant_app/run_worker.py)  │
│                        IntradayMonitor (backend/services/intraday/) │
│                        Alerts (backend/services/channels/)  │
├─────────────────────────────────────────────────────────────┤
│ Persistence            data/state.db (SQLite + WAL)         │
│                        data/cache/data_gateway/ (Parquet L2) │
├─────────────────────────────────────────────────────────────┤
│ Config                 config/trading.yaml + .env           │
│                        core/config_defaults.py              │
├─────────────────────────────────────────────────────────────┤
│ Data Gateway           core/data_gateway/                   │
│   全系统对外网数据的唯一出口                              │
└─────────────────────────────────────────────────────────────┘
```

依赖方向：UI / API / Scheduler / CLI → use_cases → domain。use_cases 不依赖 backend / streamlit。

## Use Case 层

`core/use_cases/` 下每个文件对应一个业务用例：

| 模块 | 说明 |
|---|---|
| `analyze_stock/` | 单股综合分析（A 股 / 港股 dispatch） |
| `backtest.py` | 单标的回测 |
| `compose_portfolio.py` | 组合优化建议 |
| `daily_analysis.py` | 日终选股（DynamicStockSelector → signals + JSON） |
| `intraday_signals.py` | 盘中信号生成（IntradayMonitor 调用） |
| `morning_workflow.py` | 盘前选股 + 早报 |
| `pairs_trading_signal.py` | 配对交易信号 |
| `performance_summary.py` | 收益 / 绩效汇总 |
| `risk_snapshot.py` | 风控快照 |
| `sector_rotation_signal.py` | 行业轮动信号 |
| `submit_order.py` | 订单提交（PreTrade 风控 → broker） |
| `system_health.py` | 系统健康度评级 |

约定：

- 输入 `XxxRequest` dataclass，输出 `XxxResponse` 或 `XxxReport` dataclass
- 业务失败抛 `UseCaseError(message, code)`；HTTP 端点映射为 4xx
- 网络 / 数据层失败由 use case 内部 try-except，返回降级响应

## Data Gateway

`core/data_gateway/` 是对外网的唯一出口，其它模块通过 `from core.data_gateway import get_gateway` 拿到 `DataGateway` 单例。

### 组件

```
DataGateway
  ├─ HealthTracker     滑窗评分，按 (provider × capability) 排序
  ├─ CircuitBreaker    失败累计触发硬开关
  └─ TieredCache       L1 MemoryCache + L2 ParquetDiskCache

Providers
  ├─ TencentProvider     qt.gtimg.cn / web.ifzq.gtimg.cn（主选，字段最全）
  ├─ SinaProvider        hq.sinajs.cn（实时备选）
  ├─ EastmoneyProvider   push2.eastmoney.com（板块 / 北向 / 全市场快讯）
  ├─ BaostockProvider    api.baostock.com（A 股基本面 + 日 K）
  ├─ AkShareProvider     akshare（宏观 / 基本面 / 融资融券 / 资金流，兜底）
  └─ YFinanceProvider    yfinance（美股 / 港股指数兜底）
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
| SECTOR_RANKING | — | A(备) | A(主，含资金流) | — | — | — |
| SECTOR_CONSTITUENTS | — | — | A | — | — | — |
| NORTH_FLOW | — | — | A | — | GLOBAL | — |
| MARGIN_FLOW | — | — | — | — | GLOBAL（单日快照） | — |
| FUND_FLOW | — | — | — | — | GLOBAL | — |
| NEWS_HEADLINES | — | — | GLOBAL（EM 快讯） | — | GLOBAL（财联社电报） | — |
| MACRO | — | — | — | — | GLOBAL | — |

### 路由策略

`capabilities.ROUTING_POLICY` 集中声明 capability → strategy 映射，`DataGateway._route()` 查表分派：

| 策略 | 适用场景 | 底层原语 |
|---|---|---|
| `FAILOVER` | 单点快照 / list 类（sectors / market_index / macro / margin / news_headlines） | `_sequential_fetch` 按健康度逐个尝试，首个成功返回 |
| `MERGE_FIELDS` | dataclass 多源字段互补（quote / fundamentals / balance_sheet） | `_merged_fetch` 并发 top-K，按 `provider_health × field_authority` 字段级胜出 |
| `MERGE_FRAMES` | 时序 DataFrame 列级互补（kline / north_flow_history / fund_flow / fundamentals_history） | `_merged_history_fetch` 行索引并集 + 列级 score 胜出 |
| `MERGE_LISTS` | 多源 list 归一去重（news_headlines） | `_merged_list_fetch` 并发拉所有源 → 归一标题 dedupe → 按 ts 倒序 |

未登记的 (cap, fn) 调用 `_route` 直接 KeyError，杜绝静默走默认分支。

### 字段权威权重

| Provider | 字段 | 权重 |
|---|---|---|
| Tencent | `pe_ttm / pb / market_cap / float_cap / high_52w / low_52w` | 1.3 |
| Tencent | `turnover_rate / amplitude / limit_up / limit_down` | 1.2 |
| Sina | `bid1_price / bid1_vol / ask1_price / ask1_vol` | 1.2 |

### 缓存

`TieredCache` = L1 `MemoryCache`（毫秒级）+ L2 `ParquetDiskCache`（重启不丢、跨进程共享）。L2 持久化白名单（仅 DataFrame）：KLINE_DAILY / FUNDAMENTALS_HISTORY / BALANCE_SHEET / MARGIN_FLOW / FUND_FLOW / NORTH_FLOW / MACRO。

路径：`data/cache/data_gateway/`，`TRADING_DATA_GATEWAY_CACHE_DIR` 覆盖。

时序缓存策略：缓存键不含 `start / end / days / limit`，内部存"已知最长时序"DataFrame，出口处按用户参数切片。同 symbol 不同窗口共享同一缓存。

| 数据类型 | TTL |
|---|---|
| Quote | 30s |
| 基本面 / 板块 / 北向 / 指数 | 60s |
| 日 K | 300s |
| 分钟 K | 60s |
| 宏观 / 基本面历史 | 24h |
| 融资融券 / 资金流 | 4h |
| 新闻标题 | 30min |

### 公开 API

```python
from core.data_gateway import get_gateway

gw = get_gateway()
gw.quote('600519.SH')
gw.quotes(['600519.SH', '000001.SZ'])
gw.kline('600519.SH', interval='daily', days=120)
gw.kline('00700.HK', interval='5m', limit=100)
gw.fundamentals('600519.SH')
gw.fundamentals_history('600519.SH')
gw.balance_sheet('600519.SH')
gw.sectors(limit=50)
gw.sector_constituents('BK0475')
gw.north_flow()
gw.north_flow_history(days=252)
gw.market_index('sh000001')
gw.macro(MacroIndicator.PMI)
gw.margin_flow('600519.SH')
gw.fund_flow('600519.SH')
gw.news_headlines('600519.SH', n=20)
gw.profile('600519.SH')  # 聚合视图：一次拉全部切片
```

### StockProfile 聚合视图

`gw.profile(symbol)` 并发触发所有 capability 拉取，返回 `StockProfile`：

- 字段：`quote / fundamentals / balance_sheet / margin / fund_flow_latest / headlines / macro`
- 元数据：`as_of / completeness（0-1）/ provenance（每切片主源）`

实现在 `core/data_gateway/profile.py`。使用独立 ThreadPoolExecutor（与 `DataGateway._executor` 物理隔离）避免嵌套提交死锁。

## 因子流水线

`core/pipeline_factory.build_pipeline()` 构造 `DynamicWeightPipeline`，供 `StrategyRunner` 和回测共用。

默认因子：

| 类别 | 因子（默认权重） |
|---|---|
| 技术 | RSI 0.20 / MACDTrend 0.20 / Bollinger 0.15 / ATR 0.10 |
| 基本面 | PEPercentile 0.10 / ROEMomentum 0.10 / RevenueGrowth 0.05 / CashFlowQuality 0.05 |
| 宏观 | PMI 0.05 / M2Growth 0.05 |

`DynamicWeightPipeline` 每 21 个交易日按 63 天滚动 IC 重新分配权重。连续 3 次 IC<0 的因子自动清零，IC 转正后以 50% 等权重复活。

## 策略运行

`StrategyRunner`（`core/strategy_runner.py`）每 5 分钟运行一次：

1. 取标的列表（持仓 ∪ watchlist）
2. 调 pipeline 得 `combined_score`
3. `core/regime.py:get_regime()` 检测市场状态（`CALM / BULL / BEAR / VOLATILE`）
4. 输出 `BUY / SELL / HOLD`

`IntradayMonitor`（`backend/services/intraday_monitor.py` + `backend/services/intraday/` 子模块）在此基础上做 RSI 二次确认 + 风控过滤 + 模拟下单 + 渠道推送。子模块组织：

| 文件 | 职责 |
|---|---|
| `data.py` | 行情 / 选股 / 参数数据拉取 |
| `signaling.py` | 主循环 `_check_and_push` + use case 调用 |
| `risk.py` | 仓位裁剪、ExitEngine、组合熔断 |
| `execution.py` | 智能路由、模拟下单、交易模式切换 |
| `alerts.py` | 推送、LLM 终极审核、可观测性 |
| `market_hours.py` | 交易时段判断 |
| `cooldown.py` | 信号冷却 |

5 个 Mixin 通过 `IntradayMonitor(DataMixin, SignalingMixin, RiskMixin, ExecutionMixin, AlertsMixin)` 组合，共享状态在父类 `__init__` 集中声明，跨线程访问受 `_state_lock`（RLock）保护。

## 风控

`core/risk_engine.py` 三层：

- **PreTrade**：下单前检查仓位上限、敞口、单股集中度、相关性、VaR
- **InTrade**：持仓中跟踪 ATR / Chandelier Exit / 移动止盈 / 单日熔断
- **PostTrade**：收盘后 CVaR + 蒙特卡洛压力测试（默认 10000 次）

风险状态（HALT_NEW_BUYS 等开关）持久化到 `data/risk_state.json`（`QUANT_RISK_STATE_PATH` 覆盖）。

`risk_engine` 失败信号化：`OptimizationResult.is_fallback / fallback_reason`、`FactorResult.quality`（FULL / DEGRADED / MISSING）、`BacktestResult.degraded_steps`。降级结果对调用方可见，不再静默返回 0。

## 订单提交

`/orders/submit` 端到端流程：

1. **幂等检查**：客户端可带 `Idempotency-Key` header；`core/idempotency.IdempotencyStore` 走三段式协议：
   - `reserve(key, hash)` — DB 主键串行化抢锁，并发同 key 同 hash 仅一个 NEW，其余返回 IN_FLIGHT (HTTP 409)
   - `complete(key, hash, response)` — 成交后落响应；后续同 key 拿 REPLAY (200 + replayed=True)
   - `release(key, hash)` — 错误路径释放占位，重试可再 NEW
   - 同 key 不同 payload → HTTP 422 IDEMPOTENCY_KEY_CONFLICT
   - pending 行超过 60s 自动可被 steal（worker 崩溃恢复用）
2. **use case**：`core.use_cases.submit_order` 解析 ref price → PreTrade 风控 → `broker.submit_order()`
3. **Broker**：`backend.services.broker.PaperBroker`（生产用 HTTP）原子化"查现金 → 撮合 → 写持仓 → 写流水"

## 持久化

| 数据 | 文件 | 说明 |
|---|---|---|
| 组合 / 订单 / 信号 / 审计 / 幂等键 | `data/state.db` | SQLite + WAL，`PRAGMA busy_timeout=5000` |
| K 线 / 基本面 / 资金流时序 | `data/cache/data_gateway/*.parquet` | L2 缓存，重启不丢 |
| 情感分数 / 新闻 | `data/sentiment/` / `data/news_cache/` | Parquet |
| ML 模型 | `data/ml_models/` | joblib |
| 风险状态 | `data/risk_state.json` | HALT_NEW_BUYS 等开关 |
| 交易日历 | `data/trade_calendar.json` | AKShare 拉取后缓存 |
| Walk-Forward 结果 | `wf_results.db` | 暂未合入 state.db |

`core/state_db.state_db_path()` 三级回退：`QUANT_STATE_DB` env > `data/state.db` > legacy `backend/services/portfolio.db`。

并发模型：

- 每次 `PortfolioService.get_cursor()` 在当前线程新建 / 销毁连接，连接不跨线程共享
- 同进程所有写经 `_WRITE_LOCK` 串行化
- 每个连接都设 `PRAGMA journal_mode=WAL / synchronous=NORMAL / busy_timeout=5000`

## 单例与并发

`core/singleton.LockedSingleton[T]` 是双检锁 + 自动注册到 `SingletonRegistry` 的容器。所有跨模块全局态走它：

- `backend.api._svc_singleton`（PortfolioService）
- `backend.api_deps._idempotency_store_singleton / _risk_engine_singleton`
- `core.data_gateway.gateway._gateway_singleton`
- `core.alerting / core.data_layer / core.regime / core.metrics / core.data_gateway.http / core.data_gateway.health` 等

测试 conftest 调 `SingletonRegistry.reset_all()` 统一清理。

## LLM Provider

`core/llm_provider.py` 是 use case 与 LLM 实现之间的服务定位器。backend 首次 `import backend.services.llm` 时注册工厂，use case 通过 `core.llm_provider.create_provider()` 取 provider，不直接依赖 backend。

支持的 provider（`backend/services/llm/providers/`）：MiniMax / DeepSeek / Kimi。通过 `LLM_PROVIDER` 环境变量选择，默认 minimax。

`LLM_REVIEW_FAIL_OPEN=1`（默认）：LLM news review 失败时放行，避免 LLM 故障误拦截下单。

## OpenAPI

`backend/openapi.json` 由 `scripts/generate_openapi.py` 从 `backend/api.py:url_map` 自动生成，提交进 git。

- 本地修改路由后重跑 `python scripts/generate_openapi.py`
- CI 运行 `--check` 校验，未同步则红
- `/docs` 端点直接读这份文件，不在运行时再生成

## 鉴权 / 限流

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `TRADING_API_KEY` | 空 | 设置后非公共端点必须带 `X-API-Key` |
| `TRADING_API_REQUIRE_LOCALHOST` | `0` | `1` 时取消本机回环豁免 |
| `TRADING_RL_PER_MIN` | `120` | per-IP 每分钟上限，0 = 关闭 |

公共端点（无需鉴权）：`/health` / `/docs` / `/metrics` / `OPTIONS`。

`/orders/submit` 单独走 `@rate_limit(max_per_window=10, window_seconds=60)` 装饰器（10 笔/分钟/IP）。

## 每日时间线

| 时间 | 任务 |
|---|---|
| 09:30 | morning_runner：选股 → watchlist → RSI 信号 → 模拟下单 → 早报推送 |
| 09:31 | IntradayMonitor 启动 5 分钟轮询 |
| 15:00 | afternoon_report：收盘晚报 |
| 15:10 | /analysis/run：DynamicStockSelector 日终选股 |
| 15:30 | daily_risk_report：CVaR + 蒙特卡洛压力测试 |
| 15:45 | daily_tca：TCA 反馈闭环 |
| 16:00 | daily_ops_report：每日运营报告 |

非交易日（周末 / 节假日）全部跳过。触发窗口 ±60 秒，同一任务每日只触发一次。
