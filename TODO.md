# Roadmap & TODO

## Phase 1: Backend Service (current sprint)

> Build a persistent backend service with portfolio persistence and HTTP API.
> Agent (小黑) communicates via HTTP — not one-shot scripts.

### P0 — Core Backend
- [x] **Backend skeleton** — `backend/main.py` with HTTP server (werkzeug), starts as persistent process
- [x] **Portfolio Service** — `backend/services/portfolio.py` with SQLite persistence (positions, trades, cash)
- [x] **HTTP API endpoints** — `GET /positions`, `GET /trades`, `GET /cash`, `POST /orders`, `GET /health`
- [x] **OpenClaw tool integration** — `skills/portfolio-api/SKILL.md` wrapper for agent to call backend API

### P1 — Scheduling & Automation
- [x] **Background scheduler** — `main.py` runs scheduler thread at 15:10 CST, triggers `/analysis/run`
- [ ] **Service lifecycle** — `backend/start.sh` / `backend/stop.sh` / Windows `.bat` for startup management
- [x] **Self-healing** — log rotation (logging.FileHandler) + restart on crash in scheduler

### P2 — API Quality
- [ ] **Swagger/OpenAPI docs** — auto-generated docs at `GET /docs`
- [ ] **Request validation** — reject malformed orders with clear error messages
- [ ] **Rate limiting** — prevent abuse of `/orders` endpoint

---

## Phase 2: Broker Integration

### P0 — Paper Trading
- [ ] **Broker abstraction layer** — `backend/services/broker.py` interface, first implementation is paper trading
- [ ] **Paper executor** — simulate fills with VWAP model, no real money

### P1 — Real Broker
- [ ] **Broker choice** — Futu (富途) / Tiger (老虎) / other (TBD)
- [ ] **Account connector** — authenticate, fetch real positions and cash
- [ ] **Order execution** — market orders, limit orders with proper error handling

---

## Phase 3: Real-time Intelligence  ← IN PROGRESS

### P0 — Live Market Data
- [x] **Signal engine** — `backend/services/signals.py`: RSI approximation (prev-day RSI + intraday momentum), signal evaluation (BUY/SELL/WATCH_BUY/WATCH_SELL/VOLATILE)
- [x] **Bulk price fetch** — Tencent `qt.gtimg.cn` batch API for real-time quotes (single request for all positions)
- [x] **IntradayMonitor** — `backend/services/intraday_monitor.py`: daemon thread, 5-min polling during 9:35-11:30 & 13:00-14:55 CST Mon-Fri, cooldown tracking to prevent spam
- [ ] **Push notifications** — Feishu alerts when signals fire during market hours (gateway HTTP hook)

### P1 — Proactive Alerts
- [ ] **OpenClaw proactive reporting** — 小黑 proactively messages Sinter with signals, not just passive query

---

## Phase 4: Research Infrastructure

### Signal System
- [ ] **Walk-Forward parameter update** — auto-retrain RSI parameters quarterly
- [ ] **Market regime detection** — distinguish bull/bear/sideways; different strategy params per regime
- [ ] **Signal resonance v2** — weighted confidence scores instead of "2+ signals = strong buy"

### Data Quality
- [ ] **News quality scoring** — discard vague phrases ("有望", "或将"), weight official sources
- [ ] **Volume-Price confirmation** — price rise + volume expansion as combined filter
- [ ] **Real-time institutional data** — evaluate monthly holdings data sources

### Portfolio
- [ ] **Multi-stock expansion** — 5-10 stock portfolio with proper position sizing
- [ ] **Stop-loss module** — per-trade hard stop + trailing stop
- [ ] **Drawdown circuit breaker** — halt new positions if portfolio drawdown > 15%

### Backtesting
- [ ] **In-sample / out-of-sample split** — standard train/test before optimization
- [ ] **Monte Carlo simulation** — confidence intervals on backtest results
- [ ] **Benchmark comparison** — CSI 300 as performance baseline

---

## Phase 5: Productization

### Infrastructure
- [ ] **Web UI** — Streamlit/Gradio dashboard for positions, signals, and reports
- [ ] **Database upgrade** — PostgreSQL for structured queries, multi-user support
- [ ] **Performance optimization** — `vectorbt`/`numba` for large parameter grids

### Ecosystem
- [ ] **Strategy plugins** — drop in `strategies/strategy_xxx.py` without modifying core
- [ ] **Scheduled reports** — configurable times (pre-market 9:00, post-market 15:30)

### Community
- [ ] **Changelog** — `CHANGELOG.md` with semantic versioning
- [ ] **License clarification** — dual-licensing if commercial use cases emerge

---

## Icebox

- TradingView chart embedding in reports
- Email report option
- Dark mode for web UI
- Multi-language support (EN/CN)
- Mobile push via Server酱

---

*Last updated: 2026-04-12 | Phase 1 is the current sprint*
