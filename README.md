# a-quantitative-trading

A 股 + 港股量化研究和模拟交易,虚拟券商,单 OS 单进程。

## 安装

```bash
conda create -n quant-trading python=3.11
conda activate quant-trading
pip install -r requirements.txt

cp .env.example .env                          # 填 API key
cp config/trading.yaml.example config/trading.yaml
```

## 运行

```bash
# 一次进程跑全部:API + Scheduler + IntradayMonitor + StrategyRunner
python backend/main.py --mode all

# 只起 HTTP API
python backend/main.py --mode api --port 5555

# 只起 Scheduler / Monitor / Runner
python backend/main.py --mode worker

# Streamlit UI(默认连本机 5555 端口的 backend)
streamlit run streamlit_app.py --server.port 8501
```

Backward-compat 别名:`--mode both` → `all`、`--mode scheduler` → `worker`。

systemd 守护进程:

```bash
systemctl --user enable quant-trading-backend.service
systemctl --user start quant-trading-backend.service
journalctl --user -u quant-trading-backend.service -f
```

## 配置

| 来源 | 路径 | 用途 |
|---|---|---|
| YAML 主配置 | `config/trading.yaml` | 业务参数 |
| Secrets | `.env` | API key / 飞书 credentials |
| 环境变量 | shell | 临时覆盖,优先级最高 |

优先级:env > YAML > 默认值。

状态库路径解析(`core/state_db.state_db_path()`):
`QUANT_STATE_DB` env > `data/state.db` > legacy `backend/services/portfolio.db`。

## 鉴权

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `TRADING_API_KEY` | 空 | 设置后非公共端点必须带 `X-API-Key` |
| `TRADING_API_REQUIRE_LOCALHOST` | `0` | `1` 时取消本机回环豁免 |
| `TRADING_API_RATE_LIMIT_PER_MIN` | `0` | per-IP 每分钟上限,0=关闭 |

公共端点(无需鉴权):`/health` / `/docs` / `/metrics` / `OPTIONS`。

生产部署务必两项都拉上:

```bash
TRADING_API_KEY=$(openssl rand -hex 32)
TRADING_API_REQUIRE_LOCALHOST=1
```

否则同机进程可绕过鉴权。

## OpenAPI

```bash
# 浏览器看
http://127.0.0.1:5555/docs

# 修改路由后重新生成 spec(committed at backend/openapi.json)
python scripts/generate_openapi.py

# CI 守门:本地忘了重生成会红
python scripts/generate_openapi.py --check
```

## 项目结构

```
core/
  data_gateway/      对外网数据唯一出口,多 provider 路由 + 字段级合并 + 熔断
  use_cases/         业务用例,UI/API/CLI/Scheduler 共享同一组函数
  factors/           因子实现
  strategies/        策略实现
  brokers/           虚拟券商(PaperBroker / SimulatedBroker)
  regime.py          市场状态检测(CALM/BULL/BEAR/VOLATILE)
  risk_engine.py     三层风控
  llm_provider.py    LLM 服务定位器(use case 调用 LLM 的出口)
  state_db.py        统一状态库路径解析
  single_instance.py OS 级单实例锁
  ...

backend/
  api.py             Flask HTTP API
  main.py            shim,转发 quant_app
  services/
    intraday/        IntradayMonitor 5 Mixin(data/signaling/risk/execution/alerts)
    intraday_monitor.py  Mixin 编排入口
    llm/             MiniMax / DeepSeek / Kimi provider
    ...
  openapi.json       自动生成的 OpenAPI spec

quant_app/
  main.py            按 --mode 装配进程
  serve_api.py       HTTP server 启动器
  run_worker.py      Scheduler + IntradayMonitor 装配

ui/
  pages/             Streamlit 页面(dashboard / signals / backtest / monitoring / ...)
  data.py            UI 数据加载(全部走 backend API)
  components/        公共渲染组件

scripts/             运维 + 研究脚本(详见 scripts/README.md)
config/              YAML 配置(trading.yaml + trading.yaml.example)
data/                state.db + parquet 时序
docs/                文档(架构 / 券商 / 贡献 / changelog)
tests/               pytest
```

## 系统能力

| 功能 | 入口 |
|---|---|
| 多因子流水线 | `core/pipeline_factory.build_pipeline()` |
| 动态选股 | `scripts/dynamic_selector.py` |
| 回测 | `core/use_cases/backtest.py` + `scripts/quant/backtest_cli.py` |
| 盘中监控 | `backend/services/intraday_monitor.py`(Scheduler 09:31 自启) |
| 组合优化 | `core/use_cases/compose_portfolio.py`(min_variance / max_sharpe / risk_parity) |
| 行业轮动 | `core/use_cases/sector_rotation_signal.py` |
| 配对交易 | `core/use_cases/pairs_trading_signal.py` |
| 风控 | `core/risk_engine.py`(PreTrade / InTrade / PostTrade + CVaR + 蒙特卡洛) |
| 早晚报 | Scheduler 09:30 / 16:00 → 飞书 |

## 每日时间线

详见 `docs/ARCHITECTURE.md`。要点:

- 09:30 morning_runner、09:31 IntradayMonitor 启动
- 15:00 afternoon_report、15:10 /analysis/run、15:30 风控报告
- 15:45 TCA、16:00 daily_ops_report
- 非交易日全部跳过

## 常用运维

| 操作 | 命令 |
|---|---|
| 查日志 | `tail -F backend/backend.log` 或 `journalctl --user -u quant-trading-backend -f` |
| 重生成 OpenAPI | `python scripts/generate_openapi.py` |
| 备份状态库 | `cp data/state.db data/state.db.$(date +%F).bak` |
| 重置 PID 锁 | `rm backend/.quant-backend.pid`(确认无实例后) |
| 全量回归 | `pytest tests/ -q` |

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 分层 / Use Case / DataGateway / 时间线
- [docs/BROKERS.md](docs/BROKERS.md) — 虚拟券商策略 + deprecated 清单
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — 开发流程
- [backend/README.md](backend/README.md) — backend 服务说明 + 端点摘要
- [scripts/README.md](scripts/README.md) — 运维/研究脚本

## 免责

仅供研究与模拟交易,不接入真实券商,不构成投资建议。
