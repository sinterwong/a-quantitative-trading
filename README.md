# A-Share 量化交易系统

基于 A 股的自动化量化研究与交易平台，支持回测、实时信号监控、定时报告推送。

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────┐
│                      小黑 (Agent)                           │
│              决策推理 + 命令执行，通过 HTTP 与后台交互           │
└──────────────┬─────────────────────▲──────────────────────┘
               │ HTTP API                 │ 分析结果推送
               ▼                          │
┌──────────────────────────────────────────────────────────────┐
│          Backend Service (常驻进程, port 5555)                │
│                                                              │
│   HTTP API — 17 个端点                                       │
│   ├── GET  /health, /positions, /cash, /trades, /signals   │
│   ├── GET  /portfolio/summary, /portfolio/daily            │
│   ├── POST /trades, /signals, /orders/submit               │
│   ├── POST /portfolio/positions, /portfolio/cash            │
│   └── POST /analysis/run, /analysis/monthly/snapshot        │
│                                                              │
│   PortfolioService (SQLite 持久化)                           │
│   ├── positions | trades | cash | signals | daily_meta     │
│   └── orders | alert_history                               │
│                                                              │
│   Scheduler — 每日 15:10 触发分析                            │
│   IntradayMonitor — 交易时段每 5 分钟检查信号                 │
│   NorthBoundAlertChecker — 北向资金大幅异动推送                │
└──────────────────┬───────────────────────────────────────┘
                   │
       ┌───────────┴───────────┐
       ▼                       ▼
┌────────────────────┐   ┌────────────────────────────────┐
│   scripts/quant/  │   │   strategies/                  │
│   回测与分析引擎    │   │   策略插件 (RSI/MACD/BB)        │
│                    │   │   load_strategy() 动态加载       │
│ dynamic_selector   │   └────────────────────────────────┘
│ signal_generator  │              │
│ portfolio_engine   │              ▼
│ walkforward       │   ┌────────────────────────────────┐
│ monte_carlo       │   │   params.json / live_params.json│
│ regime_detector   │   │   策略参数配置（可编辑/热更新）    │
│ strategy_ensemble │   └────────────────────────────────┘
│ daily_journal
│ news_scorer
│ data_loader
└────────────────────┘

推送层
  └── 飞书 (REST API 直推)
       ├── 9:00  早报（市场情绪 + 核心主题 + 热门板块 + 关注标的 + 精选资讯 + 北向资金）
       ├── 15:30 收盘总结（大盘指数 + 持仓盈亏 + 今日要闻 + 北向资金）
       └── 盘中异动预警（大盘指数超 ±1.5%、自选股超阈值、板块资金突变）
```

---

## 快速启动

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动后端服务

```bash
# 同时启动 API + 定时任务 + 盘中监控
python backend/main.py --mode both

# 仅 API
python backend/main.py --mode api

# 盘中监控独立运行（测试用）
python backend/main.py --mode monitor
```

### 3. 手动触发报告

```bash
# 早报
python scripts/morning_runner.py

# 晚报
python scripts/afternoon_report.py

# 绩效归因日志
python scripts/quant/daily_journal.py
```

### 4. 查询与操作

```bash
# 组合概览
curl http://127.0.0.1:5555/portfolio/summary

# 持仓
curl http://127.0.0.1:5555/positions

# 历史日净值
curl "http://127.0.0.1:5555/portfolio/daily?limit=30"

# 记录日初净值
curl -X POST http://127.0.0.1:5555/portfolio/daily \
  -H "Content-Type: application/json" \
  -d '{"equity":132000,"cash":94000,"n_trades":2,"notes":"开盘"}'

# 提交订单意图（Paper撮合）
curl -X POST http://127.0.0.1:5555/orders/submit \
  -H "Content-Type: application/json" \
  -d '{"symbol":"600900.SH","direction":"BUY","shares":200,"price":0,"price_type":"market"}'
