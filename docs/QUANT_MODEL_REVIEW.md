# 量化交易模型层 — 全面复盘报告

> 复盘时间：2026-05-15
> 复盘范围：scripts/ · backend/services/ · core/strategies/ · core/factors/ · core/
> 团队：小黑（Hermes Agent）

---

## 一、架构总览

```
┌─────────────────────────────────────────────────────────────┐
│                        调度层 (Scheduler)                     │
│   09:30 morning_runner  │  09:31 IntradayMonitor            │
│   15:00 afternoon_report │  15:10 DynamicStockSelector      │
│   15:30 daily_risk_report │ 15:45 daily_tca  │ 16:00 ops      │
└────────────────────────────┬────────────────────────────────┘
                             │
         ┌───────────────────┼────────────────────┐
         ▼                   ▼                    ▼
  ┌──────────────┐   ┌──────────────┐   ┌──────────────────┐
  │MorningRunner │   │IntradayMonitor│   │DynamicSelectorV2 │
  │  (scripts/)  │   │ (services/)  │   │   (scripts/)     │
  └──────┬───────┘   └──────┬───────┘   └────────┬─────────┘
         │                  │                    │
         ▼                  ▼                    ▼
  ┌──────────────────────────────────────────────────────────┐
  │              DataLayer / DataGateway                      │
  │  Tencent · Sina · Eastmoney · Baostock · AkShare        │
  └──────────────────────────────────────────────────────────┘
```

---

## 二、核心发现：最大的数据层 Gap

> ⚠️ **最关键发现**：BaostockProvider + BalanceSheet + YoY 字段已完整接入 DataLayer，但三个核心业务脚本**完全未消费**。

| 数据字段 | 状态 | 业务层使用情况 |
|----------|------|---------------|
| Fundamentals（roe_ttm/eps_ttm/ revenue_yoy/profit_yoy） | ✅ 已标准化 | ❌ 完全未使用 |
| BalanceSheet（debt_to_equity/ current_ratio/quick_ratio） | ✅ 已标准化 | ❌ 完全未使用 |
| profit_yoy / eps_yoy / asset_yoy | ✅ 已标准化 | ❌ 完全未使用 |
| industry 行业分类 | ✅ 已标准化 | ❌ 完全未使用 |

**影响**：数据层投了大量工程资源，但业务层（早报/选股/盘中监控）完全没用到，Alpha 潜力被白白浪费。

---

## 三、各模块详细分析

### 3.1 scripts/morning_runner.py（355行）

**链路**：选股 → 同步 watchlist → 读取 Regime → 记录 meta → 飞书早报

| 问题 | 级别 | 说明 |
|------|------|------|
| 早报时间漏改 | P0 | `DAILY_TASKS` 漏改，已修复（08:30→09:30） |
| 硬编码端口 5555 | P2 | 直接写死 URL，未从环境变量读取 |
| 选股数量 top_n=5 硬编码 | P2 | 无法动态调整 |
| nav 基值 100000 硬编码 | P2 | afternoon_report 也有，净值基准不统一 |
| 飞书推送代码重复 | P2 | 与 afternoon_report 完全重复的代码未抽取 |

### 3.2 scripts/afternoon_report.py（338行）

**链路**：持仓快照 → 计算日收益 → 记录 meta → 飞书晚报

| 问题 | 级别 | 说明 |
|------|------|------|
| nav=equity/100000 硬编码 | P1 | 与 morning_runner 一致，但无 benchmark 对比 |
| 缺少 benchmark（沪深300） | P1 | 自身收益无参照，无法判断相对优劣 |
| 飞书代码重复 | P2 | 与 morning_runner 重复代码未抽取为 Service |

### 3.3 scripts/dynamic_selector.py（1072行）— 核心选股引擎

**五维度加权评分**：

| 维度 | 权重 | 核心数据 |
|------|------|----------|
| 新闻情绪 | 15% | 东方财富标题 + LLM 情感分析 |
| 行情技术 | 35% | 涨跌停、RSI、MACD、资金流 |
| 板块资金 | 25% | 东方财富板块资金流 |
| 技术信号 | 15% | RSI 超买超卖 |
| 一致性 | 10% | 板块内成分股评分一致性 |

**关键缺陷**：

| 缺陷 | 级别 | 说明 |
|------|------|------|
| **Baostock 基本面零消费** | **P0** | 已有 profit_yoy/eps_yoy/industry/debt_to_equity，评分体系完全不用 |
| 注释写 top_n=10 实际=3 | P2 | `calc_consistency_score_for_bk` 注释与代码不符 |
| FALLBACK_ETFS 已死代码 | P2 | 常量定义但从未使用 |
| 新闻关键词缺新热点 | P2 | 缺少 低空经济/AI Agent/量子计算 等 2025-2026 新主题 |

