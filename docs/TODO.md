# 数据层重构路线图

> **Sprint 1 已完成**（commit 11b7e72 → ef10ed2）：G8 / G3 / G1 / G2 全部交付，
> `gw.profile(symbol)` 已可用。详见各章节末尾的「✅ 已完成」标记。
>
> **Sprint 2 已完成**（commit 883f1dd → 43d5075）：G4 / G5 全部交付。
> `ROUTING_POLICY` 元数据驱动 4 种 routing 策略；`gw.news_headlines` 现已
> 走多源去重 + 时间倒序（EM kuaixun + AkShare 财联社电报）。

---



> **核心目标**：让 `core/data_gateway/` 从「多源 failover 网关」升级为
> 「多源冗余聚合 + 信息包合一」的数据层。最终调用方只需要：
> ```python
> profile = get_gateway().profile("600519.SH")
> ```
> 就能拿到一份**信息量巨大、来源透明、字段级互补**的数据包，
> 无需关心数据源在哪、谁更可靠、谁宕机了。

本文档记录从 PR #22 合入后启动的数据层 Sprint 系列重构。

---

## Sprint 1：基础设施 + 信息包雏形

### G8 — 启用 ParquetDiskCache + TieredCache

**动机**：`cache.py:73-136` 已实现 `ParquetDiskCache`，但 `DataGateway` 只用 `MemoryCache`。
进程重启后内存缓存全失，所有冗余数据要重拉一次；多进程间也无法共享缓存。

**范围**：
- `cache.py` 增加 `TieredCache`：L1=MemoryCache（毫秒级）+ L2=ParquetDiskCache（重启不丢）
- `DataGateway.__init__` 默认注入 disk cache，路径取 `data/cache/data_gateway/`
- 选择性启用：仅 K 线 / fundamentals_history / fund_flow / margin_flow / north_flow_history / macro 落盘
  （Quote / 实时类不落盘，避免污染）
- 配置项 `TRADING_DATA_GATEWAY_CACHE_DIR` 可覆盖默认路径

**验收**：
- `tests/test_data_gateway/test_cache.py` 增加 TieredCache 单元测试
- DataGateway 集成测试：重启后 disk cache 命中、L1 失效后从 L2 回填
- 全套既有测试通过

✅ 已完成：commit 99e742c，+9 cache 单元测试 + 4 gateway 集成测试。

---

### G3 — 时序缓存改"全量+切片"

**动机**：现在缓存键含 `start/end/days/limit`：
```python
cache_key = f"fundamentals_history:{symbol}:{start}:{end}"
cache_key = f"kline:{symbol}:{interval}:{days}:{adjust}:{limit}"
```
每个切片占独立缓存槽，而 provider 实际拉的可能是同一份原始数据。这是冗余浪费。

**范围**：
- 改造 `fundamentals_history` / `kline` / `fund_flow` / `north_flow_history`
  / `margin_flow`(若未来接入时序源) 的缓存策略
- 缓存键只含**结构性参数**（symbol / interval / adjust），不含**时间窗口**
- 内部缓存"已知最长时序"DataFrame
- 在 gateway 出口处按用户参数做 `.loc[start:end]` 或 `.tail(n)` 切片
- 提供 `invalidate_history(symbol)` API 精确清除

**验收**：
- 同一 symbol 两次不同时间窗口请求，缓存命中第二次（无网络 IO）
- 切片正确：返回的 DataFrame 索引在用户请求的 [start, end] 区间内
- 不破坏现有 `MarginDataStore` / 因子层调用兼容性

✅ 已完成：commit 5954279，+7 切片复用 / 宽抓取 / 精确 invalidate 测试。

---

### G1 — K 线字段级合并（抽 _merged_history_fetch）

**动机**：`gateway.kline()` 用 `_sequential_fetch`，找到第一个非空源就返回，
完全放弃了多源对账与字段互补能力（腾讯 turnover_rate / amount 字段更全、
Baostock 复权更权威、yfinance 美股延迟更低）。

而 `fundamentals_history()` 已经实现了「按 score 降序、列级互补合并」的成熟模式
（gateway.py:618-671）——这套逻辑应该被抽出来给所有时序数据复用。

**范围**：
- 把 `fundamentals_history` 内联的列合并逻辑抽到 `DataGateway._merged_history_fetch(capability, fn_name, *args)`
- `kline` / `fund_flow` / `north_flow_history` 全部走它
- 同一日同一列多源时，按 `health × authority` 加权胜出
- 保留 `_sequential_fetch` 作为"明确不需要合并"的策略选项（如单只 SectorRanking）

