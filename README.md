# A 股量化交易系统

A 股 + 港股量化研究与全自动模拟交易平台，支持多因子选股、机器学习价格预测、算法订单执行、组合优化、实时盘中监控与告警推送。

---

## 系统能力

### 核心功能

| 功能 | 说明 |
|------|------|
| **多因子策略** | 22 个因子（价格动量、技术面、基本面、情绪、宏观），支持动态 IC 加权 |
| **回测引擎** | 事件驱动 Walk-Forward 回测，防过拟合验证 |
| **机器学习框架** | LightGBM 价格预测 + Walk-Forward 训练，实盘前充分验证 |
| **组合优化** | MVO / Black-Litterman / 风险平价 / 最大分散化 |
| **算法执行** | VWAP / TWAP，减小市场冲击 |
| **盘中监控** | 5 分钟轮询，涨跌停/止盈止损/新仓信号检测，飞书实时推送 |
| **风控体系** | PreTrade / InTrade / PostTrade 三层，含 CVaR + Monte Carlo 压力测试 |
| **港股打新分析** | IPO Stars — 多源数据交叉验证，三档限价单建议（feature/ipo-stars 分支） |
| **早晚报推送** | 盘前市场概况 + 盘后绩效归因，飞书自动推送 |
| **可观测性 API** | Prometheus 格式监控指标、参数 CRUD、实时行情查询 |

### 支持市场

- **A 股**（沪深北交所）— 全自动模拟交易
- **港股**— 实时行情 + 模拟交易
- **美股**— 数据源就绪（IBKR 券商适配层stub）

---

## 快速启动

### 1. 环境准备

```bash
# 使用 conda
conda create -n quant-trading python=3.11
conda activate quant-trading
pip install -r requirements.txt
```

### 2. 配置

```bash
cp params.json.example params.json
# 编辑 params.json 中的券商参数和告警渠道
```

### 3. 启动后端

```bash
cd backend
python main.py --mode both --port 5555
# --mode api      仅启动 HTTP API
# --mode scheduler 仅启动定时任务
# --mode both     两者都启动
```

### 4. 触发一次分析

```bash
curl -X POST http://127.0.0.1:5555/analysis/run
```

### 5. 查看 API 文档

```
http://127.0.0.1:5555/docs
```

---

## 项目结构

```
a-quantitative-trading-xh/
├── core/                          # 核心业务逻辑
│   ├── backtest_engine.py         # 事件驱动回测引擎
│   ├── strategy_runner.py         # 实盘策略运行器（asyncio）
│   ├── event_bus.py               # 事件总线
│   ├── data_layer.py              # 数据抽象层
│   ├── portfolio.py               # 组合管理
│   ├── pipeline_factory.py        # 因子流水线工厂
│   ├── regime.py                  # 市场状态检测
│   ├── factors/                   # 22 个因子实现
│   ├── strategies/                # 独立策略模块
│   ├── ml/                        # ML 预测框架
│   └── brokers/                   # 券商适配层（Futu/IBKR/Tiger）
├── backend/
│   ├── main.py                    # 服务入口
│   ├── api.py                     # Flask HTTP API（30+ 端点）
│   └── services/
│       ├── portfolio.py           # SQLite 组合持久化
│       ├── alert_manager.py        # 飞书/钉钉推送
│       └── north_bound.py          # 北向资金
├── scheduler/
│   ├── morning_runner.py          # 盘前流程编排
│   ├── afternoon_report.py        # 盘后报告
│   └── ipo_scanner.py             # 港股打新扫描（IPO Stars）
├── scripts/
│   ├── walkforward_job.py        # Walk-Forward 参数验证
│   ├── bayesian_optimize.py       # 贝叶斯参数优化
│   └── quant/                     # 研究脚本
├── tests/                         # 测试套件
└── params.json                    # 策略参数配置
```

---

## 核心模块

### 多因子系统

```python
from core.pipeline_factory import make_a_stock_pipeline

pipeline = make_a_stock_pipeline(symbol="000001.SH")
pipeline.run()  # 返回 composite_score 和 signal
```

### 回测

```python
from core.backtest_engine import BacktestEngine
from core.strategies.rsi_strategy import RSIStrategy

engine = BacktestEngine(
    strategy=RSIStrategy(),
    start="20200101",
    end="20251231",
    initial_cash=1_000_000,
)
result = engine.run()
```

### 港股打新分析

```bash
# 手动分析一只新股（feature/ipo-stars 分支）
curl -X POST "http://127.0.0.1:5555/ipo/analyze?stock_code=01236"

# 查看近期招股列表
curl http://127.0.0.1:5555/ipo/upcoming
```

---

## 运行测试

```bash
# 方式一
python tests/run_tests.py

# 方式二
pytest tests/ -v
```

---

## 免责声明

本系统仅供研究与模拟交易验证之用，不构成任何投资建议。实盘交易存在亏损风险，请谨慎评估。