**升级建议（优先级排序）**：

1. **P0**：在行情维度（35%）中叠加 Baostock 基本面因子——profit_yoy、eps_yoy、debt_to_equity
2. **P1**：在板块资金维度（25%）中叠加行业分类（industry），对不同行业差异化权重
3. **P2**：更新新闻关键词库，补充新热点主题

---

## 四、盘中监控层

### 4.1 backend/services/intraday_monitor.py（1831行）

**架构亮点**：
- 5 分钟轮询，信号来源已统一为 FactorPipeline（消除旧 RSI 硬编码路径）
- 多层安全过滤：熔断联动 → 分钟确认 → 新闻情绪 → LLM 审核 → PreTrade
- ExitEngine 统一退出（优先级：EMERGENCY > PORTFOLIO_REDUCE > STOP_LOSS > TAKE_PROFIT）

**关键缺陷**：

| 缺陷 | 级别 | 说明 |
|------|------|------|
| LLM 审核同步阻塞 | P1 | 多标的串行调用，建议 asyncio 并行 |
| LLM 失败时 auto-approve | P1 | 保守策略应改为可配置的 strict/lenient 模式 |
| Kelly 公式每日只更新一次 | P2 | 应增加滑动窗口自适应 |
| 飞书 API 裸写无重试 | P2 | 应抽取为独立 FeishuService |
| 模拟/实盘切换无审计 | P2 | trading_mode.json 无二次确认 |

### 4.2 core/strategy_runner.py（818行）

**架构亮点**：
- 注入式设计（DataLayer / Pipeline / RiskEngine / OMS 均可替换）
- Regime 感知风控（BEAR 禁止新多仓 + 阈值×1.4）
- ExitEngine 钩子 + 组合再平衡（max_sharpe / min_variance / risk_parity）
- 线程安全（_stop_event + _results_lock）

**关键缺陷**：

| 缺陷 | 级别 | 说明 |
|------|------|------|
| **串行扫描瓶颈** | **P0** | N 标的延迟 N×200ms，`async_runner.py` 已存在但未设为默认 |
| 信号发射双重路径 | P1 | EventBus → OMS，可能重复提交 |
| 再平衡信号逻辑冗余 | P1 | `_emit_signal(pr=None)` 后又直接构造 Signal 调 OMS |
| ExitEngine 互斥无运行时校验 | P2 | 文档约定但无 assert |

---

## 五、因子与策略层

### 5.1 技术因子（core/factors/price_momentum.py）

| 因子 | 算法 | 状态 | 说明 |
|------|------|------|------|
| RSIFactor | Wilder 平滑 RSI(14) | ✅ 完全对齐 | buy=30 偏保守，可 WFA 优化 |
| BollingerFactor | BBP 归一化 | ✅ 完全对齐 | — |
| MACDFactor | 标准 MACD(12,26,9) | ⚠️ 重复 | macd_trend.py 有另一套实现，**建议统一** |
| ATRFactor | ATR ratio | ✅ 完全对齐 | 不产生信号，仅作过滤器 |
| OrderImbalanceFactor | 阳线成交量占比 | ✅ 完全对齐 | — |

### 5.2 基本面因子（core/factors/fundamental.py）

| 因子 | 依赖字段 | 状态 | 说明 |
|------|----------|------|------|
| PEPercentileFactor | pe_ttm | ❌ 降级为零 | Baostock query_history_k_data_plus 可返回 pe，需接入 |
| ROEMomentumFactor | roe_ttm | ✅ 可用 | — |
| EarningsSurpriseFactor | eps_ttm | ✅ 可用 | — |
| RevenueGrowthFactor | revenue_yoy | ✅ 可用 | — |
| CashFlowQualityFactor | ocf_to_profit | ❌ 降级为零 | ocf_to_profit 不可得 |
| ShareholderConcentrationFactor | holder_num | ❌ 降级为零 | 需接入 AKShare stock_hold_num_cninfo |

**6 个基本面因子中 3 个降级为零**，根因是数据源未接入。

### 5.3 宏观因子（core/factors/macro.py）

| 因子 | 数据源 | 状态 |
|------|--------|------|
| PMIFactor | AKShare macro_china_pmi_monthly | ⚠️ 需注入 |
| M2GrowthFactor | AKShare macro_china_money_supply_bal | ⚠️ 需注入 |
| CreditImpulseFactor | AKShare macro_china_shrzgm | ⚠️ 需注入 |

> 注：journalctl 日志显示 PMI/M2 今日获取失败（`'str' object has no attribute 'value'`），宏观因子实际未工作。

---

## 六、Pipeline 层

### 6.1 DynamicWeightPipeline（core/factor_pipeline.py）

