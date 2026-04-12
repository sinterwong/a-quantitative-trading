# A-Share Quantitative Trading System

An automated A-share (China stock market) quantitative trading system with a persistent backend service, multi-signal backtesting engine, and daily report generation.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    小黑 (Agent)                         │
│              Brain: decisions, reasoning, commands        │
└──────────────┬─────────────────────▲──────────────────┘
                │ HTTP API              │ analysis results
                ▼                      │
┌───────────────────────────────────────────────────────┐
│          Backend Service (persistent)                     │
│                                                       │
│   HTTP API (Flask, port 5555)                         │
│   ├── GET  /positions, /cash, /trades, /signals      │
│   ├── POST /orders/submit, /analysis/run, etc.        │
│                                                       │
│   PortfolioService (SQLite persistence)                │
│   ├── positions  │ trades │ cash │ signals │ daily   │
│                                                       │
│   Scheduler (15:10 CST daily)                        │
│   └── triggers /analysis/run each trading day          │
└───────────────────────────────────────────────────────┘
                │
                ▼
┌───────────────────────────────────────────────────────┐
│          scripts/quant/ (analysis engine)               │
│   dynamic_selector.py  — 5-dimension stock selector  │
│   quant/                                             │
│   ├── signal_generator.py  — RSI/MACD/BB/Inst signals │
│   ├── portfolio_engine_v3.py — backtesting engine       │
│   ├── daily_engine.py     — daily analysis runner      │
│   └── paper_executor.py  — VWAP execution            │
└───────────────────────────────────────────────────────┘
```

## Quick Start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Start the backend service

```bash
# API + daily scheduler
python backend/main.py --mode both --port 5555

# API only (no scheduler)
python backend/main.py --mode api --port 5555
```

### 3. Query the portfolio

```bash
# Check portfolio
curl http://127.0.0.1:5555/portfolio/summary

# Record a trade
curl -X POST http://127.0.0.1:5555/trades \
  -H "Content-Type: application/json" \
  -d '{"symbol":"600900.SH","direction":"BUY","shares":200,"price":23.50}'

# Trigger analysis manually
curl -X POST http://127.0.0.1:5555/analysis/run
```

## Project Structure

```
scripts/
├── dynamic_selector.py      # 5-dimension dynamic stock selector
├── stock_data_only.py       # Daily report generator (standalone)
└── quant/                  # Backtesting & trading engine
    ├── backtest.py              # Technical indicators
    ├── signal_generator.py      # Unified signal system (6 sources)
    ├── portfolio_engine_v3.py   # Portfolio backtesting
    ├── daily_engine.py         # Daily runner (backtest + live)
    ├── paper_executor.py       # VWAP execution + slippage
    ├── daily_journal.py       # Journal persistence
    ├── daily_reporter.py       # Report + Feishu push
    ├── config_stock_pool.py    # Stock pool + RSI params (params.json)
    └── strategies/             # Strategy implementations

backend/
├── main.py              # Entry point (API + scheduler)
├── api.py               # Flask HTTP API (13 endpoints)
└── services/
    ├── __init__.py
    └── portfolio.py     # SQLite-backed portfolio service

tests/
├── run_tests.py         # Portable test runner (88 tests)
└── test_signal_generator.py

params.json              # Strategy parameters (editable)
```

## Scoring System

| Dimension | Weight | Description |
|-----------|--------|-------------|
| News sentiment | 15% | Policy (10), Earnings (8), Product (7), Fund (6), Rumor (1) |
| Sector performance | 35% | Real-time sector change % ranking |
| Fund flow | 25% | Northbound/main capital net flow |
| Technical | 15% | Constituent stock change signals |
| Consistency | 10% | % of rising stocks within sector |

## Running Tests

```bash
python tests/run_tests.py
```

## Key Design Decisions

1. **Persistent backend** — state survives restarts; agent queries via HTTP
2. **No heavy dependencies** — pure Python stdlib + `flask` + `requests`
3. **Conservative slippage** — commission + slippage modeled conservatively
4. **Rate limiting built-in** — HTTP calls respect API limits with exponential backoff

## Known Limitations

- THS sector fallback requires authentication
- No real-time intraday trading (end-of-day only)
- News sentiment is keyword-based, not NLP-based
- Broker integration is Phase 2 work

## Contributing

See `CONTRIBUTING.md`. All tests must pass before opening a PR.

## Disclaimer

This is an **educational/research** project. Backtested results do not guarantee future performance. All data is for reference only, not investment advice.
