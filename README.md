# a-quantitative-trading

A 股 + 港股量化研究与模拟交易系统。单 OS 单进程，不接入真实券商。

## 安装

```bash
conda create -n quant-trading python=3.11
conda activate quant-trading
pip install -r requirements.txt

cp .env.example .env                          # API key / 推送渠道凭证
cp config/trading.yaml.example config/trading.yaml
```

## 启动

```bash
# API + Scheduler + IntradayMonitor + StrategyRunner
python -m quant_app.main --mode all

# 仅 HTTP API
python -m quant_app.main --mode api --port 5555

# 仅 Scheduler + Monitor + Runner
python -m quant_app.main --mode worker

# Streamlit UI（默认连本机 5555）
streamlit run streamlit_app.py --server.port 8501
./start_streamlit.sh
```

`--mode` 别名：`both` → `all`、`scheduler` → `worker`。

进程级单实例锁在 `core/single_instance.py`（`fcntl.flock` + PID 文件），同机第二个 `mode=all` 会被拒。

### systemd

```bash
systemctl --user enable quant-trading-backend.service
systemctl --user start  quant-trading-backend.service
journalctl --user -u quant-trading-backend.service -f
```

## 配置

| 来源 | 路径 | 用途 |
|---|---|---|
| YAML | `config/trading.yaml` | 业务参数（组合 / 风控 / 策略） |
| Secrets | `.env` | API key / 飞书 / Telegram 凭证 |
| 环境变量 | shell | 临时覆盖 |

优先级：env > YAML > `core/config_defaults.py` 默认值。

启动时按需读取 `.env`（`setdefault` 加载，既有 env 优先）。所有代码消费的环境变量列在 `.env.example`，顶部贴有对账脚本。

`config/trading.yaml` 支持 `base / live / dev` 层级 deep-merge，通过 `TRADING_ENV=live|dev` 切换（默认 dev）。

数值常量集中在 `core/config_defaults.py`（佣金 / 印花税 / 滑点 / 风控阈值），运行时可被 YAML 覆盖。

`python -m core.config dump-effective [--env live]` 打印生效配置。

## 鉴权与限流

| 环境变量 | 默认 | 含义 |
|---|---|---|
| `TRADING_API_KEY` | 空 | 设置后非公共端点需带 `X-API-Key` |
| `TRADING_API_REQUIRE_LOCALHOST` | `0` | `1` 时本机回环也走鉴权 |
| `TRADING_RL_PER_MIN` | `120` | per-IP 每分钟上限，0 关闭 |

公共端点：`/health` / `/docs` / `/metrics` / `OPTIONS`。

生产部署同时设置 `TRADING_API_KEY` 与 `TRADING_API_REQUIRE_LOCALHOST=1`，否则同机进程可绕过鉴权。

## OpenAPI

```bash
# 浏览器查看
http://127.0.0.1:5555/docs

# 修改路由后重新生成 spec（committed at backend/openapi.json）
python scripts/generate_openapi.py

# CI 守门
python scripts/generate_openapi.py --check
```

## 项目结构

