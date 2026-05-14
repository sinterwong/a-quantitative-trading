# Backend Service

持久化后端服务，提供 HTTP API 和定时任务调度。

## 守护进程

推荐使用 systemd 管理后端进程，保证进程常驻、崩溃自启：

```bash
# 启动
systemctl --user start quant-trading-backend.service

# 开机自启
systemctl --user enable quant-trading-backend.service

# 查看状态
systemctl --user status quant-trading-backend.service

# 日志
journalctl --user -u quant-trading-backend.service -f
```

进程层防多开：`.backend.pid` 文件锁，已有实例运行时新进程直接退出。

## 启动模式

```bash
cd backend
python main.py --mode both --port 5555   # API + Scheduler + 盘中监控
python main.py --mode api --port 5555   # 仅 HTTP API
python main.py --mode scheduler          # 仅定时任务
```

## Scheduler 每日任务

| 时间 | 任务 | 说明 |
|------|------|------|
| 09:30 | morning_runner | 选股 → watchlist → RSI 信号 → 模拟下单 → 飞书早报 |
| 09:31 | IntradayMonitor | 启动盘中 5 分钟轮询（持续到收盘） |
| 15:00 | afternoon_report | 收盘晚报（持仓快照 + 收益 → 飞书） |
| 15:10 | /analysis/run | 日终选股分析（DynamicStockSelectorV2） |
| 15:30 | daily_risk_report | CVaR + 蒙特卡洛压力测试 |
| 15:45 | daily_tca | TCA 反馈闭环 |
| 16:00 | daily_ops_report | 每日运营报告 → 飞书 |

非交易日（周末/节假日）全部跳过。Scheduler 触发窗口 ±60 秒，同一任务每日只触发一次（防多实例并发重复触发）。

## API 端点

### 组合与持仓

| Method | Path | 说明 |
|--------|------|------|
| GET | `/positions` | 当前持仓 |
| GET | `/cash` | 可用资金 |
| GET | `/trades` | 近期交易记录 |
| GET | `/signals` | 近期信号 |
| GET | `/portfolio/summary` | 组合完整快照 |
| POST | `/portfolio/positions` | 插入/更新持仓 |

### 订单

| Method | Path | 说明 |
|--------|------|------|
| POST | `/orders/submit` | 提交订单 |
| GET | `/orders` | 查询订单列表 |
| POST | `/orders/<id>/cancel` | 取消订单 |

### 分析

| Method | Path | 说明 |
|--------|------|------|
| POST | `/analysis/run` | 触发每日选股分析 |
| POST | `/backtest` | 运行回测 |

### 可观测性

| Method | Path | 说明 |
|--------|------|------|
| GET | `/health` | 健康检查 |
| GET | `/metrics` | Prometheus 格式指标 |
| GET | `/params` | 查询策略参数 |
| PUT | `/params` | 更新策略参数 |

## 示例

```bash
# 查询组合
curl http://127.0.0.1:5555/portfolio/summary

# 触发选股分析
curl -X POST http://127.0.0.1:5555/analysis/run

# 提交订单
curl -X POST http://127.0.0.1:5555/orders/submit \
  -H "Content-Type: application/json" \
  -d '{"symbol":"000001.SH","direction":"BUY","shares":200,"price":12.50}'
```

## 数据持久化

组合数据存储在 `backend/services/portfolio.db`（SQLite）。