**验收**：
- mock 多个 provider 给同 symbol 不同部分列的 K 线，验证合并后字段并集
- mock 多个 provider 同列不同值，验证按 health 选源
- 既有 `fundamentals_history` 测试不回归

✅ 已完成：commit 37167d2，+5 _merged_history_fetch 直接测试 + 2 kline
重构后语义验证测试。

---

### G2 — StockProfile 聚合视图 + gw.profile()

**动机**：当前调用方要写 8 行才能拼出"我对这只票知道什么"：
```python
quote = gw.quote(sym)
fund = gw.fundamentals(sym)
bs = gw.balance_sheet(sym)
margin = gw.margin_flow(sym, end=today)
fflow = gw.fund_flow(sym).tail(1)
sectors = gw.sectors()
news = gw.news_headlines(sym)
macro = {k: gw.macro(k).tail(1).iloc[0,0] for k in ('PMI','M2','CREDIT')}
```
这违背了「使用者无需关心数据源」的目标。

**范围**：
- `schemas.py` 新增 `StockProfile` dataclass：
  - 字段：quote / fundamentals / balance_sheet / margin / fund_flow_latest /
    sector_info / headlines / macro_snapshot
  - 元数据：`as_of` / `completeness`（0-1）/ `provenance`（每切片来源）
- 子快照 dataclass：`MarginSnapshot`、`FundFlowSnapshot`、`MacroSnapshot`、`SectorInfo`
- `DataGateway.profile(symbol)` 一次并发触发所有 capability 拉取，组装 StockProfile 返回
- 任意切片缺失不阻塞主流程（只影响 completeness）

**验收**：
- mock 全部 capability 返回，验证 StockProfile 字段填充正确
- mock 部分 capability 失败，验证 completeness < 1 且 provenance 记录正确
- `tests/test_data_gateway/test_gateway_profile.py` 新文件

✅ 已完成：commit TBD（本 commit），+11 集成测试。注意：profile() 使用
独立 ThreadPoolExecutor，避免与 self._executor 嵌套提交导致的死锁。

---

## Sprint 2：CapabilityPolicy 路由统一 + news 多源去重

### G4 — CapabilityPolicy 元数据声明 routing 策略

**动机**：G1 之后 gateway 有三个并列原语
（`_sequential_fetch` / `_merged_fetch` / `_merged_history_fetch`），但选用
哪个原语 + 是否 ffill + 哪些字段当作"标识列"，全部硬编码在
`gw.quote()` / `gw.kline()` 等公开方法里。新增数据类型时要手动复制
这套样板，且不同方法路由参数不一致很难一眼看清全貌。

**范围**：
- `capabilities.py` 新增 `RoutingStrategy` 枚举（failover / merge_fields /
  merge_frames / merge_lists）+ `CapabilityPolicy(strategy, skip_fields,
  ffill)` + `ROUTING_POLICY: {(Capability, fn_name): CapabilityPolicy}`
- `DataGateway._route(cap, market, fn_name, *args, **kwargs)` 统一分派器，
  根据 ROUTING_POLICY 查表后调对应底层原语；统一返回 `(value, prov_dict)`
- 所有 gw.* 公开方法把直接调用 `_sequential_fetch` / `_merged_fetch` /
  `_merged_history_fetch` 改为 `_route(...)`
- `MERGE_LISTS` 在 G4 里只占位（raise NotImplementedError），G5 实现

**验收**：
- 路由表覆盖完整：每个 (cap, fn) 都有 policy；未登记 → KeyError
- 改 policy 即改路由：monkeypatch policy.strategy 后底层原语切换
- FAILOVER 也写 `{"_provider": name}` 到 _last_provenance（弥补 G2 缺口）
- 数据网关全套 335 passed（324 旧 + 11 新），无回归

✅ 已完成：commit 883f1dd（policy 元数据）+ 811f710（_route 分派器）+
522d844（gw.* 全部切换）+ 9e91b50（11 个测试）。

---

### G5 — news_headlines 多源归一去重 + 时间排序

**动机**：当前 NEWS_HEADLINES 只有 EastMoney 一源，且其接口是"全市场快讯"
（symbol 参数被忽略）。引入第 2/3 源（如 Sina/Tencent 财经资讯）后，
需要在 `_merged_list_fetch` 里：