**IC 加权算法**（核心亮点）：
- 滚动 63 天 IC 窗口，每月更新一次权重
- `w_i = max(IC_i, 0) / Σ max(IC_j, 0)`（负 IC 因子清零）
- 连续 3 次 IC<0 → 衰减禁用，恢复时半权（0.5）

**关键缺陷**：

| 缺陷 | 级别 | 说明 |
|------|------|------|
| IC 计算窗口固定 | P1 | 不同因子最优窗口不同，建议 per-factor window |
| 衰减恢复判定不严格 | P1 | 单次 IC>0 即恢复，建议要求连续 2 次或阈值判定 |
| 权重/IC 无持久化 | P1 | 重启后归零，建议序列化到 SQLite |
| 因子正交化缺失 | P1 | RSI+MACD 可能高度相关，加权叠加等效双重计数 |
| `_weight_history` 无限增长 | P2 | 长期运行内存膨胀 |

---

## 七、数据层完整对齐表

| 数据字段 | 来源 | 状态 | 业务层使用情况 |
|----------|------|------|---------------|
| OHLCV 日K | Baostock | ✅ 完整 | ✅ Pipeline 技术因子 |
| roe_ttm | Baostock | ✅ 完整 | ❌ 未使用 |
| eps_ttm | Baostock | ✅ 完整 | ❌ 未使用 |
| revenue_yoy | Baostock（自算） | ✅ 完整 | ❌ 未使用 |
| **profit_yoy** | Baostock | ✅ 完整 | ❌ 未使用 |
| **eps_yoy** | Baostock | ✅ 完整 | ❌ 未使用 |
| **asset_yoy** | Baostock | ✅ 完整 | ❌ 未使用 |
| **debt_to_equity** | Baostock | ✅ 完整 | ❌ 未使用 |
| **current_ratio** | Baostock | ✅ 完整 | ❌ 未使用 |
| **quick_ratio** | Baostock | ✅ 完整 | ❌ 未使用 |
| **industry** | Baostock | ✅ 完整 | ❌ 未使用 |
| pe_ttm | Baostock query_history_k_data | ⚠️ 可得 | ❌ 未接入（PEPercentile 降级） |
| ocf_to_profit | — | ❌ 不可得 | CashFlowQuality 降级 |
| holder_num | AKShare stock_hold_num_cninfo | ❌ 未接入 | ShareholderConcentration 降级 |

---

## 八、升级路线图

### P0 — 立即处理（数据层 Gap 修复）

| 项目 | 收益 | 工作量 |
|------|------|--------|
| Baostock 基本面数据接入 DynamicSelector | 五维度评分叠加 profit_yoy/eps_yoy/industry | 中 |
| Baostock pe/pb 字段接入 Pipeline | PEPercentile 因子复活 | 小 |
| 统一 MACD 实现（消除 macd_trend.py 重复） | 代码质量 | 小 |

### P1 — 近期处理（架构优化）

| 项目 | 收益 | 工作量 |
|------|------|--------|
| AsyncStrategyRunner 设为默认 | N 标的延迟降低 5-10× | 小 |
| LLM 审核 strict/lenient 可配置 | 风控灵活性 | 小 |
| 动态权重持久化（SQLite） | 重启后 IC 历史不丢失 | 中 |
| 因子正交化（高相关对惩罚） | combined_score 更准确 | 中 |
| 宏观数据自动拉取（PMI/M2） | macro 因子复活 | 中 |
| 接入 AKShare stock_hold_num_cninfo | ShareholderConcentration 因子复活 | 中 |

### P2 — 中期处理（体验/可维护性）

| 项目 | 收益 | 工作量 |
|------|------|--------|
| 飞书推送抽取为 FeishuService | 可维护性 + 重试机制 | 小 |
| benchmark 对比（afternoon_report 加沪深300） | 收益评估有参照 | 小 |
| DynamicSelector 新闻关键词更新 | 覆盖新热点 | 小 |
| 可观测性增强（Prometheus metrics） | 监控能力 | 中 |

---

## 九、总结

**架构评价**：分层设计成熟度很高——Factor 基类、FactorRegistry、FactorPipeline、DynamicWeightPipeline、WFA、Regime 感知风控均已到位，比大多数开源量化框架更完整。

**最大问题**：**数据层重构跑在前面，业务层完全没跟上**。Baostock + BalanceSheet + YoY 字段是实质投入，但三个核心脚本（morning_runner / afternoon_report / dynamic_selector）完全没消费这些数据。

**最高价值行动**：
1. 将 Baostock 基本面数据（profit_yoy / eps_yoy / industry / debt_to_equity）接入 DynamicSelectorV2 的评分体系
2. 将 pe_ttm 从 Baostock query_history_k_data 接入 Pipeline，复活 PEPercentile 因子
3. AsyncStrategyRunner 设为默认，消除串行扫描瓶颈