# A 股量化交易系统

A 股 + 港股量化研究与全自动模拟交易平台。

---

## 产品定位

**单租户准生产实盘 + 研究台,虚拟模拟盘(无真实券商接入)**。

- 👤 **谁用**:个人/小团队 operator 自用,管 50–200 万规模虚拟资金
- 🎯 **解决什么**:把"研究 → 验证 → 模拟运营"三步打通,提供一个可信的闭环
- 🚫 **不做什么**:
  - 不接入真实券商(Futu/IBKR 等实盘接口仅保留代码雏形,不再维护)
  - 不做多用户/多租户隔离
  - 不做毫秒级高频
  - 不做付费另类数据

**单 OS 单进程**约束:同一台机器同一时刻只能跑一个实例(由 OS 级 PID 锁保护)。
未来若需横向扩展,会切到 Docker microservices 形态;本仓库当前为这一步打基础。

**关键运行模式**:`all`(默认,API + Worker 同进程) / `api` / `worker`(为未来分离做准备)。

---

## 三类使用场景(垂直切片)

| 场景 | 角色 | 入口 |
|---|---|---|
| 🤖 自动化日常 | operator | `backend/main.py` 默认 mode=all,Scheduler 驱动 |
| 👁️ 交互/分析 | trader/PM | Streamlit `streamlit_app.py` → backend API |
| 🔬 研究/回测 | researcher | `scripts/quant/backtest_cli.py` / `core.use_cases.backtest` |

所有场景共享同一组 **use case 函数**(`core/use_cases/`),业务逻辑只此一份。

---

## 系统能力

| 功能 | 说明 |
|------|------|
| 多因子策略 | 10+ 因子（技术/基本面/宏观），DynamicWeightPipeline IC 动态加权 |
| 动态选股 | 每日 15:10 基于板块行情+资金流向+新闻情绪自动选股 |
| 回测引擎 | 事件驱动 Walk-Forward 回测 |
| 盘中监控 | 5 分钟轮询，RSI 二次确认、止盈止损、飞书推送 |
| 风控体系 | PreTrade / InTrade / PostTrade 三层，含 CVaR + Monte Carlo |
| 早晚报 | 盘前选股 + 盘后持仓快照，每日 09:30 / 16:00 飞书推送 |

---

## 快速启动

```bash
# 安装
conda create -n quant-trading python=3.11
conda activate quant-trading
pip install -r requirements.txt

# 启动（API + Scheduler + 盘中监控全开）
cd backend
python main.py --mode both --port 5555

# 或用 systemd 守护进程
systemctl --user enable quant-trading-backend.service
systemctl --user start quant-trading-backend.service

# 手动触发选股分析
curl -X POST http://127.0.0.1:5555/analysis/run

# 查看 API 文档
http://127.0.0.1:5555/docs
```

---

## 项目结构

```
├── core/                          # 核心业务逻辑
│   ├── data_gateway/              # 统一数据网关（多 provider 路由 + 缓存）
│   │   ├── gateway.py             # DataGateway 主入口
│   │   ├── health.py              # 健康度跟踪器
│   │   ├── cache.py               # 内存缓存
│   │   ├── merge.py               # 字段级多源合并
│   │   ├── schemas.py             # 数据类型定义
│   │   └── providers/              # provider 实现
│   │       ├── tencent.py         # 腾讯行情（主选）
│   │       ├── sina.py            # 新浪行情
│   │       ├── eastmoney.py       # 东方财富（板块/资金流）
│   │       ├── akshare.py         # AkShare（最终备灾）
│   │       └── yfinance.py        # YFinance（美股/港股指数）
│   ├── data_layer.py             # 数据层外观（转发到 DataGateway）
│   ├── pipeline_factory.py        # 因子流水线
│   ├── strategy_runner.py         # 策略运行器
│   ├── event_bus.py               # 事件总线
│   ├── regime.py                  # 市场状态检测
│   ├── risk_engine.py             # 风控引擎
│   ├── factors/                   # 因子实现
│   └── brokers/                   # 券商适配层
├── backend/
│   ├── main.py                    # 服务入口（Scheduler 在此）
│   ├── api.py                     # HTTP API
│   └── services/                  # 持久化服务
├── docs/
│   ├── ARCHITECTURE_TARGET.md     # 目标架构(本次重构基线)
│   ├── ARCHITECTURE_CURRENT.md    # 当前架构(诚实记录,逐步收敛到 TARGET)
│   ├── CHANGELOG.md               # 变更日志
│   └── EVALUATION.md              # 系统评估
└── params.json                    # 策略参数
```

---

## 数据网关

全系统对外网数据的唯一出口，按 (provider × capability) 路由：

- **可合并数据**（实时行情、基本面）：并发问 top-K provider，字段级互补合并
- **不可合并数据**（K 线、板块、北向等）：按健康度降序逐个尝试
- **熔断器**：失败累计触发硬开关，保护系统不被单一源拖垮
- **缓存**：按数据类型设 TTL（30s ~ 24h），避免重复请求

---

## 每日运行流程

```
09:30  — 选股 → watchlist → RSI 信号扫描 → 模拟下单 → 飞书早报
09:31  — 盘中监控启动，每 5 分钟扫 RSI 金叉/死叉
15:00  — 收盘晚报（持仓快照 + 收益）
15:10  — 日终 DynamicStockSelector 选股分析
15:30  — CVaR + 蒙特卡洛压力测试
15:45  — TCA 反馈闭环
16:00  — 每日运营报告 → 飞书
```

---

## 免责声明

本系统仅供研究与模拟交易验证,不构成投资建议。**不接入真实券商,所有"下单"
均为虚拟模拟盘记账行为**。
