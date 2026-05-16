"""streamlit_app.py — 量化交易控制台入口。

仅做:
- bootstrap()  设 page config + 注入全局 CSS
- 用 st.navigation 装载所有页面(按能力分组)
- 把控制权交给 nav.run() — 每个 page 是独立脚本

实际渲染逻辑全在 ui/pages/*.py。本入口不直接调任何 backend / 业务逻辑。
"""
from __future__ import annotations

import streamlit as st

from ui.config import bootstrap

bootstrap()

_NAV = {
    '组合': [
        st.Page('ui/pages/dashboard.py', title='总览',
                icon=':material/dashboard:', default=True),
        st.Page('ui/pages/portfolio.py', title='持仓与现金',
                icon=':material/account_balance_wallet:'),
    ],
    '信号与执行': [
        st.Page('ui/pages/signals.py', title='信号与交易',
                icon=':material/auto_graph:'),
        st.Page('ui/pages/watchlist.py', title='盯盘自选池',
                icon=':material/visibility:'),
    ],
    '分析': [
        st.Page('ui/pages/daily_pick.py', title='每日选股',
                icon=':material/insights:'),
        st.Page('ui/pages/stock_deep.py', title='个股深度',
                icon=':material/manage_search:'),
        st.Page('ui/pages/sector_pairs.py', title='板块与配对',
                icon=':material/donut_small:'),
    ],
    '研究': [
        st.Page('ui/pages/backtest.py', title='回测',
                icon=':material/history:'),
        st.Page('ui/pages/composer.py', title='组合优化',
                icon=':material/balance:'),
        st.Page('ui/pages/wfa.py', title='WFA 研究',
                icon=':material/science:'),
    ],
    '市场': [
        st.Page('ui/pages/market.py', title='市场数据',
                icon=':material/public:'),
    ],
    '系统': [
        st.Page('ui/pages/system.py', title='系统与风控',
                icon=':material/health_and_safety:'),
    ],
}

st.navigation(_NAV).run()
