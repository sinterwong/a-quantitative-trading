"""
ui — Streamlit Web UI 包 (P4-1 阶段二)

子模块:
  - data: backend HTTP 封装 + @st.cache_data 数据加载器(原 streamlit_helpers.py)
  - components: 公共渲染助手(regime 徽章、metric 卡片、df 表 etc.)
  - pages: 7 个页面渲染模块(每个一个 render_page() 函数)

streamlit_app.py 作为入口,只负责:
  - sidebar + 全局 CSS
  - 通过 page 名字路由到 ui.pages.*.render_page()
"""