```
core/                      领域层 + 业务用例
  data_gateway/            对外网数据唯一出口（多 provider 路由 + 字段级合并）
  use_cases/               业务用例（UI / API / CLI / Scheduler 共享）
  factors/                 因子（technical / fundamental / macro / nlp / sentiment）
  strategies/              策略（macd_trend / pairs_trading / sector_rotation / signal_engine）
  brokers/                 虚拟券商（PaperBroker / SimulatedBroker / EventDrivenPaperBroker）
  execution/               订单执行（ImpactEstimator / 智能路由）
  ml/                      ML 训练 & 推理
  risk_engine.py           三层风控（PreTrade / InTrade / PostTrade）
  portfolio_optimizer.py   组合优化（BL / MeanVariance / RiskParity）
  regime.py                市场状态检测
  backtest_engine.py       回测引擎
  walkforward.py           Walk-Forward 验证
  pipeline_factory.py      DynamicWeightPipeline 工厂
  factor_pipeline.py       因子流水线（动态权重）
  llm_provider.py          LLM 服务定位器
  state_db.py              统一状态库路径解析
  singleton.py             LockedSingleton + 全局注册表
  lifecycle.py             进程级 Shutdown 协调
  idempotency.py           Idempotency-Key 存储（reserve/complete/release）
  config.py / config_defaults.py
  errors.py

backend/
  api.py                   Flask app + 鉴权 / 限流 / get_svc / 启动期路由注册
  api_deps.py              broker / risk_engine / idempotency_store 工厂
  api_routes/              Blueprint（按资源拆分，每个文件一个领域）
  services/
    intraday/              IntradayMonitor 子模块（data / signaling / risk / execution / alerts）
    intraday_monitor.py    Mixin 编排入口
    llm/                   LLM provider（MiniMax / DeepSeek / Kimi）
    fetchers/              外部数据源接入层
    channels/              告警通道（飞书 / Telegram / Discord）
    ipo_stars/             港股打新扫描
    portfolio.py           PortfolioService（state.db 读写）
    broker.py              生产用 PaperBroker
    signals.py / fundamentals.py / northbound.py / fund_flow.py / ...
  openapi.json             自动生成

quant_app/
  main.py                  进程入口，按 --mode 装配
  serve_api.py             API server 启动器
  run_worker.py            Scheduler + IntradayMonitor 装配

streamlit_app.py           UI 入口（st.navigation）
ui/
  config.py                bootstrap + page_config
  api_client.py            后端 HTTP 客户端
  format.py                金额 / 百分比格式化
  widgets/                 跨页面组件（layout / status / tables / charts / forms）
  pages/                   12 页面

scripts/                   运维 + 研究脚本（见 scripts/README.md）
config/                    trading.yaml + trading.yaml.example
data/                      state.db + parquet 缓存 + ML 模型 + 日历
tests/                     pytest 套件
docs/                      架构 / 券商 / 贡献 / changelog
```

## 主要能力

| 功能 | 入口 |
|---|---|
| 多因子流水线 | `core.pipeline_factory.build_pipeline()` |
| 动态选股 | `scripts/dynamic_selector.py` |
| 回测 | `core.use_cases.backtest` + `scripts/quant/backtest_cli.py` |
| 盘中监控 | `backend.services.intraday_monitor`（Scheduler 09:31 自启） |
| 组合优化 | `core.use_cases.compose_portfolio`（min_variance / max_sharpe / risk_parity） |
| 行业轮动 | `core.use_cases.sector_rotation_signal` |
| 配对交易 | `core.use_cases.pairs_trading_signal` |
| 风控 | `core.risk_engine`（PreTrade / InTrade / PostTrade + CVaR + 蒙特卡洛） |
| 早晚报 | Scheduler 09:30 / 16:00 → 飞书 / Telegram |

## 每日时间线

Scheduler 触发表（`quant_app/run_worker.py`），非交易日全部跳过：

| 时间 | 任务 |
|---|---|
| 09:30 | morning_runner：选股 → watchlist → RSI 信号 → 模拟下单 → 早报推送 |
| 09:31 | IntradayMonitor 启动 5 分钟轮询 |
| 15:00 | afternoon_report：收盘晚报（持仓快照 + 收益） |
| 15:10 | /analysis/run：DynamicStockSelector 日终选股 |
| 15:30 | daily_risk_report：CVaR + 蒙特卡洛压力测试 |
| 15:45 | daily_tca：TCA 反馈闭环 |
| 16:00 | daily_ops_report：每日运营报告 |

触发窗口 ±60 秒，同一任务每日只触发一次。

## 常用命令

| 操作 | 命令 |
|---|---|
| 查日志 | `tail -F backend/backend.log` 或 `journalctl --user -u quant-trading-backend -f` |
| 重生成 OpenAPI | `python scripts/generate_openapi.py` |
| 备份状态库 | `cp data/state.db data/state.db.$(date +%F).bak` |
| 重置 PID 锁 | `rm backend/.quant-backend.pid`（确认无实例后） |
| 全量回归 | `pytest tests/ -q` |
| Lint | `ruff check .` |
| 类型检查 | `mypy <module>`（CI strict 列表见 pyproject.toml） |
| 启动 pre-commit | `pre-commit install` |

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — 分层 / Use Case / DataGateway / 时间线
- [docs/BROKERS.md](docs/BROKERS.md) — 虚拟券商说明
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) — 开发流程
- [docs/CHANGELOG.md](docs/CHANGELOG.md) — 版本变更
- [backend/README.md](backend/README.md) — backend 服务 + 端点摘要
- [scripts/README.md](scripts/README.md) — 运维 / 研究脚本

## 免责

仅供研究与模拟交易，不接入真实券商，不构成投资建议。