```

---

## 项目结构

```
scripts/
├── morning_runner.py          # 早盘自动化（选股→信号评估→Kelly仓位→市价开仓）
├── afternoon_report.py        # 收盘自动化（持仓快照→日收益→daily_meta→飞书晚报）
├── dynamic_selector.py        # 五维动态选股 V2
├── regime_wfa_impl.py         # 市场环境 Walk-Forward 验证
├── quant/
│   ├── daily_journal.py       # 绩效归因日志（信号胜率/环境胜率/滑点分布）
│   ├── regime_detector.py     # 市场环境识别（BULL/BEAR/VOLATILE/CALM）
│   ├── strategy_ensemble.py   # 多策略组合器（环境自适应参数）
│   ├── signal_generator.py   # 信号引擎（多策略）
│   ├── portfolio_engine_v3.py # 组合回测引擎（含熔断+止损）
│   ├── walkforward.py        # Walk-Forward 分析
│   ├── monte_carlo.py        # Monte Carlo 模拟
│   ├── benchmark.py          # 沪深300 基准对比
│   ├── news_scorer.py       # 新闻情绪打分（东方财富快讯）
│   ├── news_quality.py      # 新闻质量评分
│   └── data_loader.py       # 数据加载（腾讯/新浪/AKShare）

strategies/                    # 策略插件（热插拔）
├── __init__.py               # 注册中心 + load_strategy()
├── base.py                   # BaseStrategy 基类
├── rsi_strategy.py           # RSI 策略
├── macd_strategy.py          # MACD 策略
└── bollinger_strategy.py    # 布林带策略

backend/
├── main.py                  # 入口（API + Scheduler + IntradayMonitor）
├── api.py                   # Flask HTTP API（17端点）
├── openapi.json             # OpenAPI 3.0 文档
└── services/
    ├── portfolio.py          # SQLite 组合持久化
    ├── broker.py           # Paper Broker（VWAP撮合 + 仓位上限 + Chandelier Exit）
    ├── signals.py          # A股盘中信号引擎（涨跌停 + RSI + ATR过滤 + 北向共振）
    ├── intraday_monitor.py # 盘中监控线程（RSI买卖 + 涨跌幅止损/止盈 + ATR移动止盈）
    ├── northbound.py       # 北向资金追踪（KAMT接口 + 持续/脉冲检测）
    ├── report_sender.py    # 定时报告生成与推送
    ├── live_params.json    # 最新训练参数（实时读取）
    └── watchlist.py        # 自选股管理（SQLite持久化 + 独立预警阈值）

