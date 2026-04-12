# A-Share 量化交易系统

基于 A 股的自动化量化研究与交易平台，支持回测、实时信号监控、定时报告推送。

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                      小黑 (Agent)                            │
│              决策推理 + 命令执行，通过 HTTP 与后台交互           │
└──────────────┬─────────────────────▲──────────────────────┘
                │ HTTP API                │ 分析结果推送
                ▼                        │
┌───────────────────────────────────────────────────────────┐
│          Backend Service (常驻进程, port 5555)              │
│                                                           │
│   HTTP API (Flask) — 16 个端点                             │
│   ├── GET/POST /positions, /trades, /orders, /signals    │
│   └── POST /orders/submit, /analysis/run, /portfolio/   │
│                                                           │
│   PortfolioService (SQLite 持久化)                          │
│   ├── positions | trades | cash | signals | orders        │
│   └── wf_results.db (Walk-Forward 训练结果)                │
│                                                           │
│   IntradayMonitor (交易时段后台线程)                        │
│   └── 9:35-11:30 / 13:00-14:55 每5分钟检查信号          │
└──────────────────┬──────────────────────────────────────┘
                    │
        ┌──────────┴──────────┐
        ▼                      ▼
┌───────────────────┐   ┌────────────────────────────┐
│ scripts/quant/   │   │  strategies/              │
│   回测与分析引擎   │   │   策略插件 (RSI/MACD/BB)  │
│                  │   │   load_strategy() 动态加载 │
│ dynamic_selector  │   └────────────────────────────┘
│ signal_generator │            │
│ portfolio_engine │            ▼
│ walkforward      │   ┌────────────────────────┐
│ monte_carlo      │   │ params.json           │
│ benchmark        │   │ 策略参数配置          │
└───────────────────┘   └────────────────────────┘

推送层
  └── 飞书 (REST API 直推)
       ├── 9:00  早报（外盘 + 持仓 + 关注板块）
       └── 15:30 收盘总结（大盘 + 持仓盈亏 + 信号回顾）
```

## 快速启动

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 启动后端服务

```bash
# 同时启动 API + 定时任务
python backend/main.py --mode both --port 5555

# 仅 API
python backend/main.py --mode api --port 5555
```

### 3. Web UI (可选)

```bash
streamlit run streamlit_app.py --server.port 8501
# 浏览器打开 http://localhost:8501
```

### 4. 查询与操作

```bash
# 组合概览
curl http://127.0.0.1:5555/portfolio/summary

# 记录交易
curl -X POST http://127.0.0.1:5555/trades \
  -H "Content-Type: application/json" \
  -d '{"symbol":"600900.SH","direction":"BUY","shares":200,"price":23.50}'

# 提交订单意图（Paper撮合）
curl -X POST http://127.0.0.1:5555/orders/submit \
  -H "Content-Type: application/json" \
  -d '{"symbol":"600900.SH","direction":"BUY","shares":200,"price":23.50}'

# 推送今日收盘报告（手动）
python backend/services/report_sender.py --type close
```

## 项目结构

```
scripts/
├── dynamic_selector.py       # 五维动态选股（V2）
├── stock_data_only.py       # 日报脚本（独立运行）
├── walkforward_job.py       # Walk-Forward 自动训练任务
└── quant/
    ├── signal_generator.py   # 信号引擎（RSI/MACD/布林带/机构持仓/市场环境）
    ├── portfolio_engine_v3.py # 组合回测引擎（含熔断+止损）
    ├── walkforward.py        # Walk-Forward 分析
    ├── monte_carlo.py        # Monte Carlo 模拟
    ├── benchmark.py          # 沪深300 基准对比
    ├── news_quality.py      # 新闻质量评分
    └── data_loader.py        # 数据加载（腾讯/新浪/AKShare）

strategies/                   # 策略插件（热插拔）
├── __init__.py               # 注册中心 + load_strategy()
├── base.py                   # BaseStrategy 基类
├── rsi_strategy.py            # RSI 策略
├── macd_strategy.py           # MACD 策略
└── bollinger_strategy.py     # 布林带策略

