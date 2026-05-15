# 目标架构(本次重构基线)

> 这份文档描述"重构完成后系统应该是什么样",作为所有 PR 取舍的对照基线。
> 当前实际状态详见 `ARCHITECTURE_CURRENT.md`,会随每个 commit 逐步收敛到本文档。

---

## 产品定位回顾

**单租户准生产实盘 + 研究台,虚拟模拟盘**。

- 单进程单 OS,OS 级 PID 锁保护
- 未来微服务化的基础(代码上分离 API / Worker,本次仍合进程跑)
- 数据/状态/配置各 1 个权威入口

---

## 8 层水平架构

```
┌───────────────────────────────────────────────────────────────────┐
│  Application Layer (薄)                                          │
│  ┌──────────────┬──────────────┬──────────────┬──────────────┐  │
│  │ Web UI       │ REST API     │ Scheduled    │ CLI          │  │
│  │ (Streamlit)  │ (Flask)      │ Jobs         │ (research)   │  │
│  │ streamlit_   │ backend/     │ scripts/     │ scripts/     │  │
│  │  app.py +    │  api.py      │ morning_     │  quant/      │  │
│  │  pages/      │  瘦身后       │ runner.py    │  *_cli.py    │  │
│  └──────────────┴──────────────┴──────────────┴──────────────┘  │
│         ↓ 全部走同一组 Use Case 函数 (core/use_cases/)            │
├───────────────────────────────────────────────────────────────────┤
│  Use Case Layer (新增)                                           │
│  core/use_cases/                                                  │
│  • analyze_stock.py                                               │
│  • intraday_signals.py                                            │
│  • morning_workflow.py                                            │
│  • backtest.py                                                    │
│  • compose_portfolio.py                                           │
│                                                                   │
│  约定: 输入/输出皆 dataclass,业务逻辑只此一份                     │
├───────────────────────────────────────────────────────────────────┤
│  Domain Services Layer  (现 core/* 的清理后版本)                 │
│  ┌────────┬─────────┬─────────┬─────────┬─────────┬─────────┐    │
│  │ Model  │ Regime  │ Risk    │Portfolio│Execution│Research │    │
│  │factors │         │         │         │         │         │    │
│  │pipeline│         │         │         │         │         │    │
│  └────────┴─────────┴─────────┴─────────┴─────────┴─────────┘    │
│                                                                   │
│  ❌ 不再有 scripts/quant/regime_detector.py 等并行实现             │
├───────────────────────────────────────────────────────────────────┤
│  Operations Layer                                                 │
│  ┌──────────────┬──────────────┬──────────────┐                  │
│  │ Scheduler    │ Intraday     │ Alerts +     │                  │
│  │              │ Monitor      │ Reports      │                  │
│  │ (cron-style) │ (拆分后)     │ (飞书等)     │                  │
│  └──────────────┴──────────────┴──────────────┘                  │
├───────────────────────────────────────────────────────────────────┤
│  Persistence Layer  (单一权威源)                                  │
│  ┌──────────────────────┬────────────────────────┐               │
│  │ data/state.db        │ data/*.parquet         │               │
│  │ (SQLite, 单库)        │ (时序: bars/sentiment)  │               │
│  │ portfolio + orders   │                        │               │
│  │ + signals + audit    │                        │               │
│  │ + wf_results         │                        │               │
│  └──────────────────────┴────────────────────────┘               │
├───────────────────────────────────────────────────────────────────┤
│  Config Layer                                                     │
│  config/trading.yaml + .env  → core/config.py:Settings           │
│  ❌ 不再有 params.json / live_params.json / trading_mode.json /   │
│      regime_today.json 多套并存                                   │
├───────────────────────────────────────────────────────────────────┤
│  Data Gateway ✅ (已就绪)                                         │
│  core/data_gateway/   唯一对外网出口                              │
└───────────────────────────────────────────────────────────────────┘
```

---

## 3 个垂直切片(产品视角)

### 切片 1: 🤖 自动化日常 (Operator)

```
backend/main.py (mode=all)
  ├─ Scheduler (内部线程)
  │   ├─ 09:30 → 调 use_cases.morning_workflow
  │   ├─ 09:31 → 启动 IntradayMonitor
  │   ├─ 15:00 → 调 use_cases.eod_report
  │   └─ 15:10 → 调 use_cases.run_analysis
  │
  ├─ IntradayMonitor (内部线程)
  │   └─ 每 5min → 调 use_cases.generate_intraday_signals
  │              → 风控 → SimulatedBroker(虚拟下单)
  │              → 告警(飞书)
  │
  └─ Flask API (供 UI / 外部触发)
```

