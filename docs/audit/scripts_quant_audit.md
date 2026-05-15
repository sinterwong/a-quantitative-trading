# 审计:`scripts/quant/` 模块去留盘点

> 评估日期:2026-05-15 · 分支:`refactor/architecture-cohesion`
> 方法:`git grep` 引用关系 + 是否 `__main__` + 是否有 `core/` 等价实现

## 处置约定

- **KEEP**:仍被生产代码引用,且无更好的迁移路径,本次保留
- **MERGE_INTO_CORE**:有 `core/` 等价实现,后续把外部 caller 切到 core/,本文件待删
- **DEPRECATE**:无引用 / 已废弃,但本次不立即删(可能有外部 cron 调用),先标记 + warning
- **DELETE_NOW**:0 引用 + 无 `__main__` + 无 docs 提及,可立即删除

---

## 30 个文件清单

| 文件 | 行数(bytes) | __main__ | 外部引用 | 处置 | 备注 |
|---|---|---|---|---|---|
| `__init__.py` | 24 | N | — | KEEP | 包标识 |
| `atr_sweep.py` | 10k | Y | 0 | KEEP | CLI 研究脚本(ATR 参数扫描),CI py_compile 检查存在 |
| `atr_threshold_scan.py` | 5k | Y | 0 | KEEP | CLI 研究脚本 |
| `atr_wfa_scan.py` | 11k | Y | 0 | KEEP | CLI 研究脚本 |
| `backtest.py` | 20k | N | `intraday_signals.py` | KEEP | 早期 backtest,提供 `RSISignalFunc / MACDSignalFunc` 等 |
| `backtest_cli.py` | 72k | Y | 0 | KEEP | 主回测 CLI(README 提及) |
| `benchmark.py` | 7k | N | `walkforward_job.py` | KEEP | `quick_benchmark` 提供基准对比 |
| `combo_signal.py` | 9k | Y | 0 | **DEPRECATE** | CLI 但无人调,功能与 `core/factor_pipeline` 重叠 |
| `config_stock_pool.py` | 8k | Y | `intraday_signals.py`(内部) | KEEP | 提供 `get_portfolio / get_strategy_config` |
| `daily_journal.py` | 10k | Y | 0 | **DEPRECATE** | 已被 backend 日报机制覆盖 |
| `daily_reporter.py` | 13k | Y | 0 | **DEPRECATE** | 同上,backend/services/daily_ops_reporter 覆盖 |
| `data_loader.py` | 15k | N | `walkforward_job.py / regime_wfa.py / atr_sweep.py` | KEEP | 回测专用 OHLCV 加载,需保留至 backtest use case 重构后 |
| `data_provider.py` | 11k | Y | 0 | **DEPRECATE** | 实时行情用 Gateway,本文件历史功能已被替代 |
| `institutional_live.py` | 11k | Y | 0 | **DEPRECATE** | 实盘机构策略 CLI,与"虚拟模拟盘"定位冲突 |
| `intraday_signals.py` | 7k | Y | 0 | **DEPRECATE** | 与 `backend/services/intraday_monitor` 重复,后者是生产路径 |
| `llm_connect_test.py` | 5k | Y | 0 | KEEP | LLM 连通性烟测,运维偶用 |
| `monte_carlo.py` | 12k | N | `walkforward_job.py` | KEEP | `MonteCarloSimulator` |
| `news_quality.py` | 8k | N | `dynamic_selector.py` | **MERGE_INTO_CORE** | 应合入 `core/factors/nlp.py` 或新 `core/news/` |
| `news_scorer.py` | 16k | N | `dynamic_selector.py / morning_report.py` | **MERGE_INTO_CORE** | 与 `core/factors/nlp.py` 功能重叠 |
| `performance_report.py` | 21k | Y | 0 | **DEPRECATE** | `backend/services/performance.py` 已覆盖 |
| `position_sizer.py` | 4k | N | `intraday_monitor.py`(`compute_kelly_from_trades`) | KEEP | 单函数被 backend 引用 |
| `regime_detector.py` | 10k | Y | `morning_runner.py`(`get_cached_regime / get_params_for_regime`) | **MERGE_INTO_CORE** | 应合入 `core/regime.py`(`get_regime` 已是主入口) |
| `regime_selector.py` | 6k | Y | 0 | **DEPRECATE** | 研究脚本,无生产引用 |
| `regime_signal.py` | 10k | Y | 0 | **DEPRECATE** | 研究脚本 |
| `selection_pool.py` | 4k | Y | 0 | **DEPRECATE** | 与 `scripts/dynamic_selector.py` 功能重叠 |
| `signal_generator.py` | 26k | N | `walkforward_job.py / tests/test_signal_generator.py / tests/run_tests.py` | KEEP | 老的信号生成层,被 WFA 和测试依赖,本次保留 |
| `strategy_ensemble.py` | 8k | **N** | 0 | **DELETE_NOW** | 0 引用 + 无 main,纯死代码 |
| `trend_confirmed_rotation.py` | 13k | Y | 0 | **DEPRECATE** | 研究脚本,与 `core/strategies/sector_rotation.py` 重叠 |
| `walkforward.py` | 7k | N | `walkforward_job.py / regime_wfa.py` | KEEP | 早期 WFA,与 `core/walkforward.py` 平行(后续 MERGE) |

### 子目录 `scripts/quant/strategies/`

| 文件 | 行数 | 引用 | 处置 |
|---|---|---|---|
| `__init__.py` | — | — | KEEP |
| `institutional.py` | — | 0 | **DEPRECATE** |
| `mean_reversion.py` | — | 0 | **DEPRECATE** |
| `momentum.py` | — | 0 | **DEPRECATE** |
| `sector_rotation.py` | — | 0 | **DEPRECATE**(与 `core/strategies/sector_rotation.py` 重叠) |

---

## 处置统计

| 处置 | 数量 |
|---|---|
| KEEP | 14 |
| MERGE_INTO_CORE | 3 (news_quality, news_scorer, regime_detector) |
| DEPRECATE | 12 (本次不删,只标记 + 文档说明,下个周期清理) |
| **DELETE_NOW** | 1 (strategy_ensemble.py) |

---

## 第一批可立即删除清单(0 风险)

1. `scripts/quant/strategy_ensemble.py` — 0 引用 + 无 `__main__` + docs 未提

---

## 后续动作

1. **本次(P1-4)**:仅删 `strategy_ensemble.py`
2. **下个周期**:
   - 把 `news_quality / news_scorer` 合并到 `core/factors/nlp.py` 或新 `core/news/` 子包
   - 把 `regime_detector` 的 `get_cached_regime / get_params_for_regime` 合并到 `core/regime.py`
   - 然后让 `morning_runner / dynamic_selector / morning_report` 改 import 路径
   - 完成后删除 `MERGE_INTO_CORE` 三个文件
3. **再下个周期**:批量删 `DEPRECATE` 12 个文件(届时给一段 deprecation warning 后清理)

---

## 顺手发现

1. 仓库根目录 `strategies/` 是又一个独立的"策略目录"(BollingerBandStrategy 等),
   与 `core/strategies/` 和 `scripts/quant/strategies/` **三处并存**,后续单独审计
2. `scripts/quant/__init__.py` 让本目录成为 `quant` 包,但跨平台导入路径混乱
   (有 `from quant.xxx` / `from scripts.quant.xxx` / `from xxx` 三种),
   未来切到包形态后统一为 `scripts.quant.xxx`
3. `scripts/README.md` 信息陈旧:仍提及已删除的 `stock_data_only.py / daily_engine.py`
   需要在 P5-2(操作手册)时同步更新
