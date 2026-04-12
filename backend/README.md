# Backend Service

Persistent backend for the A-share trading system.
Keeps state between restarts, exposes an HTTP API.

## Quick Start

```bash
cd backend
pip install flask

# Start API + scheduler
python main.py --mode both --port 5555

# API only (no scheduler)
python main.py --mode api --port 5555
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Health check |
| GET | `/positions` | Current positions |
| GET | `/cash` | Available cash |
| GET | `/trades` | Recent trades |
| GET | `/signals` | Recent signals |
| GET | `/portfolio/summary` | Full portfolio snapshot |
| GET | `/portfolio/daily` | Recent daily summaries |
| POST | `/portfolio/positions` | Upsert a position |
| POST | `/portfolio/cash` | Set cash amount |
| POST | `/trades` | Record a trade |
| POST | `/signals` | Record a signal |
| POST | `/orders/submit` | Submit order intent |
| POST | `/analysis/run` | Trigger daily analysis |
| GET | `/analysis/status` | Last analysis status |

## Example

```bash
# Query portfolio
curl http://127.0.0.1:5555/portfolio/summary

# Record a trade
curl -X POST http://127.0.0.1:5555/trades \
  -H "Content-Type: application/json" \
  -d '{"symbol":"600900.SH","direction":"BUY","shares":200,"price":23.50}'

# Trigger analysis
curl -X POST http://127.0.0.1:5555/analysis/run
```

## Architecture

```
backend/
├── main.py              # Entry point, process manager
├── api.py               # Flask HTTP API
└── services/
    └── portfolio.py     # SQLite-backed portfolio state
```

## Data

All data persists in `services/portfolio.db` (SQLite).
