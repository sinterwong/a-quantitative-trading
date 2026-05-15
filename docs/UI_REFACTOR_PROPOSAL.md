# Web UI 重构建议(下个周期)

> 本文档由 P4-1 阶段二拆分后产出,记录当前 UI 与产品定位的契合度评估、
> 已知架构债以及下一周期(P5 以后)的具体重构建议。
>
> 当前实际状态见 [`ARCHITECTURE_CURRENT.md`](./ARCHITECTURE_CURRENT.md)。

## 产品定位回顾

> **单租户准生产实盘 + 研究台,虚拟模拟盘(无真实券商接入)**。
>
> 三类使用场景:
> - 🤖 **自动化日常**:operator 角色 → `backend/main.py --mode all` Scheduler 驱动
> - 👁️ **交互/分析**:trader/PM 角色 → Streamlit → backend API
> - 🔬 **研究/回测**:researcher 角色 → `scripts/quant/backtest_cli.py` + `core.use_cases.*`

## 阶段二已完成的拆分(本周期)

```
streamlit_app.py  1641 → 97 行(只剩入口路由)
streamlit_helpers.py  236 → 16 行 (backward-compat shim)
ui/
├── data.py                    237 行  Backend HTTP + cached loaders + 数据工具
├── components/
│   ├── __init__.py             10 行  公共渲染组件 re-export
│   └── layout.py               65 行  global_css / regime_badge / broker_badge
└── pages/
    ├── __init__.py             15 行
    ├── dashboard.py           123 行  📊 仪表盘
    ├── factor_workbench.py    181 行  🎯 因子工作台
    ├── ml_models.py           177 行  🤖 ML 模型
    ├── portfolio_optimization.py 300 行  ⚖️ 组合优化
    ├── signals_execution.py   238 行  📈 信号 & 执行
    ├── backtest.py            343 行  📉 回测验证
    └── monitoring.py          261 行  🏥 监控 & 告警
```

- 入口 97 行 ≪ TODO 原目标 ≤200 行
- 所有 page ≤ 343 行,全部 < TODO 原目标 ≤400 行
- streamlit_helpers.py 保留 shim,既有 `from streamlit_helpers import ...` 仍可用
- 全量回归 1472 测试通过

## 产品定位契合度评估

### ✅ 契合的部分

| Page | 角色 | 评价 |
|---|---|---|
| 📊 仪表盘 | operator | 准生产监控视图,完全契合 |
| 📈 信号 & 执行 | operator/trader | 模拟盘操作面板,契合 |
| 🏥 监控 & 告警 | operator | 策略健康/数据质量/AlertManager,契合 |
| 🎯 因子工作台 | researcher | 单股评分展示 OK,但越权调因子内部类 |

### ⚠️ 不契合(架构债)

**1. 研究类页面在 UI 主线程跑长任务**

| Page | 长任务 | 当前问题 |
|---|---|---|
| 🤖 ML 模型 | `WalkForwardTrainer.fit()` | 同步 1-3 分钟,浏览器刷新丢失 |
| ⚖️ 组合优化 | MVO + BL | 拉 N 个标的历史价 + 协方差估计 |
| 📉 回测验证 | `SensitivityAnalyzer` / `PaperTradeValidator` | subprocess 模式无进度回传 |

**2. UI 越权直连 `core/` 业务模块(共 21 处)**

```
core.factors.* / core.factor_pipeline / core.factor_registry      (factor_workbench)
core.ml.model_registry / core.ml.price_predictor                  (ml_models)
core.portfolio_optimizer / core.portfolio_allocator               (portfolio_optimization)
core.execution.* / core.brokers.simulated / core.oms / core.tca   (signals_execution)
core.walkforward / core.paper_trade_validator                     (backtest)
core.strategy_health / core.portfolio_risk / core.data_quality    (monitoring)
core.regime / core.alerting                                       (dashboard)
```

这违反 P4-2 精神(UI 应走 backend API)。重逻辑应包装为 use case → 暴露为 HTTP 端点。

**3. 没有任务队列**

长任务(训练/回测/优化)在 Streamlit 主线程同步跑,缺乏:
- 进度持久化(浏览器刷新即丢)
- 错误结构化(subprocess 模式只能 grep stdout)
- 取消/重试
- 准生产可靠性

---

## 下一周期(P5+)重构建议

### 阶段三 A:Use Case + Backend 端点改造(估计 1 周)

