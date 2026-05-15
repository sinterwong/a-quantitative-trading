# TODO — 架构内聚重构(2026-05-15 启动)

> 评估基线 commit: master HEAD
> 分支:`refactor/architecture-cohesion`
> 目标:消除模块越权 + 重复实现 + 进程纠缠,建立清晰的层界

## 产品定位(已锁定,作为本次所有取舍的依据)

> **单租户准生产实盘 + 研究台,虚拟模拟盘(无真实券商),单 OS 单进程不可多开,
> 为未来微服务化(Docker)打基础,UI 保持 Streamlit。**

衍生约束:
- ❌ 不做多用户/多租户隔离
- ❌ 不接入真实券商(Futu/IBKR 实盘),保留 `SimulatedBroker` 即可
- ✅ 必须有 OS 级单实例锁,防误多开
- ✅ 代码结构允许"API + Worker"逻辑分离(但本次仍在一个进程跑)
- ✅ 单一 YAML 配置源 + `.env` 覆盖
- ✅ 用例(use case)层贯通 UI/API/Cron/CLI

---

## Phase 1 — 命名清晰化 + 死代码清理(P0,~1 周)

### P1-1 产品定位 + 模块地图写入仓库
- [x] `README.md` 顶部增加"定位 / 范围 / 不做什么"段落
- [x] 新建 `docs/ARCHITECTURE_TARGET.md`:目标 8 层架构图 + 3 个垂直切片图(为对照基准)
- [x] 旧的 `docs/ARCHITECTURE.md` 重命名为 `ARCHITECTURE_CURRENT.md`(诚实记录当下,不删)
- **验收**:任何新成员读完两份文档能说出"这是个什么系统"
- **commit**:`docs: 明确产品定位与目标架构基线`

### P1-2 审计 `scripts/quant/` 30+ 文件
- [x] 新建 `docs/audit/scripts_quant_audit.md`,逐文件标注:
  - 是否被外部脚本/backend 引用(`git grep`)
  - 是否被 `core/` 等价实现替代
  - 处置建议:KEEP / DELETE / MERGE_INTO_CORE / DEPRECATE
- [x] 重点验证以下高度疑似重复的文件:
  - `data_loader.py / data_provider.py`(已标 backtest-only,确认范围)
  - `regime_detector.py`(对照 `core/regime.py`)
  - `selection_pool.py / strategy_ensemble.py`(对照 `core/strategies/`)
  - `news_scorer.py`(对照 `core/factors/nlp.py`)
  - `signal_generator.py / regime_signal.py / regime_selector.py / combo_signal.py`
  - `position_sizer.py / trend_confirmed_rotation.py`
- **commit**:`docs(audit): scripts/quant 30 个文件去留盘点`

### P1-3 审计 `backend/services/` 17 个文件
- [x] 新建 `docs/audit/backend_services_audit.md`,逐文件标注职责 + 越权点
- [x] 重点关注:
  - `intraday_monitor.py` 1831 行 → 拆分预案
  - `signals.py` 999 行 → 与 `core/factor_pipeline.py` 关系
  - `single_stock_analysis.py` 865 行 → 提为 use case 的最佳样板
  - `portfolio.py` 直接 `qt.gtimg.cn`(合规债)
  - `report_sender.py` 直接 `qt.gtimg.cn`
- **commit**:`docs(audit): backend/services 17 个文件职责盘点`

### P1-4 第一批确认死代码删除
> 仅删除 P1-2/P1-3 中明确标注 `DELETE` 且无引用的文件;每批 ≤5 个文件,分批 commit
- [x] 删除批次 A:孤立无引用文件
- [x] 删除批次 B:被 `core/` 替代的副本
- [x] 每批跑全量 `pytest tests/ -q` 确认不回退
- **commit**(分批):`chore: 删除已审计的孤立/重复文件 (批次 N)`

### P1-5 虚拟券商定位明确化
- [x] `core/brokers/`:确认仅 `SimulatedBroker` 为 supported,其它(Futu 等)标 deprecated
- [x] `config/trading.yaml.example`:默认 broker=simulated
- [x] 文档说明"本系统不接入真实券商,只跑虚拟盘"
- **commit**:`feat(broker): 明确虚拟券商定位,标 Futu 等为 deprecated`

---

## Phase 2 — Use Case 层 + Backend 瘦身(P0,~3 周)

### P2-1 建立 use case 层骨架
- [x] 新建 `core/use_cases/` 包
- [x] 定义 `BaseUseCase`(可选)+ 通用约定(输入/输出 dataclass,异常)
- [x] 增加 `tests/test_use_cases/__init__.py`
- **commit**:`feat(use_cases): 建立 use case 层骨架`

