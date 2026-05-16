"""
ui/pages — 7 个页面渲染模块 (P4-1 阶段二)

每个模块暴露 `render_page()` 函数,无参数(数据通过 ui.data.load_* 拉取)。
streamlit_app.py 根据 sidebar 选项路由到对应模块。

页面 → 模块映射:
  📊 仪表盘       → ui.pages.dashboard
  🎯 因子工作台   → ui.pages.factor_workbench
  🤖 ML 模型      → ui.pages.ml_models
  ⚖️ 组合优化    → ui.pages.portfolio_optimization
  📈 信号 & 执行 → ui.pages.signals_execution
  📉 回测验证    → ui.pages.backtest
  🏥 监控 & 告警 → ui.pages.monitoring
"""
