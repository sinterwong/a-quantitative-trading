#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
streamlit_app.py — 量化系统 Web UI 入口 (P4-1 阶段二)
===================================================
系统定位:SimulatedBroker 模拟实盘 · A 股市场 · 单租户准生产实盘 + 研究台

启动方式:
  streamlit run streamlit_app.py --server.port 8501

页面结构(每个 page 一个独立模块,见 `ui/pages/`):
  1. 📊 仪表盘      — operator: 账户摘要 / 净值 / Regime / 信号 / 告警
  2. 🎯 因子工作台  — researcher: 22 因子 Z-score / 动态权重 / NLP
  3. 🤖 ML 模型     — researcher: 模型注册表 / Walk-Forward 训练 / 特征重要性
  4. ⚖️ 组合优化   — researcher: MVO / BL / 资金分配
  5. 📈 信号 & 执行 — operator/trader: 实时信号 / VWAP/TWAP / 成交 TCA
  6. 📉 回测验证   — researcher: WFA / 敏感性 / 一致性
  7. 🏥 监控 & 告警 — operator: 策略健康 / 蒙特卡洛 / 数据质量 / AlertManager

本入口只负责:
  - 全局 CSS / page_config
  - sidebar 导航 + backend 健康指示
  - 根据 sidebar 选项路由到 ui.pages.<module>.render_page()

研究类页面(2/3/4/6)直连 core.* 的架构债已在
docs/UI_REFACTOR_PROPOSAL.md 中记录,下个周期改为 use case + backend 端点。
"""

from __future__ import annotations

import os
from datetime import datetime

import streamlit as st

from ui.data import api_get
from ui.components import global_css, broker_badge
from ui.pages import (
    dashboard,
    factor_workbench,
    ml_models,
    portfolio_optimization,
    signals_execution,
    backtest,
    monitoring,
)

# 清理 proxy 环境变量(ui.data 加载时已做,这里冗余保护)
for k in list(os.environ.keys()):
    if 'proxy' in k.lower():
        del os.environ[k]


# ─── Page config ────────────────────────────────────────────

st.set_page_config(
    page_title='量化系统',
    page_icon='📊',
    layout='wide',
    menu_items={'About': '## 量化系统 v3 · SimulatedBroker · A 股 · ~95分'},
)
global_css()


# ─── Sidebar ────────────────────────────────────────────────

st.sidebar.title('量化系统')
st.sidebar.caption(datetime.now().strftime('%Y-%m-%d  %H:%M'))

backend_ok = api_get('/health', timeout=3).get('status') == 'ok'
if backend_ok:
    st.sidebar.success('Backend 运行中')
else:
    st.sidebar.warning('Backend 未连接(部分功能受限)')

broker_badge()
st.sidebar.markdown('---')

_PAGE_ROUTE = {
    '📊 仪表盘':      dashboard.render_page,
    '🎯 因子工作台':  factor_workbench.render_page,
    '🤖 ML 模型':     ml_models.render_page,
    '⚖️ 组合优化':   portfolio_optimization.render_page,
    '📈 信号 & 执行': signals_execution.render_page,
    '📉 回测验证':   backtest.render_page,
    '🏥 监控 & 告警': monitoring.render_page,
}

page = st.sidebar.radio('导航', list(_PAGE_ROUTE.keys()), index=0)

st.sidebar.markdown('---')
st.sidebar.button('全局刷新', on_click=st.cache_data.clear, use_container_width=True)


# ─── 路由 ───────────────────────────────────────────────────

_PAGE_ROUTE[page]()
