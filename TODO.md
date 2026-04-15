# TODO — 开发任务

> 最后更新：2026-04-15
> 当前基线：回测框架完成，RSI WFA Sharpe=0.467，70分级别
> 目标：专业级个人量化系统 → 85分

---

## 📊 目标分解：从 70 → 85 分

| 维度 | 当前(70) | 目标(85) | 核心任务 |
|------|---------|---------|---------|
| 策略稳健性 | 单一 RSI，Sharpe=0.467 | 动态切换多策略，Sharpe≥0.8 | 市场环境识别 + 多策略组合 |
| 策略执行力 | 手动确认信号 | 全自动闭环，零干预 | 盘中自动化 + 实盘对接 |
| 参数适应性 | 固定参数 | 市场环境自适应 | WFA 定期自动更新参数 |
| 绩效分析 | 无 | 完整归因 + 周报 | 日志分析 + 归因报告 |
| 数据质量 | 东方财富为主 | 多源共振，更稳定 | 北向共振精细化 |

---

## P4 · 市场环境识别 + 多策略组合（核心突破）

> **目标**：Sharpe 从 0.467 提升至 ≥ 0.8
> **核心思路**：不同市场环境使用不同策略参数

### ✅ — 市场环境识别引擎
- **新建** `scripts/quant/regime_detector.py`
- 四种环境：
  - **BULL** — 上证站上 20日均线 AND 均线多头排列
  - **BEAR** — 上证跌破 20日均线 AND 均线空头排列
  - **VOLATILE** — ATR ratio > 0.85（高波动，均值回归失效）
  - **CALM** — ATR ratio ≤ 0.85 AND 趋势不明朗
- 每日缓存全天（`regime_today.json`）
- AkShare `stock_zh_index_daily` 获取上证指数数据
- CLI: `python regime_detector.py` 快速检测当前环境

### ✅ — 策略参数表（按环境）
- **Bull**: RSI(25/65) + ATR_threshold 0.90
- **Bear**: RSI(40/70) + ATR_threshold 0.80（更严格，避免抄底）
- **Volatile**: RSI(30/60) + ATR_threshold 0.80（保守）
- **Calm**: RSI(25/65) + ATR_threshold 0.85（标准）
- 参数表：REGIME_PARAMS 字典（regime_detector.py）

### ✅ — 多策略组合器
- **新建** `scripts/quant/strategy_ensemble.py`
- `StrategyEnsemble` 类：detect() / get_params() / evaluate()
- 根据当前环境返回对应参数 + 允许策略列表

### ✅ — WFA 验证
- `backtest_cli.py` 新增 `regime-wfa` 命令
- 对比固定 RSI(25/65) vs 环境自适应策略
- 注意：当前数据仅支持 1 个 WFA 窗口，需补充更多历史数据才能得出统计结论
- 结论框架已就绪，Sharpe ≥ 0.8 目标待更长数据验证
- 对比：单策略 RSI(25/65) vs 环境自适应策略
- 验收：Sharpe 提升 ≥ 30%，正收益窗口 ≥ 80%

---

## P5 · 全自动 Paper Trade 闭环（实盘对接第一阶段）

> **目标**：每日 9:00 启动后，完全零人工干预运行至收盘

### 🔲 — 早盘自动化（morning_runner.py 升级）
- 9:00 动态选股 → 输出 watchlist
- 9:05 对 watchlist 每只运行 `evaluate_signal()` → RSI_BUY 确认
- 9:06 对确认标的计算 Kelly 仓位 → 发出 BUY 市价单
- 9:10 同步日初净值 → Backend API

### 🔲 — 持仓追踪升级
- `intraday_monitor.py` — 持仓触发 WATCH_SELL 时记录信号原因
- 持仓触发止损/止盈时记录完整日志（触发价格、时间、信号强度）

### 🔲 — 收盘自动化（afternoon_report.py 升级）
- 15:00 触发：
  1. 查询所有今日持仓 → 计算浮动盈亏
  2. 查询所有今日成交 → 计算已实现盈亏
  3. 计算当日收益 → 记录 `daily_meta`
  4. 生成收盘晚报（持仓状态 + 全天信号回顾）

### 🔲 — 日志分析模块
- **新建** `scripts/quant/daily_journal.py`
- 字段：date / symbol / direction / entry_price / exit_price / shares / pnl / signal_reason / regime / slippage_bps
- 功能：
  - 统计各信号触发频率（RSI vs MACD vs BBANDS）
  - 统计各环境下胜率（Bull vs Bear vs Volatile）
  - 统计滑点分布（avg, p95）

