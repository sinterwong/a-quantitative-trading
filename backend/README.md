# Backend Service

持久化后端服务，提供 HTTP API 和定时任务调度。

## 启动

```bash
cd backend
python main.py --mode both --port 5555

# 仅 HTTP API
python main.py --mode api --port 5555

# 仅定时任务
python main.py --mode scheduler
```

## API 端点

### 组合与持仓

| Method | Path | 说明 |
|--------|------|------|
| GET | `/positions` | 当前持仓 |
| GET | `/cash` | 可用资金 |
| GET | `/trades` | 近期交易记录 |
| GET | `/signals` | 近期信号 |
| GET | `/portfolio/summary` | 组合完整快照 |
| GET | `/portfolio/daily` | 每日汇总 |
| POST | `/portfolio/positions` | 插入/更新持仓 |
| POST | `/portfolio/cash` | 设置资金 |

### 订单

| Method | Path | 说明 |
|--------|------|------|
| POST | `/orders/submit` | 提交订单 |
| GET | `/orders` | 查询订单列表 |
| GET | `/orders/<id>` | 查询单个订单 |
| POST | `/orders/<id>/cancel` | 取消订单 |

### 分析与回测

| Method | Path | 说明 |
|--------|------|------|
| POST | `/analysis/run` | 触发每日分析 |
| GET | `/analysis/status` | 分析状态 |
| POST | `/backtest` | 运行回测 |

### 可观测性

| Method | Path | 说明 |
|--------|------|------|
| GET | `/metrics` | Prometheus 格式指标 |
| GET | `/health` | 健康检查 |
| GET | `/params` | 查询策略参数 |
| PUT | `/params` | 更新策略参数 |

### 港股打新分析（feature/ipo-stars）

| Method | Path | 说明 |
|--------|------|------|
| POST | `/ipo/analyze?stock_code=xxx` | 分析单只新股 |
| GET | `/ipo/upcoming` | 近期招股列表 |
| GET | `/ipo/history/<code>` | 历史分析记录 |

## 示例

```bash
# 查询组合
curl http://127.0.0.1:5555/portfolio/summary

# 提交订单
curl -X POST http://127.0.0.1:5555/orders/submit \
  -H "Content-Type: application/json" \
  -d '{"symbol":"000001.SH","direction":"BUY","shares":200,"price":12.50}'

# 触发分析
curl -X POST http://127.0.0.1:5555/analysis/run

# 查看 API 文档
open http://127.0.0.1:5555/docs
```

## 数据持久化

所有组合数据存储在 `backend/services/portfolio.db`（SQLite）。