### 切片 2: 👁️ 交互/分析 (Trader/PM)

```
Streamlit (pages/*) 
  └─ HTTP → backend API
            └─ Flask 端点 (≤25 行/端点)
                └─ 调对应 use_case
                    └─ 返回 dataclass.to_dict()
```

### 切片 3: 🔬 研究/回测 (Researcher)

```
CLI: scripts/quant/backtest_cli.py
  └─ 调 use_cases.backtest(BacktestRequest)
      └─ core/backtest_engine.py
          └─ Data Gateway (read parquet snapshots)
```

---

## 进程模型

### 当前(本次重构后):单进程,代码分离

```
$ python backend/main.py --mode all
  ┌─────────────────────────────┐
  │ Python Process              │
  │  ┌────────┬──────────────┐  │
  │  │ Flask  │ Scheduler +  │  │
  │  │ API    │ IntradayMon  │  │
  │  └────────┴──────────────┘  │
  │  ↓ 共享 SQLite + Settings   │
  └─────────────────────────────┘
       ↑ OS PID lock(单实例)
```

### 未来(微服务,本次仅打基础)

```
docker-compose.yml
  ├─ quant-api (mode=api)        # Flask only
  ├─ quant-worker (mode=worker)  # Scheduler + IntradayMonitor only
  ├─ quant-ui (Streamlit)
  └─ quant-db (Postgres,SQLite 迁移目标)
```

---

## 命名约定

### use_cases 包结构

```python
# core/use_cases/analyze_stock.py
from dataclasses import dataclass

@dataclass
class AnalyzeStockRequest:
    symbol: str
    market: str  # 'A' / 'HK'
    ...

@dataclass
class AnalyzeStockResponse:
    ...

def analyze_stock(req: AnalyzeStockRequest) -> AnalyzeStockResponse:
    """Use case 实现。所有 caller (API / UI / CLI / Scheduler) 走这里。"""
    ...
```

### API 端点约定

```python
@app.route('/analysis/stock/a', methods=['POST'])
def analyze_a_stock_endpoint():
    _check_auth_and_rate_limit()
    req = AnalyzeStockRequest.from_body(request.get_json())
    resp = analyze_stock(req)  # ← use case 调用
    return ok(resp.to_dict())
```

理想端点 ≤ 25 行。

### Settings 约定

```python
# core/config.py
class Settings(BaseModel):
    broker: Literal['simulated'] = 'simulated'
    api_port: int = 5555
    ...

def get_settings() -> Settings:
    """Pydantic 加载 YAML + .env 覆盖。"""
    ...
```

---

## 反模式(本次要消灭)

1. ❌ Flask 端点直接做业务:`backend/api.py` 1988 行,平均 35 行/端点
2. ❌ 同一概念 N 处实现:Regime / 选股 / 新闻打分 / 信号生成
3. ❌ UI 直连数据源:`streamlit_app.py` 内 `qt.gtimg.cn` 直连
4. ❌ 越层导入:`backend/services` 内 `qt.gtimg.cn` 绕过 Gateway
5. ❌ 配置散落:5+ 个 JSON 各管一摊
6. ❌ 状态散落:2+ 个 SQLite + N 个 JSON
7. ❌ 业务死耦在进程内:Flask / Scheduler / IntradayMonitor 拆不开
8. ❌ 一文件超长:`intraday_monitor.py` 1831 行,`api.py` 1988 行,`streamlit_app.py` 1850 行

---

## 验收指标(本次重构完成时)

| 维度 | 当前 | 目标 |
|---|---|---|
| `backend/api.py` 平均端点行数 | ~35 | ≤25 |
| 最长单文件行数 | 1988 | ≤500 |
| 重复领域实现 | 6 类 ×N 份 | 0 重复 |
| 配置文件数 | 5 | 1(YAML)+ .env |
| 状态 DB 数 | 2 | 1(state.db) |
| 单 OS 多开 | 允许(危险) | OS lock 禁止 |
| UI 绕过 backend | 至少 2 处 | 0 |
| use case 模块数 | 0 | ≥ 5 |
