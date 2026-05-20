# scripts

运维 / 研究脚本。单元测试在 `tests/`。

## scripts/ 顶层

### 定时任务（由 Scheduler 触发，见 backend/README.md）

| 文件 | 说明 |
|---|---|
| `morning_runner.py` | 09:30 盘前流程入口（选股 + 信号 + 早报） |
| `morning_report.py` | 盘前市场分析报告 |
| `afternoon_report.py` | 15:00 收盘晚报（持仓快照 + 收益） |
| `daily_risk_report.py` | 15:30 CVaR + 蒙特卡洛压力测试 |
| `daily_tca.py` | 15:45 TCA 反馈闭环 |
| `walkforward_job.py` | Walk-Forward 参数验证定时任务 |
| `regime_wfa.py` | Regime 感知的 WFA 分析 |
| `nlp_batch_score.py` | 新闻情感批量打分 |
| `ml_train_all.py` | 全量 ML 模型训练 |
| `sensitivity_job.py` | 敏感性分析定时任务 |

### 研究 / 工具

| 文件 | 说明 |
|---|---|
| `dynamic_selector.py` | 动态选股器（`DynamicStockSelector`，日终 15:10 跑） |
| `bayesian_optimize.py` | 贝叶斯参数优化 |
| `run_morning_report.py` | 盘前报告手动入口 |
| `generate_openapi.py` | 从 `backend/` 包自动生成 `backend/openapi.json`，支持 `--check` |

## scripts/quant/

回测 / 信号 / 因子研究脚本。部分文件是早期实现，已被 `core/` 替代但保留兼容性，新代码不要往这里加。

### 仍在使用

| 文件 | 用途 |
|---|---|
| `backtest_cli.py` | 主回测 CLI，接 `core/use_cases/backtest.py` |
| `backtest.py` | 提供 `RSISignalFunc / MACDSignalFunc` 给 `intraday_signals.py` |
| `walkforward.py` | 早期 WFA，被 `walkforward_job.py` 与 `regime_wfa.py` 依赖 |
| `signal_generator.py` | 老信号层，被 WFA 和测试依赖 |
| `data_loader.py` | 回测专用 OHLCV 加载 |
| `benchmark.py` | `quick_benchmark` 基准对比 |
| `monte_carlo.py` | `MonteCarloSimulator` |
| `position_sizer.py` | `compute_kelly_from_trades`，被 IntradayMonitor 引用 |
| `news_quality.py` / `news_scorer.py` | 新闻打分，被 `dynamic_selector.py` 引用 |
| `regime_detector.py` | 提供 `get_cached_regime / get_params_for_regime` |
| `config_stock_pool.py` | 标的池配置 |
| `atr_sweep.py` / `atr_threshold_scan.py` / `atr_wfa_scan.py` | ATR 因子扫描 |
| `llm_connect_test.py` | LLM 连通性烟测 |

### 已 deprecated（代码保留，下个清理周期删）

`combo_signal.py` / `daily_journal.py` / `daily_reporter.py` /
`data_provider.py` / `institutional_live.py` / `intraday_signals.py` /
`performance_report.py` / `regime_selector.py` / `regime_signal.py` /
`selection_pool.py` / `trend_confirmed_rotation.py`

`scripts/quant/strategies/` 下的 `institutional.py` / `mean_reversion.py` /
`momentum.py` / `sector_rotation.py` 同样已被 `core/strategies/` 替代。

## 调用示例

```bash
# 手动盘前报告
python scripts/run_morning_report.py

# Walk-Forward 全周期回测
python scripts/walkforward_job.py --start 20200101 --end 20251231

# 贝叶斯参数优化
python scripts/bayesian_optimize.py --n-trials 100

# 主回测 CLI
python scripts/quant/backtest_cli.py --symbol 600519.SH --start 20240101
```

## 备注

- 调试脚本依赖外网，不进 CI
- `scripts/quant/__init__.py` 让本目录是包，统一用 `from scripts.quant.xxx` import
