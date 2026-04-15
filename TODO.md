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

### ✅ — 早盘自动化（morning_runner.py 升级）
- 9:00 动态选股 → 输出 watchlist
- 9:05 对 watchlist 每只运行 `evaluate_signal()` → RSI_BUY 确认（环境感知参数）
- 9:06 分钟级 RSI 二次确认 → Kelly 仓位（市价单）
- 9:10 同步日初净值 → Backend API
- `evaluate_watchlist_and_submit()`: 全流程自动化
- `log_opening_state()`: 记录完整开盘状态到 daily_meta notes

### ✅ — 持仓追踪升级
- `_check_stop_losses` / `_check_take_profits` 已完整实现
- WATCH_SELL 触发飞书预警（包含信号原因）
- 止损/止盈成交后自动记录 trade + signal

### ✅ — 收盘自动化
- **新建** `scripts/afternoon_report.py`
- 15:00 触发：持仓快照 → 浮动/已实现盈亏 → daily_meta → 飞书晚报
- `build_closing_report()`: 生成收盘晚报（持仓+成交+信号回顾）

### ✅ — 日志分析模块
- **新建** `scripts/quant/daily_journal.py`
- 字段：date / symbol / direction / entry_price / exit_price / shares / pnl / signal_reason / regime / slippage_bps
- `analyze_journal()`: 统计各信号胜率 / 各环境胜率 / 滑点分布（avg, p95）
- `format_journal_summary()`: 生成可读文本报告

---

## P6 · 绩效归因 + 参数自适应

### ✅ — 绩效归因报告
- **新建** `scripts/quant/performance_report.py`
- 周度运行（每周一推送上周报告）：
  - 总收益 / 年化收益 / 夏普比率 / 最大回撤（日线数据计算）
  - 盈利来源：按信号类型（RSI/MACD/BBANDS）统计胜率/均值
  - 亏损分析：轻度/中度/重度亏损分层 + 最大亏损标的
  - 滑点总结（avg / P95 / max）
  - 行业集中度：交易分布 + 风险预警（>50%触发提示）
  - 格式：飞书文本推送
- 周一 cron 触发：`python scripts/quant/performance_report.py --days 7`

### ✅ — 参数自适应更新
- **新建** `scripts/regime_wfa.py`
- 每月第一个交易日自动运行 WFA 分析（2年训练/1年测试窗口）
- 对 watchlist 每个标的执行 RSI 网格搜索，统计最优 RSI 参数
- 决策逻辑：
  - Sharpe 提升 ≥ 0.10 且 RSI 差值 ≤ 5 → **自动批准**
  - Sharpe 提升 ≥ 0.10 但 RSI 差值 > 5 → **人工审批**
  - Sharpe 无提升 → **拒绝变更**
- 变更时推送飞书审批通知（`FEISHU_WEBHOOK`）
- 通过 `--auto-approve` 标志自动写入 `live_params.json`

### ✅ — 动态选股环境感知
- **修改** `scripts/dynamic_selector.py`（P6.3 patch）
- `DynamicStockSelectorV2.__init__(regime='CALM')` 接受市场环境参数
- `_regime_modulate(score_dict, regime)` 根据环境调制板块评分：
  - **BULL**: 动量板块（AI/芯片/5G/军工/新能源）× 1.2
  - **BEAR**: 防御板块（电力/医药/消费/银行）× 1.2，非防御 × 0.85
  - **VOLATILE**: 全部分数 × 0.80（降低敏感度）
  - **CALM**: 不变
- `select_stocks(top_n, regime=None)` 支持传入当前环境参数
- `morning_runner.py` 调用时传入 `get_regime_params()['regime']`

---

## P7 · 风险管理精细化

### ✅ — 单一标的仓位上限（25%）
- `broker.py` submit_order 内置前检查：
  - 新开仓：超出 25% 权益上限 → 自动压缩至上限
  - 已有持仓超标 → 拒绝开仓

### ✅ — Chandelier Exit（3×ATR）
- `signals.py` check_atr_trailing_stop() 已完整实现（最高价 - 3×ATR）
- `intraday_monitor.py` 止盈循环中优先执行 ATR 移动止盈

### ✅ — 北向共振精细化（持续 vs 脉冲）
- `northbound.py` 新增三个函数：
  - get_north_flow_direction(threshold_yi=50.0) → strength=2(持续)/1(脉冲)/0(中性)
  - get_north_flow_history() → 读取/写入 north_flow_history.json
  - NorthBoundAlertChecker → 大幅净流入/出推送
- `signals.py` evaluate_signal() 集成：strength=2 → 北向持续共振+XXX亿；strength=1 → 北向脉冲+XXX亿

---

## P8 · 数据源稳定化（持续性工程）

### ✅ — KAMT 多源缓存 + Fallback
- **新建** `backend/services/data_cache.py` + `scripts/quant/data_cache.py`
- `_SafeCache`: 线程安全单调时间 TTL 缓存（60s KAMT / 60s 分钟K线 / 30s 通用 HTTP）
- `cached_kamt()`: 三级 Fallback 链：
  1. eastmoney `push2.eastmoney.com/api/qt/kamt.rtmin`（实时，每分钟更新）
  2. eastmoney `push2.eastmoney.com/api/qt/kamt/get`（日度摘要，配额数据）
  3. 过期缓存（返回 `stale=True` 标记）
- `northbound.py` 的 `fetch_kamt()` 替换为 `cached_kamt()` 包装，60s 内重复调用走缓存

### ✅ — 分钟 K 线缓存（60s TTL）
- `cached_minute_kline(symbol, fetch_fn)`: 防止同一分钟内重复请求导致限流
- 适用于腾讯/新浪分钟K线数据源
- `cached_get(url)`: 通用 HTTP GET 缓存（默认 30s TTL）

---

## 已完成 → 85 分目标

### P6 已完成
| 任务 | 文件 |
|------|------|
| 周度绩效归因报告 | `scripts/quant/performance_report.py` |
| 参数自适应 WFA | `scripts/regime_wfa.py` |
| 动态选股环境感知 | `scripts/dynamic_selector.py` (P6.3 patch) |

### P7 已完成
| 任务 | 文件 |
|------|------|
| 单标的仓位上限 25% | `broker.py` |
| Chandelier Exit 3×ATR | `signals.py` + `intraday_monitor.py` |
| 北向共振精细化 | `northbound.py` |

## 历史已完成

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
