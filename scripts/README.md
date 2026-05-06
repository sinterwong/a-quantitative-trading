# scripts/ 目录

运营脚本和调试工具目录，单元测试统一在 `tests/`。

---

## 运营脚本

### 定时任务

| 文件 | 说明 |
|------|------|
| `morning_runner.py` | 每日盘前流程：获取信号 + 生成报告 |
| `morning_report.py` | 盘前市场分析报告 |
| `afternoon_report.py` | 盘后绩效归因报告 |
| `run_morning_report.py` | 盘前报告 CLI 入口 |
| `walkforward_job.py` | Walk-Forward 参数验证定时任务 |
| `regime_wfa.py` | Regime 感知的 WFA 分析 |
| `ipo_scanner.py` | 港股打新扫描（每日 09:00，feature/ipo-stars 分支） |

### 研究与调试

| 文件 | 说明 |
|------|------|
| `dynamic_selector.py` | 动态标的筛选器 |
| `stock_data_only.py` | 仅获取行情数据（不生成信号） |
| `bayesian_optimize.py` | 贝叶斯参数优化（配合 Walk-Forward） |

---

## quant/ 子目录

量化研究脚本。

| 文件/目录 | 说明 |
|-----------|------|
| `backtest.py` / `backtest_cli.py` | 回测 CLI 工具 |
| `daily_engine.py` | 日内实时信号引擎 |
| `daily_reporter.py` / `daily_journal.py` | 每日报告生成 |
| `data_loader.py` / `data_provider.py` | 数据获取适配器 |
| `walkforward.py` | Walk-Forward 验证（早期版本） |
| `regime_detector.py` / `regime_selector.py` | 市场状态检测研究 |
| `monte_carlo.py` | 蒙特卡洛模拟 |
| `strategies/` | 早期策略实现（已迁移至 `core/strategies/`） |
| `atr_wfa_scan.py` | ATR 因子 WFA 扫描 |

---

## 运行方式

```bash
# 盘前报告
python scripts/run_morning_report.py

# Walk-Forward 验证
python scripts/walkforward_job.py --start 20200101 --end 20251231

# 贝叶斯参数优化
python scripts/bayesian_optimize.py --n-trials 100

# 港股打新扫描（需在 feature/ipo-stars 分支）
python -m scheduler.ipo_scanner
```

---

## 注意

- 单元测试统一在 `tests/`，使用 `pytest` 或 `python tests/run_tests.py`
- `scripts/quant/` 中的部分脚本依赖较早接口，功能已被 `core/` 模块替代
- 调试脚本需要网络连接，不作为 CI 测试
