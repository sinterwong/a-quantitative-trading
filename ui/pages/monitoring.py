"""
ui/pages/monitoring.py — 🏥 监控 & 告警 (P4-1 阶段二)

operator 运营视图:策略健康 + 蒙特卡洛风险 + 数据质量 + AlertManager 告警中心。

⚠ 架构债(下个周期):
  - core.strategy_health.StrategyHealthMonitor 直连 → 走 use case (P2-8 中
    已抽出 system_health,扩展为 strategy_health_metrics 即可)
  - core.portfolio_risk.MonteCarloStressTest 直连 → 走 use case
  - core.data_quality / core.level2_quality 直连 → 走 backend
"""

from __future__ import annotations

import os

import pandas as pd
import plotly.express as px
import streamlit as st

from ui.data import (
    load_portfolio_summary, load_daily_stats, load_realtime, make_price_df,
)


def render_page() -> None:
    st.title('🏥 监控 & 告警')
    st.caption('策略健康 · CVaR / Monte Carlo · 数据质量 · AlertManager')

    tab_health, tab_data, tab_alert = st.tabs(
        ['💓 策略健康', '🔍 数据质量', '🔔 告警中心']
    )
    with tab_health:
        _render_health_tab()
    with tab_data:
        _render_data_quality_tab()
    with tab_alert:
        _render_alerts_tab()


def _render_health_tab() -> None:
    daily_stats = load_daily_stats(250)
    if not daily_stats:
        st.info('暂无日度统计数据(backend 未连接或 state.db 无记录)')
        return

    try:
        from core.strategy_health import StrategyHealthMonitor
        monitor = StrategyHealthMonitor()
        health_report = monitor.check(daily_stats)
        health_series = monitor.check_series(daily_stats)
    except Exception as e:
        st.warning(f'健康监控加载失败: {e}')
        health_report = None
        health_series = pd.DataFrame()

    if health_report:
        level = health_report.worst_level()
        icon = {'OK': '🟢', 'WARN': '🟡', 'CRITICAL': '🔴'}.get(level, '⚪')
        hc1, hc2, hc3, hc4, hc5 = st.columns(5)
        hc1.metric('系统状态', f'{icon} {level}')
        hc2.metric('Sharpe(20d)', f'{health_report.rolling_sharpe_20d:.3f}',
                   delta=f'{health_report.sharpe_change_pct:+.1f}%')
        hc3.metric('Sharpe(60d)', f'{health_report.rolling_sharpe_60d:.3f}')
        hc4.metric('今日收益', f'{health_report.latest_daily_return*100:+.2f}%')
        hc5.metric('连续亏损天', f'{health_report.consecutive_loss_days} 天')

        if health_report.alerts:
            for alert in health_report.alerts:
                fn = {'CRITICAL': st.error, 'WARN': st.warning, 'OK': st.success}.get(
                    alert.level, st.info)
                pause = ' **【建议暂停自动交易】**' if alert.should_pause else ''
                fn(f'**[{alert.level}] {alert.check_name}**: {alert.message}{pause}')
        else:
            st.success('策略运行正常,无健康告警')

    st.markdown('---')
    st.subheader('Rolling Sharpe 时序')
    if not health_series.empty and 'sharpe_20d' in health_series.columns:
        chart_df = (
            health_series[['date', 'sharpe_20d', 'sharpe_60d']]
            .dropna(subset=['sharpe_20d'])
            .rename(columns={'sharpe_20d': 'Sharpe(20d)', 'sharpe_60d': 'Sharpe(60d)'})
            .set_index('date')
        )
        st.line_chart(chart_df, use_container_width=True)
    else:
        st.info('数据不足(需 ≥ 20 条日度记录)')

    st.markdown('---')
    st.subheader('风险分析(CVaR · Monte Carlo)')
    try:
        from core.portfolio_risk import MonteCarloStressTest
        portfolio_mc = load_portfolio_summary()
        equity_mc = float(portfolio_mc.get('total_equity', 100_000) or 100_000)

        ret_series = pd.Series([
            float(s.get('daily_return', 0) if isinstance(s, dict)
                  else getattr(s, 'daily_return', 0))
            for s in daily_stats
        ]).dropna()

        if len(ret_series) >= 30:
            n_sim = st.slider('模拟次数', 500, 5000, 2000, step=500)
            mc = MonteCarloStressTest(n_simulations=n_sim, horizon_days=63, seed=42)
            mc_result = mc.run(ret_series, initial_equity=equity_mc)

            mc1, mc2, mc3, mc4 = st.columns(4)
            mc1.metric('P5 净值(63日)', f'¥{mc_result.p5_final:,.0f}',
                       delta=f'{(mc_result.p5_final/equity_mc-1)*100:+.1f}%')
            mc2.metric('P50 净值', f'¥{mc_result.p50_final:,.0f}',
                       delta=f'{(mc_result.p50_final/equity_mc-1)*100:+.1f}%')
            mc3.metric('亏损概率', f'{mc_result.prob_loss*100:.1f}%')
            mc4.metric('ES (95%)', f'{mc_result.expected_shortfall*100:.2f}%')

            with st.expander('完整 Monte Carlo 报告'):
                st.text(mc_result.summary())
        else:
            st.info(f'日度数据不足({len(ret_series)} 条,需 ≥ 30 条)')
    except Exception as e:
        st.warning(f'风险分析失败: {e}')


