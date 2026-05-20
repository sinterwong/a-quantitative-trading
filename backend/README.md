# backend

HTTP API + 持久化服务。

## 运行

```bash
python -m quant_app.main --mode all      # API + Scheduler + Monitor + Runner
python -m quant_app.main --mode api      # 仅 HTTP API
python -m quant_app.main --mode worker   # 仅 Scheduler / Monitor / Runner
```

进程层防多开：`backend/.quant-backend.pid` 文件锁，已有实例运行时新进程直接退出（`fcntl.flock`）。

## systemd

```bash
systemctl --user start  quant-trading-backend.service
systemctl --user enable quant-trading-backend.service
systemctl --user status quant-trading-backend.service
journalctl --user -u quant-trading-backend.service -f
```

## Scheduler 每日任务

| 时间 | 任务 | 说明 |
|---|---|---|
| 09:30 | morning_runner | 选股 → watchlist → RSI 信号 → 模拟下单 → 飞书早报 |
| 09:31 | IntradayMonitor | 启动盘中 5 分钟轮询（持续到收盘） |
| 15:00 | afternoon_report | 收盘晚报（持仓快照 + 收益 → 飞书） |
| 15:10 | /analysis/run | 日终选股（DynamicStockSelector） |
| 15:30 | daily_risk_report | CVaR + 蒙特卡洛压力测试 |
| 15:45 | daily_tca | TCA 反馈闭环 |
| 16:00 | daily_ops_report | 每日运营报告 → 飞书 |

非交易日（周末 / 节假日）跳过。触发窗口 ±60 秒，每日同一任务只触发一次。

## API 端点

共 55 个端点，按资源分散在 `backend/api_routes/` 下：

| Blueprint | 端点 |
|---|---|
| `orders` | `/orders/submit` `GET /orders/recent` `GET /orders/pending` `POST /orders/<id>/cancel` |
| `portfolio` | `/positions` `/cash` `/trades` `/signals` `/portfolio/summary` `/portfolio/positions` `/portfolio/cash` `/portfolio/daily` |
| `trades_signals_params` | `/trades` `/signals` `/params` `/params/<symbol>` |
| `watchlist_alerts` | `/watchlist` `/watchlist/add` `DELETE /watchlist/<symbol>` `PATCH /watchlist/<symbol>` `/alerts/*` |
| `data` | `/data/daily` `/data/realtime` `/data/fund_flow` `/data/status` |
| `market` | `/northbound/flow` `/performance/summary` `/macro/<indicator>` `/fundamentals/<symbol>` `/market/status` `/data/news/<symbol>` |
| `analysis` | `/analysis/*` `/analysis/monthly` `/analysis/monthly/snapshot` `/analysis/monthly/history` |
| `research` | `/backtest/run` `/portfolio/compose` `/wfa/history` `/wfa/summary` |
| `ops` | `/trading/mode` `/monitor/status` `/risk/status` `/metrics` `/llm/analyze` |

OpenAPI spec 在 `backend/openapi.json`（由 `scripts/generate_openapi.py` 自动生成），浏览器打开 `/docs` 查看交互式文档。

常用端点速查：

### 组合与持仓

| Method | Path |
|---|---|
| GET | `/positions` |
| GET | `/cash` |
| GET | `/trades` |
| GET | `/signals` |
| GET | `/portfolio/summary` |
| POST | `/portfolio/positions` |
| POST | `/portfolio/cash` |
| GET / POST | `/portfolio/daily` |

### 订单

| Method | Path |
|---|---|
| POST | `/orders/submit` |
| GET | `/orders/recent` |
| GET | `/orders/pending` |
| POST | `/orders/<id>/cancel` |

`POST /orders/submit` 支持 `Idempotency-Key` header（客户端生成 UUID）。同 key 24h 内重放返回原响应；同 key 不同 payload 返回 HTTP 422。

### 分析

| Method | Path |
|---|---|
| POST | `/analysis/run` |
| POST | `/analysis/stock/a` |
| POST | `/analysis/stock/hk` |
| POST | `/analysis/sector_rotation` |
| POST | `/analysis/pairs_trading` |
| POST | `/analysis/sector/compare` |
| GET | `/analysis/health` |
| GET | `/analysis/status` |
| GET | `/analysis/monthly` |
| POST | `/analysis/monthly/snapshot` |
| GET | `/analysis/monthly/history` |

### Watchlist / Params

| Method | Path |
|---|---|
| GET | `/watchlist` |
| POST | `/watchlist/add` |
| DELETE | `/watchlist/<symbol>` |
| PATCH | `/watchlist/<symbol>` |
| GET | `/params` |
| GET / PATCH | `/params/<symbol>` |

### 数据

| Method | Path |
|---|---|
| GET | `/data/daily/<code>` |
| GET | `/data/realtime/<symbol>` |
| GET | `/data/news/<symbol>` |
| GET | `/data/macro/<indicator>` |
| GET | `/data/fund_flow` |
| GET | `/fundamentals/<symbol>` |

### 可观测性 / 运维

| Method | Path |
|---|---|
| GET | `/health` |
| GET | `/docs` |
| GET | `/metrics`（Prometheus） |
| GET | `/monitor/status` |
| GET | `/risk/status` |
| GET | `/market/status` |
| PUT | `/trading/mode` |

## 调用示例

```bash
curl http://127.0.0.1:5555/portfolio/summary
curl -X POST http://127.0.0.1:5555/analysis/run
curl -X POST http://127.0.0.1:5555/orders/submit \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: $(uuidgen)" \
  -d '{"symbol":"600519.SH","direction":"BUY","shares":100,"price":1820.0}'
```

带鉴权：

```bash
curl -H "X-API-Key: $TRADING_API_KEY" http://127.0.0.1:5555/portfolio/summary
```

## 持久化

| 数据 | 位置 |
|---|---|
| 组合 / 订单 / 信号 / 审计 / 幂等键 | `data/state.db`（SQLite + WAL，`busy_timeout=5000`） |
| K 线 / 基本面 / 资金流 | `data/cache/data_gateway/*.parquet`（L2 缓存，重启不丢） |
| 情感分数 / 新闻 | `data/sentiment/` / `data/news_cache/` |
| Walk-Forward 结果 | `wf_results.db` |

`core/state_db.state_db_path()` 三级回退：`QUANT_STATE_DB` env > `data/state.db` > legacy `backend/services/portfolio.db`。

并发模型：每次 `PortfolioService.get_cursor()` 在当前线程新建连接（不跨线程共享），进程内写操作经 `_WRITE_LOCK` 串行化。

## services/ 子模块

| 子目录 / 文件 | 说明 |
|---|---|
| `intraday/` | IntradayMonitor 5 Mixin |
| `llm/` | MiniMax / DeepSeek / Kimi provider |
| `fetchers/` | Provider 接入层（akshare / sina / tencent / tencent_hk） |
| `channels/` | 告警通道（feishu / discord / telegram） |
| `ipo_stars/` | 港股打新扫描 |
| `portfolio.py` | 持仓服务 |
| `signals.py` | 信号生成 + 参数管理 |
| `watchlist.py` | 自选股 |
| `alert_history.py` | 告警历史 |
| `performance.py` | 月度报告 |
| `northbound.py` | 北向资金 |
| `fund_flow.py` | 资金流 |
