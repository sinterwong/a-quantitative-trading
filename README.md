# A 股量化交易系统

A 股 + 港股量化研究与全自动模拟交易平台。支持多因子选股、机器学习预测、算法订单执行、组合优化、盘中监控与告警推送。

---

## 系统能力

| 功能 | 说明 |
|------|------|
| 多因子策略 | 22 个因子（价格动量、技术面、基本面、情绪、宏观），动态 IC 加权 |
| 回测引擎 | 事件驱动 Walk-Forward 回测 |
| 机器学习 | LightGBM 价格预测 + Walk-Forward 训练 |
| 组合优化 | MVO / Black-Litterman / 风险平价 / 最大分散化 |
| 算法执行 | VWAP / TWAP |
| 盘中监控 | 5 分钟轮询，涨跌停/止盈止损/新仓检测，飞书推送 |
| 风控体系 | PreTrade / InTrade / PostTrade 三层，含 CVaR + Monte Carlo |
| 港股打新 | IPO Stars — 多源数据交叉验证（feature/ipo-stars 分支） |
| 早晚报 | 盘前市场概况 + 盘后绩效归因 |
| 可观测性 | Prometheus 监控指标、参数 CRUD、实时行情查询 |

---

## 快速启动

```bash
# 安装
conda create -n quant-trading python=3.11
conda activate quant-trading
pip install -r requirements.txt

# 配置
cp params.json.example params.json
# 编辑 params.json

# 启动
cd backend
python main.py --mode both --port 5555

# 触发分析
curl -X POST http://127.0.0.1:5555/analysis/run

# 查看 API 文档
http://127.0.0.1:5555/docs
```

---

## 项目结构

```
├── core/                          # 核心业务逻辑
│   ├── backtest_engine.py         # 回测引擎
│   ├── strategy_runner.py         # 策略运行器
│   ├── event_bus.py               # 事件总线
│   ├── data_layer.py              # 数据层
│   ├── portfolio.py               # 组合管理
│   ├── pipeline_factory.py        # 因子流水线
│   ├── regime.py                  # 市场状态检测
│   ├── factors/                   # 22 个因子
│   ├── strategies/                # 策略模块
│   ├── ml/                        # ML 框架
│   └── brokers/                   # 券商适配层
├── backend/
│   ├── main.py                    # 服务入口
│   ├── api.py                     # HTTP API
│   └── services/                  # 持久化服务
├── docs/                          # 文档目录
├── scripts/                       # 运营脚本
├── tests/                         # 测试套件
└── params.json                    # 策略参数
```

---

## 核心模块

### 多因子系统

```python
from core.pipeline_factory import build_pipeline

pipeline = build_pipeline(symbol="000001.SH")
result = pipeline.run(symbol="000001.SH", data=df, price=current_price)
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

### 港股打新（feature/ipo-stars 分支）

```bash
curl -X POST "http://127.0.0.1:5555/ipo/analyze?stock_code=01236"
curl http://127.0.0.1:5555/ipo/upcoming
```

---

## 文档

- [系统架构](docs/ARCHITECTURE.md)
- [贡献指南](docs/CONTRIBUTING.md)
- [变更日志](docs/CHANGELOG.md)
- [系统评估](docs/EVALUATION.md)
- [开发任务](docs/TODO.md)

---

## 运行测试

```bash
pytest tests/ -v
```

---

## 免责声明

本系统仅供研究与模拟交易验证，不构成投资建议。实盘交易存在亏损风险，请谨慎评估。