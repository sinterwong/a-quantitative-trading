# Skills 盘点 — 量化交易系统可构建能力

## 一、API 全景（51 个端点）

### 1. 持仓与组合（6）
| 端点 | 方法 | 用途 |
|------|------|------|
| `/positions` | GET | 当前全部持仓 |
| `/portfolio/summary` | GET | 完整组合快照（浮盈浮亏/现金/总权益） |
| `/portfolio/daily` | GET/POST | 日终总结（历史净值） |
| `/portfolio/cash` | POST | 设置现金余额 |
| `/portfolio/positions` | POST | 录入/更新持仓 |
| `/portfolio/compose` | POST | 组合构建（批量录入） |

### 2. 交易记录（2）
| 端点 | 方法 | 用途 |
|------|------|------|
| `/trades` | GET/POST | 成交历史（支持 symbol/limit 过滤） |
| `/signals` | GET/POST | 信号历史 |

### 3. 订单（4）
| 端点 | 方法 | 用途 |
|------|------|------|
| `/orders/submit` | POST | 提交订单（触发 broker 撮合） |
| `/orders/recent` | GET | 最近订单状态 |
| `/orders/pending` | GET | 待成交订单 |
| `/orders/{order_id}/cancel` | POST | 取消订单 |

### 4. 分析引擎（11）
| 端点 | 方法 | 用途 |
|------|------|------|
| `/analysis/run` | POST | 触发每日市场分析 |
| `/analysis/status` | GET | 分析状态/最后结果 |
| `/analysis/health` | GET | 策略健康度 |
| `/analysis/stock/a` | POST | A股个股分析 |
| `/analysis/stock/hk` | POST | 港股分析 |
| `/analysis/pairs_trading` | POST | 配对交易分析 |
| `/analysis/sector/compare` | POST | 板块对比 |
| `/analysis/sector_rotation` | POST | 板块轮动分析 |
| `/analysis/monthly` | GET | 月度总结 |
| `/analysis/monthly/snapshot` | POST | 月度快照 |
| `/analysis/monthly/history` | GET | 月度历史 |

### 5. 市场数据（6）
| 端点 | 方法 | 用途 |
|------|------|------|
| `/data/realtime/{symbol}` | GET | 实时行情 |
| `/data/daily/{code}` | GET | 日线历史 |
| `/data/news/{symbol}` | GET | 个股新闻 |
| `/data/macro/{indicator}` | GET | 宏观指标 |
| `/data/fund_flow` | GET | 资金流向 |
| `/data/status` | GET | 数据源状态 |

### 6. 基本面（1）
| 端点 | 方法 | 用途 |
|------|------|------|
| `/fundamentals/{symbol}` | GET | 个股基本面数据 |

### 7. 交易运行模式（5）
| 端点 | 方法 | 用途 |
|------|------|------|
| `/trading/mode` | GET/PUT | 查询/切换 simulation/live |
| `/monitor/status` | GET | IntradayMonitor 状态 |
| `/risk/status` | GET | 风控状态 |
| `/metrics` | GET | 系统指标 |
| `/llm/analyze` | POST | LLM 独立分析 |

### 8. 监控与预警（5）
| 端点 | 方法 | 用途 |
|------|------|------|
| `/watchlist` | GET | 监控标的列表 |
| `/watchlist/add` | POST | 添加监控标的 |
| `/watchlist/{symbol}` | DELETE/PATCH | 删除/更新标的 |
| `/alerts/history` | GET | 预警历史 |
| `/alerts/clear` | POST | 清除预警 |

### 9. 回测与研究（4）
| 端点 | 方法 | 用途 |
|------|------|------|
| `/backtest/run` | POST | 因子/策略回测 |
| `/wfa/summary` | GET | Walk-Forward 分析摘要 |
| `/wfa/history` | GET | WFA 历史 |
| `/northbound/flow` | GET | 北向资金流 |

### 10. 参数与配置（3）
| 端点 | 方法 | 用途 |
|------|------|------|
| `/params` | GET | 全局参数 |
| `/params/{symbol}` | GET/PATCH | 单标的参数（RSI阈值/止盈止损等） |

### 11. 绩效（1）
| 端点 | 方法 | 用途 |
|------|------|------|
| `/performance/summary` | GET | 组合绩效指标 |

---

## 二、可构建 Skills 候选

### 高优先级

#### 1. `monthly-performance-report`
**触发**：每月最后交易日（或定时 28 日）
**能力**：
- GET `/performance/summary` — 总收益率、夏普比、最大回撤
- GET `/portfolio/daily` — 月度收益曲线
- GET `/trades?limit=100` — 本月全部成交
- GET `/analysis/monthly` — 月度市场分析
- 汇总输出：收益归因、持仓变化、胜率统计、因子表现
- 推送飞书

**价值**：每月定期回顾，不用手动查数据

---

