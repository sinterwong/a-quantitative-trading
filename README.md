# A-Share Quantitative Trading System

An automated A-share (China stock market) quantitative trading system featuring dynamic sector rotation based on multi-dimensional scoring, a backtesting engine with multiple signal sources, and daily report generation.

## Features

### Dynamic Stock Selector
- **5-dimension scoring**: News sentiment (15%) + Sector performance (35%) + Fund flow (25%) + Technical (15%) + Consistency (10%)
- **Multi-source fallback**: Eastmoney API → File cache (no external dependencies at runtime)
- **Domain rate limiting**: 200ms gap between calls to prevent rate limiting
- **Graceful degradation**: Falls back to broad ETFs (CSI300/GEM) when APIs are unavailable

### Backtesting Engine
- **Data source abstraction**: Backtest and live trading share the same engine via `DataProvider` interface
- **6 signal sources**: RSI, MACD, BollingerBand, Institutional holdings, Market regime filter, Blacklist filter
- **Signal resonance**: 2+ simultaneous signals → strong buy signal
- **Risk management**: Max position 30%, max drawdown circuit breaker
- **VWAP execution**: Adaptive slippage modeling

### Daily Operations
- Runs after market close (15:00 CST)
- Generates structured report: positions, trades, signals, tomorrow watch
- Optional push to Feishu (Chinese workplace chat)

## Quick Start

### Prerequisites
```bash
pip install pandas numpy requests
# Optional: pytest for running tests
pip install pytest
```

### Run Daily Stock Report
```bash
python scripts/stock_data_only.py
```

Expected output:
```
============================================================
  股市日报 - 2026-04-12 周日
============================================================
[选股] 正在从东方财富资讯获取热门板块...
[选股] 获取到 30 条资讯
[选股] 热门板块: [...]
...
```

### Run Stock Selector Standalone
```bash
python scripts/dynamic_selector.py
```

### Run Backtest
```python
from scripts.quant.data_loader import DataLoader
from scripts.quant.signal_generator import SignalGenerator
from scripts.quant.portfolio_engine_v3 import PortfolioEngineV3

loader = DataLoader()
engine = PortfolioEngineV3(capital=3000000)
engine.add_strategy('159992.SZ', 'RSI+Inst', {
    'sources': [
        ('RSISignalSource', {'rsi_buy': 35, 'rsi_sell': 70, 'take_profit': 0.20}, 1.0),
        ('InstitutionalSignalSource', {}, 0.8),
    ]
})
engine.run('20240101', '20251231')
```

## Project Structure

```
scripts/
├── dynamic_selector.py      # Multi-dimension dynamic stock selector
├── stock_data_only.py       # Daily stock report generator
└── quant/                  # Backtesting & trading engine
    ├── backtest.py              # Technical indicators (RSI/ATR/MACD/BB)
    ├── data_loader.py           # Historical OHLCV loading + caching
    ├── data_provider.py         # Data source abstraction
    ├── signal_generator.py      # Unified signal system (6 sources)
    ├── portfolio_engine_v3.py   # Portfolio backtesting engine
    ├── daily_engine.py         # Daily engine (backtest + live)
    ├── paper_executor.py      # VWAP execution + slippage
    ├── daily_journal.py       # Journal persistence
    ├── daily_reporter.py       # Report generator + Feishu push
    ├── institutional_live.py   # Institutional holding data
    ├── config_stock_pool.py   # Stock pool configuration
    ├── position_sizer.py      # Kelly criterion sizing
    ├── walkforward.py          # Walk-forward validation
    ├── selection_pool.py       # Stock screening
    ├── trend_confirmed_rotation.py  # Trend rotation
    ├── intraday_signals.py    # Intraday signals
    └── strategies/             # Strategy implementations
        ├── mean_reversion.py   # RSI/BB mean-reversion
        ├── momentum.py         # MACD trend-following
        ├── institutional.py    # Institutional signals
        └── sector_rotation.py # Sector rotation
```

## Data Sources

| Data | Source |
|------|--------|
| Real-time quotes | Tencent Finance (`qt.gtimg.cn`) |
| Sector data | Eastmoney push API (`push2.eastmoney.com`) |
| News | Eastmoney快讯 API |
| Historical OHLCV | Sina Finance (`hq.sinajs.cn`) |
| Institutional holdings | Public fund reports (quarterly) |

## Scoring System

| Dimension | Weight | Description |
|-----------|--------|-------------|
| News sentiment | 15% | Policy (10), Earnings (8), Product (7), Fund (6), Rumor (1) |
| Sector performance | 35% | Real-time sector change % ranking (hard data) |
| Fund flow | 25% | Northbound/main capital net flow (hard data) |
| Technical | 15% | Constituent stock change signals |
| Consistency | 10% | % of rising stocks within sector |

## Running Tests

```bash
# Install pytest
pip install pytest

# Run all tests
python -m pytest tests/ -v

# Or run manually (no pytest required)
python tests/test_dynamic_selector.py
```

## Key Design Decisions

1. **No heavy dependencies**: Pure Python stdlib + `requests`. No `akshare` required at runtime.
2. **Data source decoupling**: `DataProvider` interface lets backtest and live trading share the same engine.
3. **Conservative slippage**: Commission + slippage modeled conservatively.
4. **Rate limiting built-in**: All HTTP calls respect API limits with automatic retry.

## Known Limitations

- **THS fallback** (同花顺) requires authentication — sector fallback currently relies on file cache only
- **No real-time intraday trading**: System runs end-of-day only
- **News sentiment** is keyword-based, not NLP-based

## Contributing

Contributions welcome. Please read `CONTRIBUTING.md` before submitting PRs.

## Disclaimer

This is an **educational/research** project. Backtested results do not guarantee future performance. All data is for reference only, not investment advice.
