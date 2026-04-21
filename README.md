# A-Share 量化交易系统

基于 A 股的自动化量化研究与交易平台，支持回测、实时信号监控、定时报告推送。

---

## 目录

- [系统架构](#系统架构)
- [快速启动](#快速启动)
  - [环境准备](#1-环境准备)
  - [配置 .env](#2-配置-env)
  - [启动后端服务](#3-启动后端服务-port-5555)
  - [启动 Web UI](#4-启动-web-ui-port-8501)
  - [验证运行状态](#5-验证运行状态)
- [项目结构](#项目结构)
- [核心架构模块](#核心架构模块)
- [五维选股系统](#五维选股系统-v2)
- [回测框架](#回测框架)
- [策略插件](#策略插件)
- [已知限制](#已知限制)
- [免责声明](#免责声明)

---

## 系统架构

```
┌──────────────────────────────────────────────────────────────────┐
│                       Web UI (Streamlit :8501)                   │
│  组合概览 · 实时信号 · 动态选股 · 回测分析 · 持仓详情 · 历史交易  │
└─────────────────────────┬────────────────────────────────────────┘
                          │ HTTP
┌─────────────────────────▼────────────────────────────────────────┐
│              Backend Service (Flask :5555)                       │
│                                                                  │
│  HTTP API 17 端点                                                │
│  ├── GET  /health /positions /cash /trades /signals             │
│  ├── GET  /portfolio/summary /portfolio/daily                   │
│  └── POST /trades /signals /orders/submit /analysis/run …      │
│                                                                  │
│  Scheduler — 每日 15:10 触发分析                                 │
│  IntradayMonitor — 交易时段每 5 分钟检查信号                      │
│  NorthBoundAlertChecker — 北向资金大幅异动推送                    │
└─────────────┬───────────────────────────┬────────────────────────┘
              │                           │
  ┌───────────▼──────────┐   ┌────────────▼─────────────────────┐
  │   scripts/quant/     │   │   core/  （新架构层）              │
  │   回测与分析引擎      │   │                                   │
  │                      │   │  DataLayer — 统一数据接口          │
  │  dynamic_selector    │   │  FactorRegistry — 因子注册表       │
  │  signal_generator    │   │  FactorPipeline — 多因子流水线     │
  │  portfolio_engine    │   │  StrategyRunner — 策略主循环       │
  │  walkforward         │   │  PortfolioRiskChecker — 组合风控   │
  │  monte_carlo         │   │  TradingConfig — 统一 YAML 配置    │
  └──────────────────────┘   └──────────────────────────────────┘

推送层
  └── 飞书 (REST API 直推)
       ├── 9:00  早报（市场情绪 + 核心主题 + 热门板块 + 关注标的）
       ├── 15:30 晚报（大盘指数 + 持仓盈亏 + 北向资金）
       └── 盘中异动预警（大盘 ±1.5%、自选股超阈值、板块资金突变）
```

---

## 快速启动

### 1. 环境准备

**Python 版本要求：≥ 3.10**

```bash
# 克隆项目
git clone https://github.com/sinterwong/a-quantitative-trading.git
cd a-quantitative-trading

# 安装依赖
pip install -r requirements.txt
pip install anthropic          # LLM 模块（MiniMax/DeepSeek 等）
pip install pyyaml             # 统一配置加载
```

> Windows 用户如遇编码问题，在 `.env` 中加入 `PYTHONIOENCODING=utf-8`

---

### 2. 配置 .env

复制模板，填入实际密钥：

```bash
cp .env.example .env
```

编辑 `.env`，至少配置 LLM 和（可选）飞书：

```ini
# LLM — MiniMax（Anthropic 兼容格式）
MINIMAX_API_KEY=sk-cp-xxxxxxxxxxxxxxxx
MINIMAX_BASE_URL=https://api.minimaxi.com/anthropic
MINIMAX_MODEL=Minimax-M2.7
LLM_PROVIDER=minimax

# 飞书通知（可选，留空则不推送）
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_USER_OPEN_ID=
```

> `.env` 已加入 `.gitignore`，不会被提交到 git。

---

### 3. 启动后端服务（port 5555）

**Linux / macOS**

```bash
# 同时启动 API + 定时任务 + 盘中监控（推荐生产模式）
python backend/main.py --mode both

# 仅启动 API（开发调试）
python backend/main.py --mode api

# 指定绑定地址（仅本机访问，默认 0.0.0.0 接受局域网）
python backend/main.py --mode both --host 127.0.0.1 --port 5555
```

**Windows**

双击 `run_backend.bat`，或在 PowerShell 中：

```powershell
python backend\main.py --mode both --port 5555
```

启动成功后终端输出：

```
INFO backend — API: http://0.0.0.0:5555
INFO backend — Scheduler started
INFO backend — IntradayMonitor started
INFO backend — Backend running. Press Ctrl+C to stop.
```

> 后端日志写入 `backend/backend.log`，可用 `tail -f backend/backend.log` 实时查看。

**验证后端是否就绪：**

```bash
curl http://127.0.0.1:5555/health
# → {"status":"ok","timestamp":"..."}
```

---

### 4. 启动 Web UI（port 8501）

**新开一个终端窗口**，保持后端进程不退出。

**Linux / macOS**

```bash
streamlit run streamlit_app.py --server.port 8501
```

**Windows**

双击 `start_streamlit.bat`，或：

```powershell
streamlit run streamlit_app.py --server.port 8501 --browser.gatherUsageStats false
```

启动后浏览器自动打开，或手动访问：

```
http://localhost:8501
```

Web UI 页面：

| 页面 | 功能 |
|------|------|
| 组合概览 | 持仓、现金、总资产、盈亏曲线 |
| 实时信号 | RSI 预警、涨跌停、北向共振 |
| 动态选股 | 五维评分结果 |
| 回测分析 | Walk-Forward + Monte Carlo |
| 持仓详情 | 个股 RSI / 量比 / 距涨跌停 |
| 历史交易 | 成交记录与绩效归因 |

> Web UI 依赖后端 API，请确保第 3 步已启动。

---

### 5. 验证运行状态

```bash
# 后端健康检查
curl http://127.0.0.1:5555/health

# 组合概览
curl http://127.0.0.1:5555/portfolio/summary

# 持仓列表
curl http://127.0.0.1:5555/positions

# 历史日净值（最近 30 天）
curl "http://127.0.0.1:5555/portfolio/daily?limit=30"

# 手动触发早报
python scripts/morning_runner.py

# 手动触发晚报
python scripts/afternoon_report.py

# 运行全量测试（273 个测试）
python tests/run_tests.py
```

**常用 POST 示例：**

```bash
# 记录日初净值
curl -X POST http://127.0.0.1:5555/portfolio/daily \
  -H "Content-Type: application/json" \
  -d '{"equity":132000,"cash":94000,"n_trades":2,"notes":"开盘"}'

# 模拟下单（Paper Broker）
curl -X POST http://127.0.0.1:5555/orders/submit \
  -H "Content-Type: application/json" \
  -d '{"symbol":"600900.SH","direction":"BUY","shares":200,"price":0,"price_type":"market"}'
```

---

## 项目结构

```
a-quantitative-trading/
├── backend/
│   ├── main.py               # 入口（API + Scheduler + IntradayMonitor）
│   ├── api.py                # Flask HTTP API（17 端点）
│   ├── openapi.json          # OpenAPI 3.0 文档
│   ├── start.bat             # Windows 快捷启动
│   └── services/
│       ├── portfolio.py      # SQLite 组合持久化
│       ├── broker.py         # Paper Broker（VWAP 撮合）
│       ├── signals.py        # A 股盘中信号引擎
│       ├── intraday_monitor.py  # 盘中监控线程
│       ├── northbound.py     # 北向资金追踪
│       ├── report_sender.py  # 定时报告生成与推送
│       ├── watchlist.py      # 自选股管理
│       └── llm/              # LLM 服务（MiniMax/DeepSeek/Kimi）
│           ├── factory.py    # Provider 工厂（读 LLM_PROVIDER 环境变量）
│           ├── service.py    # LLMService（新闻情绪 / 政策解读）
│           └── providers/    # MiniMaxProvider / DeepSeekProvider / KimiProvider
│
├── core/                     # 新架构层（可测试、可组合）
│   ├── data_layer.py         # 统一数据接口（DataLayer + BacktestDataLayer）
│   ├── factor_registry.py    # 因子注册表（FactorRegistry）
│   ├── factor_pipeline.py    # 多因子流水线（FactorPipeline + PipelineResult）
│   ├── strategy_runner.py    # 策略主循环（StrategyRunner）
│   ├── portfolio_risk.py     # 组合层风控（VaR / 行业 / 相关性 / 回撤）
│   ├── config.py             # 统一配置加载（load_config / TradingConfig）
│   ├── risk_engine.py        # 单标的风控（PreTrade / InTrade / PostTrade）
│   ├── oms.py                # 订单管理（OMS + BrokerAdapter）
│   ├── event_bus.py          # 事件总线（EventBus）
│   └── factors/
│       ├── base.py           # Factor 基类 + FactorCategory + Signal
│       └── price_momentum.py # RSIFactor / BollingerFactor / MACDFactor / ATRFactor
│
├── config/
│   └── trading.yaml          # 统一策略配置（替代分散的 params.json）
│
├── scripts/
│   ├── morning_runner.py     # 早盘自动化
│   ├── afternoon_report.py   # 收盘自动化
│   ├── dynamic_selector.py   # 五维动态选股 V2
│   └── quant/
│       ├── signal_generator.py   # 信号引擎
│       ├── portfolio_engine_v3.py # 组合回测引擎
│       ├── walkforward.py        # Walk-Forward 分析
│       ├── monte_carlo.py        # Monte Carlo 模拟
│       ├── regime_detector.py    # 市场环境识别
│       ├── news_scorer.py        # 新闻情绪打分
│       └── data_loader.py        # 数据加载（腾讯/新浪/AKShare）
│
├── strategies/               # 策略插件（热插拔）
│   ├── __init__.py           # 注册中心 + load_strategy()
│   ├── rsi_strategy.py
│   ├── macd_strategy.py
│   └── bollinger_strategy.py
│
├── tests/
│   ├── run_tests.py          # 主测试运行器（273 个测试）
│   ├── test_data_layer.py    # Phase 1 DataLayer（48 个测试）
│   ├── test_factor_pipeline.py  # Phase 2 FactorPipeline（38 个测试）
│   ├── test_strategy_runner.py  # Phase 3 StrategyRunner（32 个测试）
│   ├── test_portfolio_risk.py   # Phase 4 PortfolioRisk（34 个测试）
│   └── test_config.py           # Phase 5 Config（33 个测试）
│
├── streamlit_app.py          # Web UI 入口
├── params.json               # 策略参数（历史兼容）
├── config/trading.yaml       # 统一配置（新）
├── .env.example              # 环境变量模板
├── requirements.txt
├── run_backend.bat           # Windows 后端快捷启动
└── start_streamlit.bat       # Windows Web UI 快捷启动
```

---

## 核心架构模块

> `core/` 是系统的新架构层，提供可测试、可组合的核心能力，回测与实盘共用同一代码路径。

### DataLayer — 统一数据接口

```python
from core.data_layer import get_data_layer, BacktestDataLayer

# 生产模式（调用腾讯/新浪 API，带 TTL 缓存）
dl = get_data_layer()
bars = dl.get_bars('600519.SH', days=120)   # → pd.DataFrame
quote = dl.get_realtime('600519.SH')        # → Quote

# 回测模式（防前视偏差）
dl = BacktestDataLayer(data={'600519.SH': df})
dl.set_date('2024-06-01')
bars = dl.get_bars('600519.SH', days=30)   # 只返回 2024-06-01 及之前的数据
```

### FactorPipeline — 多因子流水线

```python
from core.factor_pipeline import FactorPipeline

pipeline = FactorPipeline()
pipeline.add('RSI',  weight=0.5, params={'period': 14, 'buy_threshold': 25})
pipeline.add('MACD', weight=0.3)
pipeline.add('ATR',  weight=0.2)

result = pipeline.run(symbol='510310.SH', data=df)
print(result.combined_score)    # float，正=偏多，负=偏空
print(result.dominant_signal)  # 'BUY' / 'SELL' / 'HOLD'
print(result.signals)          # List[Signal]，按强度降序
```

### StrategyRunner — 策略主循环

```python
from core.strategy_runner import StrategyRunner, RunnerConfig

cfg = RunnerConfig(
    symbols=['510310.SH', '600900.SH', '300750.SZ'],
    pipeline=pipeline,
    interval=300,      # 每 5 分钟扫描一次
    dry_run=True,      # True=仅打印，False=真实下单
    signal_threshold=0.5,
)
runner = StrategyRunner(cfg, data_layer=dl)

# 单轮扫描（回测/调试）
results = runner.run_once()

# 生产主循环（Ctrl+C 退出）
runner.run_loop()
```

### PortfolioRiskChecker — 组合层风控

```python
from core.portfolio_risk import PortfolioRiskChecker, PortfolioSnapshot

checker = PortfolioRiskChecker(
    var_limit=0.03,         # 单日 VaR ≤ 3%（95% 置信度）
    max_sector_weight=0.30, # 行业集中度 ≤ 30%
    max_drawdown=0.15,      # 最大回撤 ≤ 15%
    max_correlation=0.85,   # 持仓相关性预警阈值
)

snap = PortfolioSnapshot(
    positions={'510310.SH': 30000, '600900.SH': 20000},
    equity=100000,
    peak_equity=105000,
    sector_map={'510310.SH': '宽基ETF', '600900.SH': '电力'},
    returns={'510310.SH': returns_series_a, '600900.SH': returns_series_b},
)

result = checker.check_before_buy(snap)
if not result.passed:
    print('风控拒绝:', result.reason)
```

### TradingConfig — 统一配置

```bash
# 切换环境
export TRADING_ENV=live    # 真实下单、关闭 dry_run
export TRADING_ENV=dev     # 模拟运行（默认）
```

```python
from core.config import load_config

cfg = load_config()                   # 读 config/trading.yaml + TRADING_ENV
cfg = load_config(env='live')         # 强制 live

print(cfg.portfolio.capital)          # 20000
print(cfg.risk.max_drawdown)          # 0.15
print(cfg.runner.dry_run)             # True/False
print(cfg.strategy('RSI').symbol)     # '510310.SH'
print(cfg.live_symbol_list())         # ['510310.SH', '600900.SH', ...]
```

---

## 五维选股系统 V2

| 维度 | 权重 | 数据来源 | 说明 |
|------|------|---------|------|
| 新闻热度 | 15% | 东方财富快讯 | 含糊表述（有望/或将）降权 |
| 板块行情 | 35% | 东方财富 BK 涨跌幅 | 北向资金排名优选 |
| 资金流向 | 25% | 北向/主力净流入 | 持续流入 > 脉冲 |
| 技术趋势 | 15% | 成分股涨跌信号 | 板块内一致性 |
| 一致性   | 10% | 板块内联动强度 | 成分股共振度 |

降级机制：API 失败时自动切换宽基 ETF（沪深300、创业板、酒 ETF）。

---

## 市场环境与策略参数

系统自动识别四种市场环境，参数自适应：

| 环境 | 判断条件 | RSI | ATR 阈值 | 止盈 | 止损 |
|------|---------|-----|---------|------|------|
| **BULL** | MA20>MA60 且指数>MA20 | 25/65 | 0.90 | 20% | 5% |
| **BEAR** | 指数<MA60 或均线空头 | 40/70 | 0.80 | 15% | 5% |
| **VOLATILE** | ATR ratio > 0.90 | 30/60 | 0.80 | 25% | 5% |
| **CALM** | ATR ratio ≤ 0.85 | 25/65 | 0.85 | 20% | 5% |

ATR Ratio = 当前 ATR(14) / 近 20 日 ATR 最高值  
ATR Ratio > 0.85 时不开新仓（高波动期 RSI 均值回归失效）

---

## 信号类型

| 信号 | 含义 | 触发动作 |
|------|------|---------|
| `RSI_BUY` / `RSI_SELL` | RSI 超买超卖 + 15min 二次确认 | 满足环境参数 → 开仓/平仓 |
| `WATCH_BUY` / `WATCH_SELL` | RSI 极端区域观察 | 仅推送，不交易 |
| `LIMIT_UP` / `LIMIT_DOWN` | 涨跌停检测 | 风险屏蔽 |
| `CHANDELIER_LONG` | Chandelier Exit（3×ATR） | 移动止盈 |
| `北向持续共振` | 北向连续 3 日 > 50 亿 | 强化买入信号 |
| `北向脉冲` | 单日 > 100 亿 | 辅助确认 |

---

## 回测框架

```bash
# RSI 参数网格搜索
python scripts/quant/backtest_cli.py single 600900.SH --rsi 25 75 --days 500

# Walk-Forward 滚动验证
python scripts/quant/backtest_cli.py wf 600900.SH

# 多策略对比
python scripts/quant/backtest_cli.py compare 510310.SH

# 环境自适应 vs 固定参数
python scripts/quant/backtest_cli.py regime-wfa 510310.SH

# 压力测试（股灾/贸战/封控）
python scripts/quant/backtest_cli.py crash-test 510310.SH
```

验收标准：Sharpe ≥ 0.5，MaxDD ≤ 20%，正收益窗口 ≥ 60%

---

## 策略插件

```python
from strategies import load_strategy

strat = load_strategy('RSI', {'rsi_buy': 30, 'rsi_sell': 65}, symbol='600519.SH')
result = strat.evaluate(kline_data, i=-1)
```

自定义策略继承 `strategies.base.BaseStrategy`，放入 `strategies/` 目录后即可通过 `load_strategy()` 加载，无需修改核心代码。

---

## 运行测试

```bash
# 全量测试（273 个）
python tests/run_tests.py

# 单独运行各阶段测试
python tests/test_data_layer.py       # Phase 1: DataLayer (48)
python tests/test_factor_pipeline.py  # Phase 2: FactorPipeline (38)
python tests/test_strategy_runner.py  # Phase 3: StrategyRunner (32)
python tests/test_portfolio_risk.py   # Phase 4: PortfolioRisk (34)
python tests/test_config.py           # Phase 5: Config (33)
```

---

## 已知限制

- 盘中信号基于日线 RSI 近似，非真实分钟级数据
- 真实券商接入待完成（Futu/Tiger/IBKR 适配器已有接口骨架）
- 北向资金数据单位需进一步验证
- `PortfolioRiskChecker` 的 VaR 使用历史模拟法，未来可替换为参数法或 Monte Carlo

---

## 免责声明

本系统仅供研究与教育目的。回测结果不代表未来收益，所有数据仅供参考，不构成投资建议。

---

## 协议

MIT License