### P2-2 use case 1:`analyze_stock`
- [x] 把 `backend/services/single_stock_analysis.py` 的 `analyze_a_share / analyze_hk_share` 搬入 `core/use_cases/analyze_stock.py`
- [x] `AnalysisRequest / AnalysisReport` dataclass 也移动
- [x] `backend/services/single_stock_analysis.py` 退化为 ≤30 行的 wrapper(import 转发)
- [x] `backend/api.py:analyze_a_stock_endpoint` 改为薄壳调用
- [x] 现有测试不回退
- **commit**:`refactor(use_case): 抽出 analyze_stock 用例,backend 退化为壳`

### P2-3 use case 2:`generate_intraday_signals`
- [x] 从 `backend/services/intraday_monitor.py` 抽出"信号生成"逻辑到 `core/use_cases/intraday_signals.py`
- [x] 输入:`Watchlist + RegimeInfo + PriceSnapshot`,输出:`List[Signal]`
- [x] intraday_monitor.py 改为编排:取数据 → 调 use case → 执行 + 告警
- [x] 单元测试覆盖 use case
- **commit**:`refactor(use_case): 抽出 intraday_signals,IntradayMonitor 瘦身 step1`

### P2-4 use case 3:`run_morning_workflow`
- [x] `scripts/morning_runner.py` 业务逻辑搬入 `core/use_cases/morning_workflow.py`
- [x] morning_runner.py 退化为 ≤50 行的 CLI 入口
- [x] use case 输出 `MorningReport` dataclass
- **commit**:`refactor(use_case): 抽出 morning_workflow,morning_runner.py 退化为 CLI 壳`

### P2-5 use case 4:`backtest`
- [x] 整合 `core/backtest_engine.py` + `scripts/quant/backtest.py` 等的入口
- [x] `core/use_cases/backtest.py` 提供统一 `run_backtest(BacktestRequest) → BacktestResult`
- [x] CLI 入口保留在 `scripts/quant/backtest_cli.py` 但只调 use case
- **commit**:`refactor(use_case): 抽出 backtest 用例`

### P2-6 use case 5:`compose_portfolio`
- [x] 整合 `core/portfolio_optimizer.py + portfolio_allocator.py` 为统一入口
- [x] use case 输入持仓现状 + universe + 风险参数,输出建议持仓比例
- [x] 不下单,只产出 PortfolioAdvice
- **commit**:`refactor(use_case): 抽出 compose_portfolio 用例`

### P2-7 拆分 `intraday_monitor.py`(1831 行)
- [x] 按职责拆分为 5 个 ≤400 行子模块:
  - `intraday/data.py` — 行情拉取(270 行)
  - `intraday/signaling.py` — 调 use case 生成信号(314 行)
  - `intraday/risk.py` — 风控过滤(395 行)
  - `intraday/execution.py` — 模拟下单(345 行)
  - `intraday/alerts.py` — 告警/记录(300 行)
- [x] 原 `intraday_monitor.py` 改为 ≤200 行的编排器(190 行,Mixin 组合)
- **commit**:`refactor(intraday): IntradayMonitor 拆分为 5 个职责模块`

### P2-8 backend api.py 端点瘦身
- [x] 批次 1 — positions / cash / trades(5 端点):`/portfolio/daily POST` 32→16
- [x] 批次 2 — orders(4 端点):`/orders/submit` 50→30(`main.get_broker()`)
- [x] 批次 3 — params(2 端点):抽 `services.signals.update_symbol_params` + `list_symbols_with_params`
- [x] 批次 4 — analysis(7 端点):新增 use case `daily_analysis` / `sector_rotation_signal` / `pairs_trading_signal` / `system_health`,各端点 ≤20 行
- [x] 收尾 — 新增 `risk_snapshot` / `performance_summary` use case + `MetricsRegistry.refresh_from_service()` 助手,`/market/status` 与 `/llm/analyze` 紧凑化
- 最终指标:54 个端点,平均 24.5 行,17 个超标(主要为大段 docstring)
- **commit**(分批 5 个):`refactor(api): {资源组} 端点退化为薄壳`

---

## Phase 3 — 进程模型 + 配置/状态收口(P1,~1 周)

### P3-1 OS 级单实例锁
- [x] 把现有 `core/strategy_runner.py` 的 PID lock 抽象成 `core/single_instance.py`
- [x] `backend/main.py` 启动时加 `acquire_singleton("quant-system")`
- [x] 已运行时抛 `SystemExit`,提示用户先停止已有实例
- [x] 测试:同时启动两次,第二次必失败
- **commit**:`feat(ops): 全局 OS 单实例锁,防误多开`

### P3-2 进程逻辑分离(API vs Worker)
> 注:仍跑在同一 Python 进程内,但代码上让两者解耦,未来可一行配置切到独立进程。
- [x] backend/main.py 拆分为:
  - `quant_app/serve_api.py`(44 行) — Flask + werkzeug make_server
  - `quant_app/run_worker.py`(582 行) — Scheduler + 交易日历 + IntradayMonitor 装配 + StrategyRunner 装配
  - `quant_app/main.py`(189 行) — 启动器,按 mode(all/api/worker)装配