- 标题归一化（去前后空白、全/半角统一、去常见前后缀如「【快讯】」）
- 按归一标题 dedupe
- 若 item 含时间戳按时间倒序；否则按 provider health 顺序

**范围**：
- 评估第 2/3 源（commit 1fc2537 前调研）：
  - ✅ AkShare 财联社电报 `stock_info_global_cls` 可用（含发布日期/时间）
  - ✗ AkShare `stock_news_em` 因 PyArrow regex bug 不可用
  - 最终方案：EM kuaixun + AkShare 财联社电报 2 源
- 新增 `schemas.NewsItem(title, timestamp, source, content)`，
  `base.fetch_news_headlines` 返回类型升级到 `List[NewsItem]`
- EM 重写：解析 showtime 字段写入 timestamp + source="eastmoney"
- AkShareProvider 新增 NEWS_HEADLINES capability + fetch 财联社电报
- gateway 加 `_merged_list_fetch`：并发拉所有候选源 → `_news_dedupe_key`
  归一化标题（去 "【...】"/"[...]" ≤12 字前缀、全角空格转半角、多空白折叠、
  末尾"。"/"."去除）→ 按 score 高的源先入 seen_keys → 有 ts 的按 ts 倒序、
  缺 ts 的按 source health 紧随
- `gw.news_headlines()` 公开签名 `(symbol, n) -> List[str]` 保留，
  内部投影 NewsItem.title
- ROUTING_POLICY 把 NEWS_HEADLINES 切到 MERGE_LISTS

**验收**：
- _news_dedupe_key 参数化覆盖各归一化规则
- 跨源同事件不同写法保留 score 高的那条
- ts 倒序 + 缺 ts 排末尾
- prov_dict 按"实际入选"条数计（去重失败的源不出现）
- gw.news_headlines 出口仍 List[str]
- 既有 EM news 测试全部不破

✅ 已完成：commit 3c473b7（G5-1 NewsItem + EM）+ 1fc2537（G5-2 AkShare
财联社）+ 7969fe4（G5-3 _merged_list_fetch + policy 切换）+ 43d5075
（G5-4 20 个直接单测）。数据网关 360 用例通过。

### Sprint 3：数据质量与可观测性
- G6: 字段级矛盾检测（divergence_pct 超阈值告警）
- G7: 在 schema 上暴露 completeness / confidence / stale_seconds
- G11: provenance 累计 metrics，接入 prometheus

### Sprint 4：运维 / 工程化
- G9: 配置驱动 provider 启用/禁用（`config/trading.yaml`）
- G16: 录制回放 provider（实盘录制 fixture，CI 重放）

### Backlog（低优先）
- G10: MemoryCache LRU 改 OrderedDict 实现（O(1)）
- G12: DataFrame schema 契约校验
- G13: 表驱动 Capability 注册
- G14: 跨 capability 字段拼接（用 Tencent.fundamentals 替代当前 hack）
- G15: batch RPC 接口
- G17: `gw.diagnose()` 结构化诊断

---

## 执行原则

- **小步提交**：每个 G* 独立 commit，msg 标注 "feat(data-layer): G* ..."
- **测试先行**：每个改动配单元测试，全套测试不回归
- **向后兼容**：现有 API 签名不变，新能力以新方法暴露
- **文档同步**：完成后更新 `docs/ARCHITECTURE.md` 的 Data Gateway 章节

---

# 系统级 Code Review 整改路线图

> 来源：2026-05-19 全项目代码 review（架构 / 错误处理 / 并发 / 风格 / 测试 / 配置 六维度）。
> 任务编号 R* 与上文 G*（数据层）平行，两条线并行推进。
> **优先级标记**：P0=实盘前必须修；P1=严重缺陷尽快修；P2=工程化债务持续偿还。
>
> 每个任务格式统一为「动机 + 范围 + 验收」，未完成的不打 ✅。

---

## Sprint R0：实盘安全闸（P0，最高优先级）

目标：在任何资金进入系统前，关闭可能导致**资金损失 / 成交不一致 / 数据被错值污染**的窗口。

### R0-1 — 订单提交幂等性 + 跨步骤事务

