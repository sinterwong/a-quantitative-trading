# A-Share Quantitative Trading System

An A-share (China stock market) quantitative trading system with dynamic sector rotation, multi-signal backtesting engine, and automated daily reporting.

## Project Structure

```
scripts/
├── dynamic_selector.py      # Multi-dimension dynamic stock selector
├── stock_data_only.py       # Daily stock report generator
└── quant/                  # Backtesting & trading engine
    ├── backtest.py              # Technical indicators (RSI/ATR/MACD/BB)
    ├── data_loader.py           # Historical OHLCV data loading + caching
    ├── data_provider.py         # Data source abstraction (historical vs live)
    ├── signal_generator.py      # Unified signal system (6 signal sources)
    ├── portfolio_engine_v3.py   # Portfolio backtesting engine
    ├── daily_engine.py         # Daily engine (backtest + live mode)
    ├── paper_executor.py       # VWAP execution + slippage model
    ├── daily_journal.py        # Daily trading journal (JSON + Markdown)
    ├── daily_reporter.py      # Report generator + Feishu push
    ├── institutional_live.py   # Real institutional holding data
    ├── config_stock_pool.py    # Stock pool configuration
    ├── position_sizer.py       # Kelly criterion position sizing
    ├── walkforward.py          # Walk-forward parameter validation
    ├── selection_pool.py       # Stock screening pool builder
    ├── trend_confirmed_rotation.py  # Trend-following rotation
    ├── intraday_signals.py     # Intraday signal detection
    └── strategies/             # Strategy implementations
        ├── mean_reversion.py   # RSI/BB mean-reversion
        ├── momentum.py         # MACD trend-following
        ├── institutional.py    # Institutional signal
        └── sector_rotation.py  # Sector rotation
```

## Features

### Dynamic Stock Selector (动态选股)
- **5-dimension scoring**: News sentiment (15%) + Sector performance (35%) + Fund flow (25%) + Technical (15%) + Consistency (10%)
- **Multi-source fallback**: Eastmoney -> Tonghuashun -> File cache
- **Domain rate limiting**: 200ms gap between calls to same API domain
- **Graceful degradation**: Falls back to broad ETFs (CSI300/GEM) when APIs are unavailable

### Backtesting Engine
- Data source abstraction (historical backtest vs live trading use the same code)
- 6 signal sources: RSI, MACD, BollingerBand, Institutional, Blacklist filter
- Signal resonance mechanism: 2+ simultaneous signals → strong buy
- Portfolio risk management: max position 30%, max drawdown circuit breaker
- VWAP execution with adaptive slippage modeling

### Daily Operations
- Runs daily after market close (15:00 CST)
- Generates structured report with positions, trades, signals, and tomorrow watch
- Pushes report to Feishu (optional)

## Installation

```bash
pip install pandas numpy requests
```

No other dependencies required — uses only standard library + lightweight HTTP calls.

## Quick Start

### Daily Stock Report
```bash
python scripts/stock_data_only.py
```

### Dynamic Stock Selection (standalone)
```bash
python scripts/dynamic_selector.py
```

### Backtesting
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

## Data Sources

| Data | Source |
|-------|--------|
| Real-time quotes | Tencent Finance (`qt.gtimg.cn`) |
| Sector/板块 data | Eastmoney push API (`push2.eastmoney.com`) |
| News/资讯 | Eastmoney快讯 API |
| Historical OHLCV | Sina Finance (`hq.sinajs.cn`) |
| Institutional holdings | AkShare (fund holding reports) |

## Key Design Decisions

1. **No heavy dependencies**: Pure Python stdlib + lightweight HTTP. No `akshare` required at runtime.
2. **Data source decoupling**: `DataProvider` interface lets backtest and live trading share the same engine.
3. **Conservative slippage**: Commission + slippage modeled conservatively (high-frequency strategies will underperform in backtest).
4. **Rate limiting built-in**: All HTTP calls respect API rate limits with automatic retry.

## Disclaimer

This is an **educational/research** project. Backtested results do not guarantee future performance. All data is for reference only, not investment advice.