- [x] 默认 `all`,backward-compat 别名 `both` → `all`、`scheduler` → `worker`
- [x] mode=`api` 跳过 Scheduler/Monitor/Runner,仅起 HTTP server;mode=`worker` 不开 API
- [x] backend/main.py 退化为 56 行 shim,转发所有符号保证既有 `from backend.main import …` 不破坏
- **commit**:`refactor(process): API 与 Worker 代码上解耦,默认仍合进程跑`

### P3-3 统一配置入口
- [x] 新建 `config/trading.yaml.example`(基于现有 trading.yaml 完整模板)
- [x] `core.config.warn_legacy_configs()` 启动时扫描 params.json / live_params.json / trading_mode.json,Output deprecation warning
- [x] quant_app/main.py 启动钩子调用该函数
- [x] .env 自动加载已在 quant_app/main.py(setdefault 模式,既有 env vars 优先)
- [ ] (延后) Pydantic 校验:现有 TradingConfig dataclass 已能用,Pydantic 迁移收益较低,暂不做
- [ ] (延后) 所有读配置点改用 `Settings.get()`:params.json 散落 30+ 处,本次仅加 deprecation,实际迁移在 P4-* 后续阶段
- **commit**:`feat(config): trading.yaml.example + 遗留 JSON deprecation warning (P3-3 阶段一)`

### P3-4 统一状态数据库
- [ ] 合并 `backend/services/portfolio.db` + `backend/wf_results.db` → `data/state.db`
- [ ] schema 版本表 + 迁移脚本
- [ ] 旧路径保留软链接 / fallback 读取一段时间
- [ ] 测试:迁移后所有读写测试通过
- **commit**:`feat(state): 统一 SQLite 状态库 + schema 版本管理`

---

## Phase 4 — UI 重构 + 端到端契约(P1,~2 周)

### P4-1 streamlit_app.py 拆为 pages/
- [ ] 1850 行单文件拆为:
  - `streamlit_app.py` — 入口 + 公共组件 ≤200 行
  - `pages/01_总览.py`
  - `pages/02_个股分析.py`
  - `pages/03_信号面板.py`
  - `pages/04_持仓与订单.py`
  - `pages/05_研究.py`
- [ ] 每个 page ≤400 行
- **commit**:`refactor(ui): Streamlit 拆为多页面结构`

### P4-2 UI 数据源全部走 backend API
- [ ] 移除 streamlit 内的 `qt.gtimg.cn` 直连
- [ ] 移除 streamlit 内的 `_fetch_news_eastmoney` 直接调用因子
- [ ] 全部改为调 `BACKEND_URL` 的对应端点
- [ ] 若 backend 缺端点则在 backend 加(走 use case 层)
- **commit**:`refactor(ui): UI 不再直连数据源,全部经 backend`

### P4-3 OpenAPI schema + 契约测试
- [ ] 用 `flask-smorest` 或 `apispec` 自动生成 OpenAPI 3.0 schema
- [ ] `backend/openapi.json` 由代码生成而非手维护
- [ ] 新增 `tests/test_api_contract.py`:每个端点 200/4xx 响应符合 schema
- **commit**:`feat(api): 自动生成 OpenAPI schema + 契约测试`

---

## Phase 5 — 收官 + 文档(P2,~3 天)

### P5-1 ARCHITECTURE_CURRENT.md 更新为最终态
- [ ] 把 ARCHITECTURE_TARGET.md 的图改写进 ARCHITECTURE_CURRENT.md
- [ ] 删除已完成对照文件
- **commit**:`docs: 架构文档收敛到最终态`

### P5-2 README 操作手册
- [ ] 启动方式(`all` / `api` / `worker`)
- [ ] 配置文件位置 + 优先级
- [ ] 常见运维操作(查日志 / 强制重置 / 备份 state.db)
- **commit**:`docs: README 操作手册`

### P5-3 全量回归 + 性能基线
- [ ] `pytest tests/ -q`(目标 ≥ 1425 passed)
- [ ] 启动时间基线(目标 < 5s)
- [ ] 内存占用基线
- **commit**:`chore: 架构重构收官,记录性能基线`

---

## 验证清单(每个 commit)

- [ ] 关联单元测试通过
- [ ] 全量测试不回退(`pytest tests/ -q`)
- [ ] commit message:`{type}({scope}): {what} + {why}`
- [ ] 若涉及 schema/API 变更,文档同步

---

## 不在本次范围

| 项 | 排除原因 |
|---|---|
| 真实券商接入(Futu/IBKR) | 产品定位明确不做 |
| 多用户/多租户 | 单租户 |
| Docker 化部署脚本 | 本次只打基础,真正容器化下个周期 |
| 前端框架升级(Next.js 等) | 维持 Streamlit |
| ML 模型架构升级 | 与本次重构正交 |
| 数据源新增 | 与本次重构正交 |