**动机**：`backend/api.py:471-559` 的 `/orders/submit` 把"风控检查 → broker 撮合 → 写持仓"
拆成三段，broker 内的 `_lock` 不跨服务边界；网络重试 / 用户双击 / 客户端超时重发都可能
让一个 BUY 被执行两次（多扣现金 + 多持仓）。

**范围**：
- API 层接收 `Idempotency-Key` header（客户端 UUID）；24h 内同 key 直接返回首单结果。
- `state.db` 新增 `order_idempotency(key TEXT PRIMARY KEY, order_id, response_json, created_at)`。
- `PortfolioService` 把"查现金 / 撮合 / 写持仓 / 写流水"四步包到一个事务（`BEGIN IMMEDIATE`），失败回滚。
- order_id 加 UNIQUE 索引，重复插入直接 IntegrityError → 4xx 返回。

**验收**：
- 新增 `tests/test_order_idempotency.py`：同 key 重复 POST，仅第一次扣款且 response 一致。
- 并发测试：10 线程用同 key 提交，仅 1 单成交。
- 现有 `test_oms_kelly_drawdown` / `test_paper_broker_thread_safety` 不回归。

---

### R0-2 — 抽 `core/singleton.py` 统一全局态 + 加锁

**动机**：~10 处模块级 `_global_*` 单例各自手写"lazy init + global 关键字 + reset"，
其中多数（`backend/api.py:_svc`、`core/oms.py:_orders/_positions`、`core/data_layer._global_layer`、
`core/regime._cache`、`core/llm_provider._factory`、`core/metrics._registry`、`core/alerting._global_alert_manager`、
`backend/api.py:_GLOBAL_RATE_LIMIT`）**完全无锁**。Flask WSGI 多线程下可创建出双实例 /
触发 `RuntimeError: dictionary changed size during iteration`。

**范围**：
- 新增 `core/singleton.py`：提供 `LockedSingleton[T]` 容器，封装双检锁 + reset + thread-local 选项。
- 迁移以下单例（每迁完一个独立 commit）：
  - `core/alerting.py:_global_alert_manager`
  - `core/data_layer.py:_global_layer`
  - `core/regime.py:_cache / _cache_date / _last_change_date / _persistent_regime`
  - `core/llm_provider.py:_factory`
  - `core/metrics.py:_registry`
  - `core/state_db.py:_MIGRATION_ATTEMPTED`（迁移要加锁外二次检查）
  - `core/data_gateway/http.py:_client`
  - `core/data_gateway/health.py:_tracker`
  - `backend/api.py:_svc / _RISK_ENGINE / _GLOBAL_RATE_LIMIT`
  - `core/oms.py:_orders / _positions`（值是 dict，要么换 `threading.RLock` 包，要么改 `concurrent.futures` safe map）
- `tests/conftest.py` 接入 `SingletonRegistry.reset_all()`，删除现有手写清理。

**验收**：
- 新增 `tests/test_singleton.py`：100 线程并发 `get()` 仅生成一个实例。
- 现有 `test_paper_broker_thread_safety` 不回归。
- `grep -rn "^\s*global " core/ backend/` 结果减半以上。

---

### R0-3 — SQLite 多线程使用与 WAL

**动机**：`backend/services/portfolio.py:49-80` 用 `check_same_thread=False` 跨线程共享同一
连接，与 SQLite WAL 并发模型冲突，高负载下 `database is locked` 或 WAL checkpoint 失败丢数据。

**范围**：
- `PortfolioService` 改为 thread-local connection（`threading.local()` 或 `contextvars`）。
- 进程启动时在第一个连接上统一执行：`PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA busy_timeout=5000;`。
- `state_db.py` 同步处理。
- 文档化"每线程一连接"的约定，新代码 lint 检查。

**验收**：
- 新增并发压测 `tests/test_portfolio_concurrent_writes.py`：50 线程交替 BUY/SELL，最终现金 + 持仓市值 = 初始资金（误差 < 0.01）。
- `journal_mode` 实际为 WAL（启动日志中打印）。

---

### R0-4 — 关键路径"失败即静默"全部上交

**动机**：review 找到 571 处 `except Exception`，其中**关键路径**有多个直接 `pass` 或仅
warning：
- `core/backtest_engine.py:512` 因子 signals 抛错被吞 → 当根 bar 信号缺失。
- `core/backtest_engine.py:446-452 / :659-660` ExitEngine / Kelly 抛错返回 `[]` / `0`。
- `core/risk_engine.py:480-481` 实时价拉取失败 → `price=0` 流转下游。
- `core/risk_engine.py:513-517` ATR/RSI 失败 → 强制取中性值继续止损判断。
- `core/strategy_runner.py:774 / 799 / 815` 持仓读取失败 → `[]`，再平衡被静默跳过。

