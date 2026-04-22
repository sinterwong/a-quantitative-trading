# scripts/ 目录说明

本目录存放**运营脚本**和**调试工具**，不包含单元测试（测试文件位于 `tests/`）。

## 顶层脚本

| 文件 | 用途 |
|------|------|
| `morning_runner.py` | 每日盘前自动运行：获取信号、发送报告 |
| `morning_report.py` | 生成每日盘前分析报告 |
| `afternoon_report.py` | 生成每日盘后绩效报告 |
| `run_morning_report.py` | 盘前报告 CLI 入口 |
| `dynamic_selector.py` | 动态标的筛选器 |
| `stock_data_only.py` | 仅获取行情数据（不生成信号） |
| `walkforward_job.py` | 定时 Walk-Forward 验证任务 |
| `regime_wfa.py` | Regime 感知的 WFA 分析 |
| `test_em_l2_depth.py` | 调试：东方财富 Level2 盘口接口探测 |
| `test_level2.py` | 调试：Level2 数据源格式验证 |

## scripts/quant/ 子目录

存放量化研究脚本，包含：

| 文件/模块 | 用途 |
|-----------|------|
| `backtest.py` / `backtest_cli.py` | 回测 CLI 工具 |
| `daily_engine.py` | 日内实时信号引擎 |
| `daily_reporter.py` / `daily_journal.py` | 每日报告生成 |
| `data_loader.py` / `data_provider.py` | 数据获取适配器 |
| `walkforward.py` | Walk-Forward 验证脚本（早期版本，新版见 `core/walkforward.py`） |
| `regime_detector.py` / `regime_selector.py` | 市场状态检测研究 |
| `monte_carlo.py` | 蒙特卡洛模拟 |
| `strategies/` | 早期策略实现（已迁移至 `core/strategies/`） |

## 注意

- **单元测试**统一在 `tests/` 目录，用 `pytest` 或 `python tests/run_tests.py` 运行
- `scripts/quant/` 中的脚本依赖较早的接口，部分功能已被 `core/` 替代
- 调试脚本（`test_em_l2_depth.py`, `test_level2.py`）需要网络连接，不作为 CI 测试
