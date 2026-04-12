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

- [x] **Signal engine v2** — `signals.py`: A-share limit detection + RSI + volume ratio
- [x] **Bulk price fetch** — Tencent `qt.gtimg.cn` batch API (corrected field indices)
- [x] **IntradayMonitor** — daemon thread, 5-min polling 9:35-11:30 & 13:00-14:55 CST Mon-Fri
- [x] **Feishu push** — REST API direct push (appId/appSecret auth, tested ✅)
- [x] **Cooldown tracking** — 15-min per-symbol to prevent spam

**A-share 专用信号类型：**
- `LIMIT_UP` / `LIMIT_DOWN` — 涨跌停（含放量/缩量判断）
- `LIMIT_RISK_UP` / `LIMIT_RISK_DOWN` — 逼近涨跌停（<1%）
- `WATCH_LIMIT_UP` / `WATCH_LIMIT_DOWN` — 接近涨跌停（<3%）
- `RSI_BUY` / `RSI_SELL` — RSI 超买超卖 + 动量确认
- `WATCH_BUY` / `WATCH_SELL` — RSI 极端区域
- `VOLATILE` — 大幅波动警示（>3%）

---

## Phase 4: Research Infrastructure

### Signal System ✅ DONE
- [x] **Walk-Forward** — `walkforward_job.py` + `walkforward_persistence.py` + 季度自动重训
- [x] **Market regime** — `MarketRegimeSource` 已有 (MA200)
- [x] **News quality scoring** — `news_quality.py`（含糊词过滤 + 权威来源加分）

### Data Quality ✅ DONE
- [x] **News quality scoring** — filter vague phrases (有望/或将/知情人士), weight official sources
- [x] **Volume-Price confirmation** — 内置于 LIMIT_UP 信号（放量=真拉升，缩量=诱多）

### Portfolio
- [ ] **Multi-stock expansion** — 5-10 stocks with position sizing
- [ ] **Stop-loss module** — hard stop + trailing stop per trade (backtest.py 已有)
- [ ] **A-share drawdown circuit** — position-level limit risk warnings (not market halt)

### Backtesting
- [x] **In-sample / out-of-sample** — `WalkForwardAnalyzer` 已强制分离
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