**范围**：
- 每处改为：`except <具体类型> as e: logger.warning(...); self._failure_counter.inc()`。
- 在 `BacktestResult` / `StrategyHealth` / `RiskEngineReport` 上新增 `degraded_steps` 字段，
  > 0 时回测/实盘结束触发 WARNING 级告警。
- 引入 `core.errors.DataSourceError` / `FactorEvalError`，区分"数据缺失"和"逻辑 bug"。

**验收**：
- `tests/test_backtest_degraded.py`：mock 一个 always-raise 因子，回测完成但 `degraded_steps > 0` 且日志可见。
- `tests/test_risk_engine_no_price.py`：mock realtime 拉取失败，RiskEngine 不再返回 ATR=`price*0.015`，改为 raise / return None 让上层决策。

---

### R0-5 — 故障值不缓存（LLM / 基本面 / NLP）

**动机**：`backend/services/llm/cache.py:60-139` 用 `time.time()` TTL，把 LLM 故障期的
`{"sentiment": "neutral", "confidence": 0.0}` 当作正常结果缓存 24h；`core/fundamental_data.py:98-103`
异常 → 空 DataFrame → 因子静默降级为 0。系统恢复后仍读老缓存。

**范围**：
- 缓存层区分 success / fallback：fallback TTL = 0（不写盘）或 ≤60s。
- `core/factors/fundamental.py` 等"静默降级为 0"的因子改为：数据缺失 → 抛 `DataSourceError`；
  pipeline 在 catch 处理时**显式记录**该 symbol 该 bar 的因子缺失，写入 `FactorPipelineResult.missing`。
- 缓存键加 `trade_date` 维度，跨交易日自动失效。

**验收**：
- `tests/test_llm_cache_fallback.py`：模拟 LLM 抛错，第二次调用必须重新请求而非读缓存。
- `tests/test_factor_pipeline_missing.py`：basic data 缺失时 pipeline 不返回 0，而是在 `missing` 字段记录。

---

### R0-6 — 进程关停协调

**动机**：`backend/services/intraday_monitor.py` + `core/strategy_runner.py` + `core/async_runner.py`
缺少全局 shutdown 协调；systemd 重启 / Ctrl+C 时可能新任务还在 enqueue、worker 已开始退出，
订单孤立在内存队列里既不撤也不报。

**范围**：
- 新增 `core/lifecycle.py:Shutdown`：进程级 singleton，暴露 `request()` / `is_shutting_down` /
  `register_handler(callback)`。
- `intraday_monitor` / `async_runner` / scheduler / API server 在循环开头 `if Shutdown.is_shutting_down: break`，
  在新任务 enqueue 前 `if Shutdown.is_shutting_down: raise ShuttingDown`。
- 注册 SIGTERM / SIGINT handler → `Shutdown.request()` → 等待所有 worker join（timeout 30s）。
- 关停期间的 pending orders 持久化到 DB 状态 `CANCELLED_ON_SHUTDOWN` + 告警。

**验收**：
- `tests/test_graceful_shutdown.py`：启动 worker → 提交任务 → 发 SIGTERM → 在 timeout 内退出 +
  无 pending order 残留。

---

## Sprint R1：错误处理纪律（P1）

### R1-1 — 禁止裸 except / broad except + CI gate

**动机**：8 处裸 `except:`、571 处 `except Exception`，根因是没有 lint。

**范围**：
- 新增 `pyproject.toml` + `ruff` 配置，开启规则：`E722`（裸 except）、`BLE001`（broad except）、`B904`（raise from None）。
- 第一轮：所有裸 `except:` 必须换为 `except Exception`，并补 logger。
- 第二轮：把 `core/`、`backend/`、`quant_app/` 下的 `except Exception` 分批 triage：
  - 业务层（factors / use_cases / risk / oms）→ 收窄到具体异常类。
  - 边界层（HTTP fetch / file IO / parsing）→ 保留 `Exception` 但必须 logger + 计数器。
- 配置 pre-commit hook + GitHub Actions 跑 ruff，失败阻断 PR。

