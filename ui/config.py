"""ui/config.py — env bootstrap + 全局 page config。

仅在 streamlit_app.py 顶部 import 一次:`from ui.config import bootstrap`。
bootstrap() 必须在任何 st.* UI 调用之前执行。
"""
from __future__ import annotations

import os
import streamlit as st

APP_TITLE = '量化交易控制台'
APP_ICON = ':material/show_chart:'

BACKEND_URL = os.environ.get('QUANT_UI_BACKEND_URL', 'http://127.0.0.1:5555').rstrip('/')
API_KEY = os.environ.get('TRADING_API_KEY', '').strip()
REQUEST_TIMEOUT = float(os.environ.get('QUANT_UI_TIMEOUT', '8'))


def bootstrap() -> None:
    """设置 page config + 注入全局 CSS。"""
    st.set_page_config(
        page_title=APP_TITLE,
        page_icon=APP_ICON,
        layout='wide',
        initial_sidebar_state='expanded',
    )
    from ui.widgets.layout import global_css
    global_css()
