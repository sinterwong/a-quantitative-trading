# 路线图

## 数据层

### 字段级矛盾检测

`core/data_gateway/gateway.py::DataGateway._merged_fetch` 和 `_merged_history_fetch` 完成字段级合并后，对同字段多源出现冲突的情况计算差异：

- 数值字段：`abs(v1 - v2) / max(abs(v1), abs(v2), eps)` 作为 `divergence_pct`。
- 阈值通过环境变量 `TRADING_DIVERGENCE_THRESHOLD` 配置（默认 `0.05`）。
- 超阈值写 WARNING 日志（含 `symbol / field / providers / values`），同时将 `divergence_pct` 写入 `self._last_provenance[cache_key]`，键名 `<field>__divergence`。

验收：`tests/test_data_gateway/test_divergence.py` 新增用例，mock 两个 provider 对同一 capability 返回差异值（一个超阈值、一个不超），验证日志与 provenance 行为。

---

### schema 完整性元数据

`core/data_gateway/schemas.py` 中：

- `StockProfile` 增加 `missing_capabilities: List[str] = field(default_factory=list)`，由 `core/data_gateway/profile.py::build_profile` 在某 capability 抛异常或返回空时追加 capability 名称。
- `Fundamentals` 和 `BalanceSheet` 增加 `stale_seconds: int = 0`，由 `gateway.py` 在缓存命中分支根据 `cache.peek_age(key)` 写入。
- `Quote` 增加 `confidence: float = 1.0`，由 `merge_field_level` 在 MERGE_FIELDS 分支根据贡献源的 `HealthTracker.score()` 平均值给出。

验收：`tests/test_data_gateway/test_schemas_metadata.py` 三个用例分别覆盖 `missing_capabilities`、`stale_seconds`、`confidence` 字段。

---

### provider 调用指标接入 Prometheus

`core/metrics.py::MetricsRegistry` 增加两个带 label 的指标：

- `data_gateway_provider_requests_total{provider, capability, status}` Counter，status ∈ `{ok, error, timeout, circuit_open}`。
- `data_gateway_provider_latency_seconds{provider, capability}` Histogram，bucket 用默认。

`core/data_gateway/health.py::HealthTracker.record_event` 在写 `_Event` 之后调用 `get_registry().observe_provider(...)`，把 provider、capability、status、latency 一并上报。

验收：`curl localhost:5555/metrics | grep data_gateway_provider` 至少返回 `_total` 与 `_latency_seconds_bucket` 两行，且 label 含有真实 provider 名称。

---

## 系统层

### 回测入口收敛到 core/use_cases

现状：`scripts/quant/backtest.py` (499 行) 内置一个独立 `BacktestEngine`，`scripts/quant/backtest_cli.py` (1831 行) 在 CLI 层重复实现了大量回测/报告逻辑；`scripts/quant/walkforward.py` (189 行) 与 `core/walkforward.py` (472 行) 功能重叠。

步骤：

1. 把 `scripts/quant/backtest.py::BacktestEngine` 迁移合并到 `core/use_cases/backtest.py`（或确认已被覆盖后删除原文件）。
2. `scripts/quant/backtest_cli.py` 改为薄壳：参数解析 → 构造 `BacktestRequest` → 调 `core.use_cases.backtest.run(...)` → 打印 / 落盘。文件目标 < 200 行。
3. `scripts/quant/walkforward.py` 删除或改为薄壳，统一走 `core/walkforward.py`。

验收：`python scripts/quant/backtest_cli.py --symbol 600519.SH --start 20240101 --end 20240601` 跑通，关键指标（CAGR / Sharpe / MaxDD / 交易笔数）与直接调 `core.use_cases.backtest.run(...)` 完全一致；`grep -rn "BacktestEngine" scripts/` 只剩薄壳引用。

---

### 4 个核心模块纳入 mypy strict

`pyproject.toml` 已有 35 个 strict 文件清单。下列文件加入并修复：

| 文件 | 当前 strict 报错 | 主要问题 |
|---|---|---|
| `core/oms.py` (762 行) | ~20 | 函数签名缺注解、`dict[type-arg]` 泛型缺失 |
| `core/risk_engine.py` (563 行) | ~34 | 同上 + 模块级 dict 字面量缺类型 |
| `core/use_cases/compose_portfolio.py` (213 行) | ~6 | 含真实 bug，需结合测试一起修 |
| `core/use_cases/pairs_trading_signal.py` (115 行) | ~8 | `core/use_cases/pairs_trading_signal.py:89` 调 `strat.latest_signal(...)`，需核对该方法实际是否存在并补齐 |

完成顺序建议：`compose_portfolio.py → pairs_trading_signal.py → oms.py → risk_engine.py`，先小后大。

验收：`mypy --strict` 在 `pyproject.toml::[[tool.mypy.overrides]]` 列出的全部 strict 文件通过；`pytest tests/test_use_cases tests/test_oms tests/test_risk_engine` 不回归。