**验收**：
- `ruff check core/ backend/ quant_app/` 无 E722 / BLE001 告警。
- 任意 except 子句关联到 `logger.*` 调用（grep 验证）。

---

### R1-2 — 显式降级（fallback）信号化

**动机**：Black-Litterman 矩阵奇异 → `warnings.warn` + 返回 min_variance；
基本面缺失 → 因子返回 0；NLP 无 key → 返回零分。**调用方完全不知道**。

**范围**：
- `core/portfolio_optimizer.py:368-370` 等位置：返回值改为 dataclass `OptimizationResult(weights, is_fallback, fallback_reason)`。
- `core/factors/base.py:FactorResult` 增加 `quality` 字段（FULL / DEGRADED / MISSING）。
- `FactorPipeline` 输出汇总 quality，主流程可在 quality < threshold 时拒绝下单。

**验收**：
- `tests/test_portfolio_optimizer_fallback.py` 验证 BL 失败时 result.is_fallback=True。
- 现有 `test_factor_pipeline` 不回归。

---

## Sprint R2：分层清理（P1-P2）

### R2-1 — `/orders/submit` 业务逻辑下沉 use_cases

**动机**：`backend/api.py:473-559` 直接构造 Signal、调 RiskEngine、调 broker；
`_get_or_build_broker / _get_risk_engine` 在 endpoint 文件管理生命周期。违反 use_cases 设计契约。

**范围**：
- 新建 `core/use_cases/submit_order.py:submit_order(req: SubmitOrderRequest) -> SubmitOrderResponse`。
- API endpoint 退化为：参数解析 → 调用 use case → 序列化。
- broker / risk_engine 的获取改为通过 `core/dependencies.py`（DI 容器或 simple registry）。
- 同步整改 `/analysis/run` 等"endpoint 里组合两个 use case"的位置：要么合并到单一 use case，要么明确允许 endpoint 编排。

**验收**：
- 新 use case 至少 3 个测试（happy / risk_reject / broker_fail）。
- `backend/api.py` 不再 import `core.factors`、`core.risk_engine`、`core.brokers.*`。

---

### R2-2 — 删除 / 重启死代码与 shim

**动机**：维护负担。
- `core/brokers/ibkr.py` / `core/brokers/tiger.py` 全是 TODO 桩，已打 DeprecationWarning。
- `core/brokers/facade.py` 7.3K 但无调用点。
- `backend/main.py` 是 `quant_app/main.py` 的转发 shim。
- `backend/services/single_stock_analysis.py:1-35` 是 use_case 重构后的 shim。

**范围**：
- 决策树：
  - IBKR / Tiger：若 6 个月内不接，删除整个文件 + 文档说明仅支持 Futu / Paper；否则补一份"实现计划"独立 issue。
  - facade.py：grep 确认 0 引用后删除。
  - backend/main.py：把 systemd unit / 启动脚本 / README 引用全切到 `quant_app/main.py` 后删除。
  - single_stock_analysis.py shim：把所有调用方改 import use_case，再删除 shim。

**验收**：
- `git grep` 确认无引用。
- 测试套件不回归。

---

### R2-3 — 统一 scripts/quant 与 core 回测/WFA 入口

**动机**：`scripts/quant/backtest.py:105 + backtest_cli.py:1827` 独立重写了 BacktestEngine、
TechnicalIndicators、止损/仓位逻辑；`core/walkforward.py` / `core/research.py` / `scripts/quant/walkforward.py`
三套 WFA 并存。两套代码各自维护，bug 修复不同步。

**范围**：
- `scripts/quant/backtest_cli.py` 改为薄壳（CLI 参数 → 调 `core.use_cases.backtest`）。
- 删除 `scripts/quant/backtest.py` 中重复的 `BacktestEngine` 类，迁移到使用 `core.backtest_engine.BacktestEngine`。
- WFA：保留 `core/walkforward.py` 作为唯一实现，`core/research.py` 中的 WFA 逻辑迁出（research.py 只留 IC 分析 / regime 分析）。
- 验证 CLI 的产出（回测报告 / 指标）格式不变。

**验收**：
- `scripts/quant/backtest_cli.py` 行数 < 500。
- `python scripts/quant/backtest_cli.py --strategy macd_trend --symbol 600519.SH` 跑通且结果与 `core` 一致（diff 关键指标）。

---

### R2-4 — 巨型文件拆分

**动机**：4 个 1k+ 行文件难审查、难测、隐藏循环依赖。

