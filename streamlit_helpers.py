"""
streamlit_helpers.py — backward-compat shim (P4-1 阶段二)

实际实现搬到 `ui/data.py`。本文件保留导入面以兼容既有 import:
    from streamlit_helpers import api_get, load_positions, ...
"""

from ui.data import (  # noqa: F401
    BACKEND_URL, BASE_DIR, BACKEND_DIR, DATA_DIR, OUTPUTS_DIR,
    api_get, api_post,
    load_portfolio_summary, load_positions, load_trades, load_signals,
    load_daily_equity, load_daily_stats, load_wf_results,
    load_realtime, load_news_headlines, load_watchlist, load_trading_config,
    limit_up_pct, make_price_df,
    _make_price_df_from_akshare,
)
