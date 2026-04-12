# Roadmap & TODO

## Short-term: Stable Daily Operation (1-3 months)

### P0 — Must Fix Before Production
- [ ] **Verify THS sector fallback** — replace broken `d.10jqka.com.cn` URL with working Sina Finance sector API (`vip.stock.finance.sina.com.cn`)
- [ ] **Monday open validation** — observe if Eastmoney push API recovers after weekend cooldown; confirm cron delivers to Feishu
- [x] **Expand test coverage** — added `tests/test_signal_generator.py` (20 tests: RSISignalSource, MarketRegimeSource, SignalGenerator, BlackListFilter) + `tests/run_tests.py` (88 tests total, no external deps). For GitHub Actions use `python -m pytest tests/ -v`.
- [ ] **Error handling audit** — every API call should have a clear fallback path; empty data must not crash the report

### P1 — Improve Reliability
- [ ] **Parameter externalization** — move RSI parameters (`rsi_buy=35`, `rsi_sell=70`, etc.) out of code into `config_stock_pool.py` or a YAML file
- [ ] **Journal persistence** — verify full signal → trade → position cycle writes correctly to `scripts/quant/journal/`
- [ ] **Retry with backoff** — current 200ms rate limit works; add exponential backoff (1s → 2s → 4s) when API returns 429/503
- [ ] **Empty-state reporting** — when all APIs fail and we fall back to broad ETFs, generate a visible "Data Unavailable" notice in the report

### P2 — Polish
- [ ] **CI on Windows** — add `runs-on: windows-latest` matrix job to GitHub Actions; some scripts use Windows-specific path handling
- [ ] **Log file rotation** — prevent `cache/` and `journal/` from growing unbounded
- [ ] **Daily health check** — add a lightweight pre-check at 14:55 that verifies API connectivity before market close

---

## Mid-term: Signal Quality (3-6 months)

### Signal System
- [ ] **Walk-Forward parameter update** — auto-retrain RSI parameters quarterly using `scripts/quant/walkforward.py` instead of hardcoded values
- [ ] **Market regime detection** — distinguish bull/bear/sideways; apply different strategy parameters per regime
- [ ] **Signal resonance v2** — current rule is "2+ signals = strong buy"; refine with weighted signal confidence scores

### Data Quality
- [ ] **Real-time institutional data** — current fund-holding data is quarterly and lagged; evaluate 月度持仓 or 雪球/韭圈儿 as alternatives
- [ ] **News quality scoring** — replace keyword-only matching with: (a) discard vague phrases like "有望", "或将"; (b) weight official sources (Xinhua, 证监会) vs 自媒体
- [ ] **Volume-Price confirmation** — require price rise + volume expansion as a combined signal filter

### Portfolio
- [ ] **Multi-stock expansion** — extend from single-ETF to 5-10 stock portfolio with proper position sizing
- [ ] **Stop-loss module** — implement per-trade stop-loss (e.g., -5% hard stop) and trailing stop
- [ ] **Drawdown circuit breaker** — if portfolio drawdown > 15%, halt new position opening for that day

### Backtesting
- [ ] **In-sample / out-of-sample split** — standard train/test split before any parameter optimization
- [ ] **Monte Carlo simulation** — run backtest with randomized slippage and commission to get confidence intervals
- [ ] **Benchmark comparison** — add CSI 300 and行业指数 as performance benchmarks in report

---

## Long-term: Productization (6-12 months)

### Infrastructure
- [ ] **Web UI** — build a Streamlit or Gradio dashboard for non-technical users; view positions, signals, and daily reports in browser
- [ ] **Database** — replace JSON file journal with SQLite/PostgreSQL for structured queries (e.g., "show all buy signals for 创业板 in last 30 days")
- [ ] **Performance optimization** — use `vectorbt` or `numba` to accelerate backtesting 10-100x for large parameter grids

### Ecosystem
- [ ] **Strategy plugins** — make `scripts/quant/strategies/` a pluggable interface; users can drop in a new `strategy_xxx.py` without modifying core engine
- [ ] **Paper trading integration** — connect to a broker's simulated account API (大多数券商 support 模拟资金账户); execute signals automatically
- [ ] **Scheduled reports** — configurable report time (currently hardcoded to 15:00 CST); add 9:00 pre-market summary

### Community
- [ ] **CONTRIBUTING.md** — contribution guidelines, coding style (PEP 8 + black), PR template
- [ ] **Issue templates** — Bug Report / Feature Request / Question templates on GitHub
- [ ] **Changelog** — maintain `CHANGELOG.md` with every release; use semantic versioning
- [ ] **License clarification** — current MIT is permissive; consider dual-licensing if commercial use cases emerge

---

## Icebox (Nice to Have, Low Priority)

- [ ] TradingView integration for chart embedding in reports
- [ ] Email report option (currently Feishu only)
- [ ] Dark mode for web UI
- [ ] Multi-language support (English / 中文 switch)
- [ ] Mobile push notification via Server酱 or PushPlus

---

*Last updated: 2026-04-12*