**范围**：
- `backend/api.py` (1830 行) → 按资源拆 `backend/api/` 包：`orders.py` / `portfolio.py` / `analysis.py` / `health.py` / `auth.py`，每个 Flask Blueprint。
- `core/data_gateway/gateway.py` (1374 行) → `gateway.py`（路由）+ `manager.py`（生命周期 / 健康）+ `merge.py`（已有，复用）。
- `core/research.py` (1088 行) → `core/research/ic_analyzer.py` + `core/research/regime_analyzer.py` + `core/research/factor_researcher.py`。
- `scripts/quant/backtest_cli.py` 已在 R2-3 处理。

**验收**：
- 每个新文件 < 600 行。
- 公共 import 路径不变（通过 `__init__.py` re-export）。
- 全套测试不回归。

---

## Sprint R3：工程化基线（P2，持续）

### R3-1 — 工具链上线

**动机**：无 `pyproject.toml`、无 ruff / black / mypy / pre-commit，所有风格问题都靠人肉把关。

**范围**：
- 新增 `pyproject.toml`：
  - `[tool.ruff]`：E / F / W / B / BLE / E722 / I / UP / SIM 选集，line-length=100。
  - `[tool.black]`：line-length=100。
  - `[tool.mypy]`：strict 模式仅启用于 `core/use_cases/`, `core/factors/`, `core/brokers/`, `core/oms.py`, `core/risk_engine.py`；其他模块用 lax。
- `.pre-commit-config.yaml`：ruff + black + mypy（仅 staged 文件）。
- GitHub Actions：PR 强制跑 ruff / mypy；失败阻断。
- 第一次全量整改用 `ruff --fix` + `black .`，单独一个 commit "chore: apply ruff/black baseline"。

**验收**：
- 主分支 ruff / mypy 无告警。
- 新 PR 必须通过 pre-commit。

---

### R3-2 — 关键接口类型注解补全

**动机**：`core.factors.base.Factor` / `core.brokers.base.BrokerBase` / `core.use_cases.*` 是公共接口，
注解不全的话调用方完全失去 IDE 帮助。

**范围**：
- `Factor.evaluate(data) -> pd.Series`、`Factor.signals(fv, price) -> List[Signal]` 等返回类型补全。
- `BrokerBase` 全部方法 mypy strict 通过。
- 所有 `use_case` 函数签名 `(req: XxxRequest) -> XxxResponse` 强制（dataclass 化）。
- `core/use_cases/analyze_stock/_a_share.py:34-46` 那种"用 `getattr(req, k, None)` 裸取 dataclass"改为字段直访问。

**验收**：
- `mypy --strict core/factors/base.py core/brokers/base.py core/use_cases/` 无错。

---

### R3-3 — 测试质量整改

**动机**：~80 测试文件，但过度 mock、时间不冻结、断言抓不到关键不变量。

**范围**：
- 引入 `freezegun`，把所有 `datetime.now()` 敏感的测试用 `@freeze_time` 包裹（清单：扫 `tests/` 凡引用 `datetime.now/today` 的）。
- 关键集成测试改混合策略：
  - `tests/test_async_runner.py` — pipeline / data_layer 用真实小实例而非 MagicMock。
  - `tests/test_paper_broker_thread_safety.py` — 用真实 `PortfolioService`，断言并发后 cash + position 守恒。
  - `tests/test_broker_base.py` — 补 `assert fill.commission ≈ price × qty × commission_rate`、滑点落入 `[bps下界, bps上界]`。
- `tests/conftest.py` 的 session-scope DB 改为 function-scope；或为复用考虑，session-scope 但 function 级清空。
- 引入 `pytest-randomly`（或 `pytest --random-order`）暴露顺序依赖；修复暴露的问题。

**验收**：
- `pytest --random-order` 全套通过。
- `grep -rn "datetime.now\|datetime.today" tests/ | wc -l` 显著下降，剩余的都在 `freeze_time` 上下文中。

---

### R3-4 — 配置中心化

**动机**：佣金 / 滑点 / 印花税 / 策略阈值在 `config/trading.yaml` + `core/config.py` 默认值 + 各模块硬编码三处定义；
`os.environ.get` 散落 ~40 处，部分未文档化（`LLM_REVIEW_FAIL_OPEN` 等）；
`backend/services/report_sender.py:18` 启动期间删除所有含 `proxy` 的 env var 是隐蔽副作用。

