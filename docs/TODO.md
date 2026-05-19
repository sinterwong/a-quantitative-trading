# 路线图

## 数据层

### 字段级矛盾检测

在 `_merged_fetch` / `_merged_history_fetch` 合并完成后，计算同字段多源差异率。`divergence_pct` 超过阈值时写 WARNING 日志并写入 provenance，阈值可通过 `TRADING_DIVERGENCE_THRESHOLD` 配置。

验收：`tests/test_data_gateway/test_divergence.py` 覆盖同字段多源差异场景。

---

### schema 完整性元数据

`StockProfile` 增加 `missing_capabilities: List[str]` 字段，列出本次返回 None 的 capability 名称。`Fundamentals` / `BalanceSheet` 等 dataclass 增加 `stale_seconds: int` 元数据，`Quote` 增加 `confidence: float` 字段。

验收：mock 部分 capability 失败场景，验证 `missing_capabilities` 包含正确的 capability 名称。

---

### provider provenance 指标接入 Prometheus

`HealthTracker` 在每次 provider 请求完成后，递增 `data_gateway_provider_requests_total{provider, capability, status}` 计数器和 `data_gateway_provider_latency_seconds{provider, capability}` 直方图，在 `/metrics` 端点暴露。

验收：`curl localhost:5555/metrics | grep data_gateway_provider` 返回有效指标。

---

## 系统层

### 统一 scripts/quant 与 core 回测入口

`scripts/quant/backtest_cli.py` 改为纯薄壳，调 `core.use_cases.backtest`。删除 `scripts/quant/backtest.py` 中重复的 BacktestEngine 类。WFA 以 `core/walkforward.py` 为唯一实现；评估 `scripts/quant/walkforward.py` 是否可简化为薄封装。

验收：`python scripts/quant/backtest_cli.py --symbol 600519.SH --start 20240101` 跑通且关键指标与直接调 `core.use_cases.backtest` 一致。

---

### oms / risk_engine / compose_portfolio / pairs_trading mypy strict

35 个核心文件已纳入 mypy strict，仍需纳入：

- `core/oms.py`（~20 错，长文件分块补类型注解）
- `core/risk_engine.py`（~34 错，`dict[type-arg]` + 函数注解）
- `core/use_cases/compose_portfolio.py`（~6 错，含真实 bug 修复）
- `core/use_cases/pairs_trading_signal.py`（~8 错，含 `latest_signal` 不存在 bug）

验收：`mypy --strict` 全部 strict 文件通过。

---

### pytest-randomly 稳定化

pytest-randomly 已引入并暴露 4 个跨测试状态泄漏，已修复。仍有部分测试在 `pytest --randomly-seed=99999` 下偶发失败，需逐个修复。

验收：`pytest --randomly-seed=99999` 全套通过。