为每个研究类页面建立对应 use case + backend 端点。以 ML 训练为例:

**新增**:
- `core/use_cases/train_ml_model.py`(已有 `core/ml/*` 包裹一层)
- `backend/api.py`:
  ```python
  POST /ml/train     → 提交训练任务,返回 task_id
  GET  /ml/tasks/<id> → 查询进度 / 结果
  GET  /ml/registry  → 模型注册表 (替代 UI 直接读 data/ml_models/)
  GET  /ml/importance/<symbol> → 特征重要性
  ```

**改 UI**:`ui/pages/ml_models.py` 改用 `api_get('/ml/registry')` / `api_post('/ml/train', ...)`,删除 `from core.ml.*`。

类似改造:
| 页面 | 新 use case | 新端点(草案) |
|---|---|---|
| 🎯 因子工作台 | `evaluate_factors` (扩展现有 intraday_signals) | `GET /factors/list`, `POST /factors/evaluate` |
| 🤖 ML 模型 | `train_ml_model` (包裹 core.ml) | `POST /ml/train`, `GET /ml/registry`, `GET /ml/importance/<symbol>` |
| ⚖️ 组合优化 | `compose_portfolio`(P2-6 已有,扩展 BL/MVO) | `POST /portfolio/optimize`(已规划) |
| 📉 回测验证 | `run_backtest`(P2-5 已有,接 WFA/Sensitivity) | `POST /backtest/wfa`, `POST /backtest/sensitivity` |
| 🏥 监控 & 告警 | `strategy_health_metrics`(扩展 system_health) | `GET /risk/strategy_health`(替代直连 StrategyHealthMonitor) |

### 阶段三 B:异步任务队列(估计 3 天)

backend 新增 `core/use_cases/tasks.py` + 端点:

```python
POST /tasks       → {task_id, status="pending"}
GET  /tasks/<id>  → {task_id, status, progress, result, error}
DELETE /tasks/<id> → 取消
```

实现选择:
- 简单方案:Python `concurrent.futures.ThreadPoolExecutor` + 内存任务表
- 准生产方案:`Celery` + Redis 或 `RQ` (Redis Queue)
- 单机方案:`apscheduler` + SQLite 持久化(契合"单 OS 单进程"定位)

UI 模式:
```
submit = st.button('开始训练')
if submit:
    task_id = api_post('/ml/train', body).get('task_id')
    st.session_state['active_task'] = task_id

if (tid := st.session_state.get('active_task')):
    status = api_get(f'/tasks/{tid}')
    if status['status'] == 'pending':
        st.progress(status.get('progress', 0))
        # st.empty + 1s 刷新
    elif status['status'] == 'done':
        st.success('训练完成')
        # 渲染结果
```

### 阶段三 C:Streamlit 原生多页面(估计 0.5 天,可选)

把 `ui/pages/*.py` 改名为 `pages/01_Dashboard.py` 等,启用 Streamlit 原生
多页面导航(替代 `st.sidebar.radio`),sidebar 会自动渲染。

**前置条件**:阶段三 A/B 完成后做,避免在文件移动期间引入业务逻辑回归。

---

## 暂时不做的事项(scope guard)

| 事项 | 理由 |
|---|---|
| 把 UI 改成 SPA(React/Next.js) | 产品定位明确"维持 Streamlit",不在本仓库 |
| 加权限/角色 | 单租户,不需要 |
| 实时 WebSocket 推送 | Streamlit auto-refresh + cache TTL 已足够 |
| 移除 `_make_price_df_from_akshare` 直连 `DataLayer` | DataLayer 本身就是项目内部数据抽象,UI 通过它取 K 线属于正常依赖 |

---

## 行动 checklist(下个周期)

- [ ] 阶段三 A:7 页对应的 use case 全部建立 + backend 端点暴露
- [ ] 阶段三 A:OpenAPI 自动 regen(scripts/generate_openapi.py)
- [ ] 阶段三 A:`tests/test_api_contract.py` 覆盖新增端点
- [ ] 阶段三 B:`core/use_cases/tasks.py` 异步任务队列(单机即可)
- [ ] 阶段三 B:UI 长任务全部走 task_id 模式
- [ ] 阶段三 C(可选):`ui/pages/*` 改名走 Streamlit 原生多页面
- [ ] 删除 UI 内的 `from core.* import` 行(<= 1 行,只保留 DataLayer)
