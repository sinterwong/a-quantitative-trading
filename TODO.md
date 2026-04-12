# Roadmap & TODO

## Phase 1: Backend Service ✅ DONE

### P0 — Core Backend
- [x] **Backend skeleton** — `backend/main.py` with werkzeug server
- [x] **Portfolio Service** — SQLite persistence (positions, trades, cash, signals, orders)
- [x] **HTTP API** — 16 endpoints with validation + OpenAPI spec at `/docs`

### P1 — Scheduling & Automation
- [x] **Background scheduler** — 15:10 CST daily analysis trigger
- [x] **Self-healing** — log rotation + restart on crash
- [ ] **Service lifecycle** — `start.bat` / `stop.bat` / `status.bat`

### P2 — API Quality
- [x] **OpenAPI docs** — `GET /docs` (16 endpoints)
- [x] **Request validation** — `@validate_fields` decorator on all POST
- [ ] **Rate limiting** — prevent abuse of `/orders` endpoint

---

## Phase 2: Broker Integration ✅ DONE

- [x] **Broker abstraction layer** — `backend/services/broker.py`
- [x] **Paper executor** — VWAP model, order lifecycle (submitted/filled/rejected/cancelled)
- [x] **Orders API** — `POST /orders/submit`, `GET /orders`, `GET /orders/<id>`, `POST /orders/<id>/cancel`
- [ ] **Real broker** — Futu/Tiger (awaiting account credentials from Sinter)

---

## Phase 3: Real-time Intelligence ✅ DONE

- [x] **Signal engine** — `signals.py`: RSI approximation (prev-day RSI + intraday momentum)
- [x] **Bulk price fetch** — Tencent `qt.gtimg.cn` batch API
- [x] **IntradayMonitor** — daemon thread, 5-min polling 9:35-11:30 & 13:00-14:55 CST Mon-Fri
- [x] **Feishu push** — REST API direct push (appId/appSecret auth, tested ✅)
- [x] **Cooldown tracking** — 15-min per-symbol to prevent spam

---

## Phase 4: Research Infrastructure

### Signal System
- [ ] **Walk-Forward** — auto-retrain RSI parameters quarterly
- [ ] **Market regime** — bull/bear/sideways detection, per-regime params
- [ ] **Signal resonance v2** — weighted confidence scores

### Data Quality
- [ ] **News quality scoring** — filter vague phrases, weight official sources
- [ ] **Volume-Price confirmation** — price rise + volume expansion filter
- [ ] **Institutional data** — monthly holdings data sources

### Portfolio
- [ ] **Multi-stock expansion** — 5-10 stocks with position sizing
- [ ] **Stop-loss module** — hard stop + trailing stop per trade
- [ ] **Drawdown circuit breaker** — halt if portfolio drawdown > 15%

### Backtesting
- [ ] **In-sample / out-of-sample** — train/test split before optimization
- [ ] **Monte Carlo simulation** — confidence intervals
- [ ] **Benchmark** — CSI 300 as baseline

---

## Phase 5: Productization

- [ ] **Web UI** — Streamlit dashboard (positions, signals, reports)
- [ ] **PostgreSQL** — upgrade from SQLite for multi-user
- [ ] **Strategy plugins** — drop-in `strategies/strategy_xxx.py`
- [ ] **Scheduled reports** — configurable times (pre-market 9:00, post-market 15:30)
- [ ] **Changelog + License**

---

## Icebox
- TradingView chart embedding
- Email reports
- Dark mode for web UI
- Multi-language (EN/CN)
- Mobile push via Server酱

---

*Last updated: 2026-04-12*