def _render_data_quality_tab() -> None:
    st.subheader('数据质量')
    try:
        from core.data_quality import DataQualityChecker
        dq_sym = st.text_input('检查标的', '000001.SZ', key='dq_sym')
        dq_days = st.slider('检查天数', 30, 120, 60, key='dq_days')

        if st.button('运行数据质量检查'):
            with st.spinner('拉取数据...'):
                df_dq = make_price_df(dq_sym, dq_days)
            if df_dq is not None:
                checker = DataQualityChecker(symbol=dq_sym)
                checker.check_and_mark(df_dq)
                report_dq = checker.report
                dq1, dq2, dq3 = st.columns(3)
                dq1.metric('数据质量评分', f'{report_dq.quality_score:.0f}/100')
                n_anomalies = (report_dq.n_zero_volume
                               + report_dq.n_abnormal_moves
                               + report_dq.n_gaps)
                dq2.metric('异常数据点', n_anomalies)
                dq3.metric('总行数', report_dq.total_bars)
                if report_dq.anomalies:
                    with st.expander('问题明细'):
                        for anm in report_dq.anomalies:
                            st.warning(f'[{anm.anomaly_type}] {anm.date} — {anm.detail}')
            else:
                st.error('数据获取失败')
    except Exception as e:
        st.warning(f'DataQualityChecker 加载失败: {e}')

    st.markdown('---')
    st.subheader('Level2 数据完整率')
    try:
        from core.level2_quality import Level2QualityReporter
        if st.button('生成 Level2 质量报告'):
            with st.spinner('分析 Level2 数据...'):
                reporter = Level2QualityReporter()
                report_l2 = reporter.generate(days=7, threshold=0.95)
                l2c1, l2c2, l2c3 = st.columns(3)
                l2c1.metric('完整率', f'{report_l2.overall_completeness:.1%}')
                l2c2.metric('快照总数', report_l2.total_snapshots)
                l2c3.metric('合格率', '✅ 合格' if report_l2.passed else '❌ 不合格')
                if report_l2.field_completeness:
                    fc_df = pd.DataFrame(
                        list(report_l2.field_completeness.items()),
                        columns=['字段', '完整率']
                    ).sort_values('完整率')
                    st.dataframe(fc_df, hide_index=True, use_container_width=True)
    except Exception as e:
        st.info(f'Level2 质量模块: {e}')

    st.markdown('---')
    st.subheader('实时行情连通性测试')
    test_sym = st.text_input('测试标的', '000001.SZ', key='ds_sym')
    if st.button('测试连通性'):
        snap_test = load_realtime(test_sym)
        if snap_test and snap_test.get('price', 0) > 0:
            st.success(f'行情获取成功:现价 ¥{snap_test["price"]:.2f},'
                       f'涨跌 {snap_test.get("pct", 0):+.2f}%')
        else:
            st.error('行情获取失败(可能是交易日外或网络问题)')


def _render_alerts_tab() -> None:
    st.subheader('AlertManager 告警中心')
    try:
        from core.alerting import AlertManager, get_alert_manager
    except ImportError as e:
        st.error(f'AlertManager 加载失败: {e}')
        return

    wechat_url = os.environ.get('WECHAT_WEBHOOK', '')
    dingtalk_url = os.environ.get('DINGTALK_WEBHOOK', '')

    a1, a2, a3 = st.columns(3)
    a1.metric('企业微信', '✅ 已配置' if wechat_url else '❌ 未配置')
    a2.metric('钉钉', '✅ 已配置' if dingtalk_url else '❌ 未配置')
    a3.metric('SMTP 邮件', '✅ 已配置' if os.environ.get('SMTP_HOST') else '❌ 未配置')

    if not wechat_url and not dingtalk_url:
        st.info('当前为 log_only 模式。在 `.env` 中配置 WECHAT_WEBHOOK 或 '
                'DINGTALK_WEBHOOK 后重启即可启用推送。')

    st.markdown('---')
    st.subheader('告警历史')
    am = get_alert_manager()
    history = am.get_history(last_n=50)

    if history:
        h_rows = [
            {'时间': rec.timestamp[:16], '级别': rec.level,
             '内容': rec.message[:80], '渠道': rec.channel, '成功': rec.sent}
            for rec in reversed(history)
        ]
        h_df = pd.DataFrame(h_rows)

        level_counts = h_df['级别'].value_counts()
        fig_h = px.bar(
            x=level_counts.index, y=level_counts.values,
            color=level_counts.index,
            color_discrete_map={'CRITICAL': '#f85149', 'WARNING': '#e3b341', 'INFO': '#4c78a8'},
            labels={'x': '级别', 'y': '条数'},
            title='告警级别分布',
        )
        fig_h.update_layout(showlegend=False, height=220, margin=dict(t=40, b=10))
        st.plotly_chart(fig_h, use_container_width=True)

        st.dataframe(h_df, hide_index=True, use_container_width=True)

        if st.button('清空告警历史'):
            am.clear_history()
            st.success('已清空')
            st.rerun()
    else:
        st.info('暂无告警记录')

    st.markdown('---')
    st.subheader('测试推送')
    test_level = st.selectbox('告警级别', ['INFO', 'WARNING', 'CRITICAL'], key='test_lvl')
    test_msg = st.text_input('测试消息', '这是一条测试告警', key='test_msg')

    if st.button('发送测试告警'):
        try:
            am_test = AlertManager(
                wechat_webhook=wechat_url,
                dingtalk_webhook=dingtalk_url,
                min_level='INFO',
                rate_limit_sec=0,
            )
            fn_map = {'INFO': am_test.send_info, 'WARNING': am_test.send_warning,
                      'CRITICAL': am_test.send_critical}
            result_t = fn_map[test_level](test_msg)
            if result_t:
                st.success('发送成功')
            else:
                st.warning('发送失败(检查 Webhook URL 是否正确)')
        except Exception as e:
            st.error(f'发送失败: {e}')