---

## P6 · 绩效归因 + 参数自适应

### 🔲 — 绩效归因报告
- **新建** `scripts/quant/performance_report.py`
- 周度运行（每周一推送上周报告）：
  - 总收益 / 夏普 / 最大回撤
  - 盈利来源：RSI 信号贡献 vs MACD 信号贡献 vs BBANDS 信号贡献
  - 亏损分析：止损 vs 回撤熔断 vs 高波动屏蔽（是否正确）
  - 滑点总结（avg / max）
  - 行业集中度回顾
- 格式：飞书文本推送

### 🔲 — 参数自适应更新
- `walkforward.py` 每月第一个交易日运行
- 输出：`live_params.json` 自动更新 RSI 参数
- 保留人工审核步骤（更新前输出变更内容，人工确认后写入）
- 推送飞书通知：`参数更新：RSI(25/65) → RSI(30/68)，原因：WFA 10窗口平均 Sharpe 下降`

### 🔲 — 动态选股环境感知
- `dynamic_selector.py` — 大盘 20日均线状态传入选股
- Bull 市：偏重趋势动量（tech_score × 1.2）
- Bear 市：偏重防御（电力/医药/消费）× 1.2
- 减少在 Bear 市追高热门板块

---

## P7 · 风险管理精细化

### 🔲 — 单一标的仓位上限
- 当前：单笔 Kelly × 0.5
- 新增：单标的最高占总仓位 25%（防止单一标的黑天鹅）
- 写入 `broker.py` submit_order 检查

### 🔲 — ATR 移动止盈（Chandelier Exit）
- 现有 ATR 止损完善为：固定止损 → ATR 跟踪止盈
- 多头持仓：从入场高点减去 3×ATR 作为移动止损
- 每次收盘更新

### 🔲 — 北向共振精细化
- 现有：北向净流入 > 50亿 → 信号强化
- 升级：区分北向"持续流入"（连续3日）vs "单日脉冲"
- 单日脉冲：信号强化 +1；持续流入：信号强化 +2
- 新增 `backend/services/northbound.py` — `get_north_flow_direction(symbol)` → 3日趋势

---

## P8 · 数据源稳定化（持续性工程）

### 🔲 — 北向资金接口多源备份
- 东方财富 KAMT 限流时 → 切换到 Sina 财经北向数据

### 🔲 — 日内数据缓存优化
- 分钟 K 线数据（腾讯接口）缓存 1 分钟
- 避免同一分钟重复请求造成限流

---

## 已完成 → 70 分基线

| 任务 | 优先级 | 文件 |
|------|--------|------|
| RSI WFA 参数验证 | P0 | `backtest_cli.py` |
| ATR ratio + RSI BUY 屏蔽 | P0 | `signals.py` |
| Kelly 仓位管理 | P1 | `scripts/quant/position_sizer.py` |
| 组合熔断（8%/12%）| P1 | `intraday_monitor.py` |
| 基本面 PE/PB 过滤 | P1 | `services/fundamentals.py` |
| 分钟 RSI 二次确认 | P1 | `intraday_monitor.py` |
| 涨跌停熔断（position-aware）| P2 | `signals.py` |
| 行业集中度检查 | P2 | `portfolio.py` + `sector_map.json` |
| 滑点监控 | P2 | `broker.py` + `portfolio.py` |
| 压力测试（crash-test）| P2 | `backtest_cli.py` |
| MACD 策略验证 | P2 | `backtest_cli.py` |
| 布林带策略验证 | P2 | `backtest_cli.py` |
| 新闻情绪打分 | P2 | `news_scorer.py` |
| 北向共振信号 | P2 | `signals.py` |
| 早报生成模块 | P2 | `morning_report.py` |
| 东方财富多源 fallback | P2 | `dynamic_selector.py`（Sina 备用）|
| 飞书推送安全处理 | 安全 | `intraday_monitor.py` |
| Credentials 移除 | 安全 | `.env` 外置 |

---

## 开发顺序（建议）

```
1. P7（数据稳定）→ 基础，确保其他功能不被限流打断
2. P4（环境识别）→ 核心突破，Sharpe 提升的关键
3. P5（全自动闭环）→ 让系统真正跑起来，产生可用日志
4. P6（绩效归因）→ 基于日志验证 P4 效果
5. P8（北向精细化）→ 锦上添花
```

---

> 核心原则：每完成一个 P 块，推送一次并更新 TODO.md。不追求一次性全部完成，追求每次完成都有可测试的产出。