#### 2. `trade-signal-monitor`
**触发**：cronjob，每 5 分钟（或依赖 IntradayMonitor 事件推送）
**能力**：
- GET `/signals?since=...` — 轮询新信号
- GET `/orders/recent` — 最新成交状态
- GET `/positions` — 当前持仓快照
- 新仓信号 → 输出标的/价格/原因/置信度
- 成交确认 → 输出执行结果
- 异常（拒单/超时/风控拦截）→ 立即告警飞书

**价值**：实时跟踪系统决策，不需要盯着日志

---

#### 3. `factor-health-monitor`
**触发**：每日开盘后 + 因子衰减告警时
**能力**：
- GET `/analysis/health` — 因子 IC/IR 状态
- GET `/risk/status` — 风控引擎状态
- 识别：连续 IC<0 的因子 → 触发衰减保护
- 对比：当前因子权重 vs 上次历史
- 推送：因子健康仪表盘（哪些因子在看多/看空/失效）
- 建议：哪些因子可以手动恢复权重

**价值**：因子失效早发现，避免在错误因子主导下做决策

---

#### 4. `backtest-runner`
**触发**：手动触发（或者每周末）
**能力**：
- POST `/backtest/run` — 用当前持仓/参数跑回测
- GET `/wfa/summary` — Walk-Forward 结果
- 对比：实盘收益 vs 回测收益
- 输出：Alpha/Beta/夏普比/最大回撤 vs 基准对比
- 判断：实盘是否跑赢回测期望

**价值**：定期验证策略是否还在线，避免在策略失效后继续运行

---

#### 5. `position-diary`
**触发**：每日收盘后（或定时）
**能力**：
- GET `/portfolio/summary` — 所有持仓浮盈浮亏
- GET `/positions` — 各标的 entry_price vs latest_price
- 计算：持仓时长（自然天/交易日）
- 标记：哪些持仓超过 max_hold_days 但未触发退出
- 标记：哪些持仓接近止损线但未触发
- 输出：持仓日记 — 每只标的的状态摘要
- 推送飞书

**价值**：解决 entry_date 缺失导致的 exit_engine 误触发问题，也有持仓全局视角

---

#### 6. `live-mode-watcher`
**触发**：实时监控（或者 cron 每 10 分钟）
**能力**：
- GET `/trading/mode` — 查询当前模式
- 监控：从 simulation → live 或 live → simulation 的切换
- 切换时告警飞书："交易模式已切换为 LIVE，请确认是否在预期内"
- 记录切换原因（人工操作还是系统自动）

**价值**：防止误操作切换到 live 模式导致意外成交

---

### 中优先级

#### 7. `market-bias-dashboard`
**触发**：每日开盘 09:35 / 14:55
**能力**：
- POST `/analysis/run` — 触发市场分析
- GET `/data/status` — 各数据源可用性
- GET `/monitor/status` — 今日市场情绪（趋势/情绪/波动率）
- 输出：大盘开盘状态 + 板块强弱 + 资金流向
- 判断：今天是适合建仓/持有/减仓

**价值**：每天开盘/尾盘给一个市场环境速览，辅助当天决策

---

#### 8. `sector-rotation-viewer`
**触发**：每周一（或 cronjob 每周一 09:30）
**能力**：
- POST `/analysis/sector/rotation` — 板块轮动分析
- GET `/northbound/flow` — 北向资金
- 输出：本周强势板块 / 弱势板块 / 轮动信号
- 对比：当前持仓板块是否在强势区

**价值**：辅助调仓参考

---

#### 9. `earnings-season-alert`
**触发**：财报季前（每年 4/7/10 月初）或监控 `/data/news/{symbol}` 时
**能力**：
- 监控 watchlist 标的的财报发布日程
- GET `/fundamentals/{symbol}` — 最新财报关键指标
- 对比：分析师预期 vs 实际 EPS/营收
- 重大miss/surprise → 立即告警

**价值**：基本面风险预警

---

#### 10. `wfa-walkforward`
**触发**：每月（或手动）
**能力**：
- GET `/wfa/summary` — Walk-Forward 分析结果
- 分析：每个窗口的 IC/IR 稳定性
- 判断：策略是否在衰退
- 输出：WFA 可视化数据（供后续出图）

**价值**：比简单回测更真实的策略有效性评估

---

## 三、Skill 实现注意事项

### 共用工具
- Backend 基础 URL：`http://localhost:5555`
- 认证：`X-API-Key` header（生产环境配置 TRADING_API_KEY）
- 当前 user_open_id：飞书推送使用 second-bot (app_id: cli_a97b6f7b40f9dcd1)

### 飞书推送目标
- 早报/日终报告 → `oc_8d67f97916478affc49b578c028152c2`（second-bot）
- 紧急告警（live模式切换/风控拦截）→ 同 chat_id

### 时区注意
- 所有时间戳 UTC，盘中cron用 Asia/Shanghai
- `/portfolio/daily` 日期用 `YYYY-MM-DD`

### 数据层已知缺口
- AkShare 港股基本面数据零覆盖，港股分析结果仅供参考
- 因子IC数据依赖日线收盘，日间实时性有限