params.json                  # 策略参数配置（可编辑）
requirements.txt
```

---

## 五维选股系统 V2

| 维度 | 权重 | 数据来源 | 说明 |
|------|------|---------|------|
| 新闻热度 | 15% | 东方财富快讯 | 含糊表述（有望/或将）降权 |
| 板块行情 | 35% | 东方财富 BK 涨跌幅 | 北向资金排名优选 |
| 资金流向 | 25% | 北向/主力净流入 | 持续流入 > 脉冲 |
| 技术趋势 | 15% | 成分股涨跌信号 | 板块内一致性 |
| 一致性 | 10% | 板块内联动强度 | 成分股共振度 |

**降级机制：** API 失败时自动切换宽基 ETF（沪深300、创业板、酒ETF）。

---

## 信号类型

| 信号 | 含义 | 触发动作 |
|------|------|---------|
| `RSI_BUY` / `RSI_SELL` | RSI 超买超卖 + 15min 二次确认 | 满足环境参数 → 开仓/平仓 |
| `WATCH_BUY` / `WATCH_SELL` | RSI 极端区域观察 | 仅推送，不交易 |
| `LIMIT_UP` / `LIMIT_DOWN` | 涨跌停检测 | 风险屏蔽 |
| `CHANDELIER_LONG` | Chandelier Exit（3×ATR） | 移动止盈 |
| `北向持续共振` | 北向连续3日 > 50亿 | 强化买入信号 |
| `北向脉冲` | 单日 > 100亿 | 辅助确认 |

---

## 市场环境与策略参数

系统自动识别四种市场环境，参数自适应：

| 环境 | 判断条件 | RSI | ATR阈值 | 止盈 | 止损 |
|------|---------|-----|--------|------|------|
| **BULL** | MA20>MA60 且指数>MA20 | 25/65 | 0.90 | 20% | 5% |
| **BEAR** | 指数<MA60 或 均线空头 | 40/70 | 0.80 | 15% | 5% |
| **VOLATILE** | ATR ratio > 0.90（高波动） | 30/60 | 0.80 | 25% | 5% |
| **CALM** | ATR ratio ≤ 0.85 | 25/65 | 0.85 | 20% | 5% |

**ATR Ratio =** 当前 ATR(14) / 近20日 ATR 最高值
- ATR Ratio > 0.85：不开新仓（高波动期 RSI 均值回归失效）

---

## 盘中风险管理

| 机制 | 参数 | 说明 |
|------|------|------|
| 仓位上限 | 单标的 ≤ 25% 总权益 | Kelly 半仓 × 双重限制 |
| RSI 止损 | RSI_SELL 区域 | 持仓触发 → 市价卖出 |
| Chandelier Exit | 3×ATR(14) | 移动止盈，锁定利润 |
| 固定止盈 | TakeProfit 阈值 | 环境自适应 |
| 涨跌停屏蔽 | ±9.5% 当日 | 风险信号不交易 |

---

## 定时报告

| 时间 | 报告 | 内容 |
|------|------|------|
| **9:00** | 早报 | 市场情绪 + 核心主题 + 热门板块 + 关注标的 + 精选资讯 + 北向资金 |
| **15:30** | 晚报 | 大盘收盘 + 热门板块 + 持仓盈亏 + 今日要闻 + 北向资金 |
| **盘中** | 异动预警 | 大盘指数 ±1.5% + 自选股异动 + 板块资金突变 |

---

## 回测框架

```bash
# RSI 网格搜索
python scripts/quant/backtest_cli.py single 600900.SH --rsi 25 75 --days 500

# WFA 滚动验证
python scripts/quant/backtest_cli.py wf 600900.SH

# 多策略对比
python scripts/quant/backtest_cli.py compare 510310.SH

# 环境自适应 vs 固定参数对比
python scripts/quant/backtest_cli.py regime-wfa 510310.SH

# 压力测试（股灾/贸战/封控）
python scripts/quant/backtest_cli.py crash-test 510310.SH

# MACD 系列对比
python scripts/quant/backtest_cli.py macd-compare 510310.SH
```

**验收标准：** Sharpe ≥ 0.5，MaxDD ≤ 20%，正收益窗口 ≥ 60%

---

## 策略插件

```python
from strategies import load_strategy

strat = load_strategy('RSI', {'rsi_buy': 30, 'rsi_sell': 65}, symbol='600519.SH')
result = strat.evaluate(kline_data, i=-1)
```

---

## 关键设计决策

1. **环境自适应** — 4种市场环境自动识别，策略参数动态切换
2. **ATR 波动率过滤** — 高波动期（>90%阈值）屏蔽新仓，避开 RSI 均值回归失效段
3. **Chandelier Exit** — 3×ATR 移动止盈，锁定趋势行情利润
4. **盘中 15min RSI 二次确认** — 避免虚假突破，提高信号质量
5. **Paper Broker** — VWAP 撮合，隔离真实资金，所有操作可追溯
6. **Walk-Forward** — 滚动训练/验证，避免过拟合

---

## 已知限制

- 盘中信号基于日线 RSI 近似（非真实分钟数据）
- 真实券商账户待接入（Futu/Tiger）
- 新闻情感为关键词判断，非 NLP 模型
- 北向资金数据来源需进一步验证单位

---

## 免责声明

本系统仅供研究与教育目的。回测结果不代表未来收益，所有数据仅供参考，不构成投资建议。

---

## 协议

MIT License