**范围**：
- 新建 `core/config_defaults.py`：所有数值常量（commission_rate / slippage_bps / stamp_tax /
  max_positions / atr_stop_multiplier / pairs_trading.max_pvalue 等）集中定义。
- `core/config.py` 加载顺序明确：`defaults < YAML < env var`，且只在此处读 YAML / env。
- 新增 CLI `python -m core.config dump-effective` 打印生效配置（供运维审计）。
- 同步 `.env.example`：补 `LLM_REVIEW_FAIL_OPEN` / `NEWS_CACHE_TTL` 等所有代码里 grep 到的 env 名。
- 删除 `backend/services/report_sender.py:18` 的 proxy env 清理副作用，改为显式参数控制。

**验收**：
- `grep -rn "= 0.0003\|= 5\.0\b\|= 0\.001\b" core/ backend/` 命中数显著下降（魔数集中）。
- `python -m core.config dump-effective` 跑通。
- `.env.example` 与 `grep -rEho "os\.(environ\.get|getenv)\(['\"][^'\"]+['\"]" ` 结果对账无遗漏。

---

### R3-5 — 命名 / 风格统一（低优先长尾）

**动机**：`lookback / window / period / rolling_window` 4 个名字同义；
私有命名 `_x` / `__x` 用法不一致；中英文 docstring 混杂；f-string / `%` / `.format` 混用。

**范围**：
- 因子参数统一为 `lookback_bars`（保留旧名作 alias deprecation 期 6 个月）。
- 公共 API 不加下划线；模块内部用单下划线；不再用双下划线（除 dunder）。
- 用 `ruff format` + 手动整改把 `%` 和 `.format()` 全部替换为 f-string。
- docstring 语言策略：英文为主、中文注释允许；不在 docstring 写 TODO（改用 `# TODO(owner, date)` 行内注释）。

**验收**：
- 风格规则写入 `CONTRIBUTING.md`。
- ruff 规则覆盖（`UP032` no .format, `UP031` no % string format）。

---

## 任务优先级矩阵

| 任务 | 优先级 | 风险维度 | 预计工作量 |
|---|---|---|---|
| R0-1 订单幂等 + 事务 | P0 | 资金安全 | 3-5 天 |
| R0-2 singleton 统一 + 加锁 | P0 | 并发崩溃 / 状态分裂 | 3-5 天 |
| R0-3 SQLite WAL + thread-local | P0 | 数据丢失 | 2-3 天 |
| R0-4 关键路径静默吞错 | P0 | 决策错误 | 2-4 天 |
| R0-5 故障值不缓存 | P0 | 决策延续错误 | 1-2 天 |
| R0-6 优雅关停 | P0 | 订单孤儿 | 2-3 天 |
| R1-1 lint gate + except 整改 | P1 | 工程纪律 | 5-7 天 |
| R1-2 fallback 信号化 | P1 | 透明度 | 2-3 天 |
| R2-1 业务下沉 use_cases | P1 | 架构 | 3-5 天 |
| R2-2 删除死代码 | P2 | 维护负担 | 1-2 天 |
| R2-3 统一 scripts / core | P2 | 重复维护 | 3-5 天 |
| R2-4 巨型文件拆分 | P2 | 可读性 | 3-5 天 |
| R3-1 工具链上线 | P2 | 长期纪律 | 2-3 天 |
| R3-2 类型注解补全 | P2 | DX | 持续 |
| R3-3 测试整改 | P2 | 回归保护 | 持续 |
| R3-4 配置中心化 | P2 | 运维 | 2-3 天 |
| R3-5 风格统一 | P2 | 可读性 | 持续 |

**总工作量估算**：~50 人天（不含 R3-2 / R3-3 / R3-5 的持续投入）。

---

## 执行原则（沿用数据层 Sprint 约定）

- **P0 优先**：实盘前完成 R0 全部 6 项；P1 在 R0 完成后立即排期。
- **小步提交**：每个 R*-N 独立 commit，msg 标注 `fix(<domain>): R*-N ...`。
- **测试驱动**：每个 R 任务先补回归测试 → 再改实现；测试不通过不合并。
- **不破坏现有 API**：除非 R2-2 删除死代码，公共签名保持兼容；新能力以新方法 / 新参数暴露。
- **文档同步**：完成后更新 `docs/ARCHITECTURE.md` / `CONTRIBUTING.md` 对应章节。