backend/
├── main.py                  # 入口（API + Scheduler + IntradayMonitor）
├── api.py                  # Flask HTTP API（16端点）
├── openapi.json             # OpenAPI 3.0 文档
└── services/
    ├── portfolio.py         # SQLite 组合持久化
    ├── broker.py           # Paper Broker（VWAP撮合）
    ├── signals.py          # A股盘中信号引擎（涨跌停+RSI）
    ├── intraday_monitor.py # 盘中监控线程
    ├── report_sender.py     # 定时报告生成与推送
    ├── strategy_loader.py   # 策略插件加载器
    ├── walkforward_persistence.py  # WFA 结果持久化
    └── live_params.json    # 最新训练参数（实时读取）

streamlit_app.py              # Web UI（6页面）
start_streamlit.bat           # Streamlit 启动脚本
start.bat / stop.bat          # Backend 启停脚本
params.json                  # 策略参数配置（可编辑）
requirements.txt
```

## 五维选股系统

| 维度 | 权重 | 数据来源 |
|------|------|---------|
| 新闻热度 | 15% | 东方财富（已过滤含糊表述） |
| 板块行情 | 35% | 东方财富 BK 涨跌幅排名 |
| 资金流向 | 25% | 北向/主力净流入 |
| 技术趋势 | 15% | 成分股涨跌信号 |
| 一致性 | 10% | 板块内联动强度 |

**新闻质量过滤：** 有望/或将/知情人士等含糊表述 → 降权或丢弃

## 信号类型

| 信号 | 含义 |
|------|------|
| `LIMIT_UP` / `LIMIT_DOWN` | 涨跌停（放量/缩量判断） |
| `LIMIT_RISK_UP` / `LIMIT_RISK_DOWN` | 逼近涨跌停（<1%） |
| `WATCH_LIMIT_UP` / `WATCH_LIMIT_DOWN` | 接近涨跌停（<3%） |
| `RSI_BUY` / `RSI_SELL` | RSI 超买超卖 + 动量确认 |
| `WATCH_BUY` / `WATCH_SELL` | RSI 极端区域 |
| `VOLATILE` | 大幅波动（>3%） |

## 策略插件

```python
from strategies import load_strategy

# 加载 RSI 策略
strat = load_strategy('RSI', {'rsi_buy': 30, 'rsi_sell': 65}, symbol='600519.SH')
result = strat.evaluate(kline_data, i=-1)

# 可用策略
# RSI    — 超买超卖均值回归
# MACD   — 金叉死叉趋势跟踪
# BollingerBand — 布林带均值回归
```

参数通过 `params.json` 配置，支持热更新。

## 定时报告（Cron）

- **9:00 早报** — 市场概况 + 持仓开盘参考 + 关注板块
- **15:30 收盘总结** — 大盘指数 + 持仓今日盈亏 + 信号回顾 + 涨跌停风险

推送至飞书，OpenClaw Cron 配置：`0 9 * * 1-5` / `30 15 * * 1-5`

## 运行测试

```bash
pytest tests/ -v
# 或
python tests/run_tests.py
```

## 关键设计决策

1. **持久化后端** — 状态不丢失，支持 agent 跨会话查询
2. **轻量化依赖** — 标准库 + flask + requests
3. **A 股专用** — 涨跌停规则（ST ±5%、创业板/科创板 ±20%）
4. **Paper Broker** — VWAP 撮合，隔离真实资金
5. **Walk-Forward** — 滚动训练/测试，避免过拟合

## 已知限制

- 盘中信号基于日线 RSI 近似（非分钟数据）
- 真实券商账户待接入（Futu/Tiger）
- 新闻情感为关键词判断，非 NLP 模型

## 免责声明

本系统仅供研究与教育目的。回测结果不代表未来收益，所有数据仅供参考，不构成投资建议。

## 协议

MIT License — 详见 `LICENSE` 文件。
