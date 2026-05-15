"""
ui/pages/factor_workbench.py — 🎯 因子工作台 (P4-1 阶段二)

researcher 视图:单因子 Z-Score / DynamicWeightPipeline 综合评分 / NLP 情感。

⚠ 架构债(下个周期):
  - core.factor_registry / core.factors.* / core.factor_pipeline 全部直连。
    应改为 backend 端点(GET /factors/list, POST /factors/evaluate),走 use case 层
  - DynamicWeightPipeline 的硬编码因子权重应配置化(trading.yaml 已有 strategies 节点)
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

from ui.data import (
    load_trading_config, load_realtime, load_news_headlines, make_price_df,
)


def render_page() -> None:
    st.title('🎯 因子工作台')
    st.caption('22 个因子实时评分 · 动态 IC 权重 · NLP 情感 · 因子相关性')

    cfg = load_trading_config()
    live_syms = cfg.get('live_symbols', [])
    default_syms = [s['symbol'] for s in live_syms] if live_syms else ['000001.SZ', '600519.SH']

    col_ctrl, col_main = st.columns([1, 3])

    with col_ctrl:
        symbol = st.selectbox('选择标的', default_syms + ['自定义'])
        if symbol == '自定义':
            symbol = st.text_input('输入代码(如 600519.SH)', '600519.SH')

        days = st.slider('历史数据天数', 60, 300, 120, step=20)
        run_btn = st.button('运行因子分析', type='primary', use_container_width=True)

        st.markdown('---')
        st.markdown('**注册因子列表(22个)**')
        try:
            from core.factor_registry import registry
            for name in registry.list_factors():
                st.caption(f'• {name}')
        except Exception as e:
            st.warning(f'注册表加载失败: {e}')

    with col_main:
        if not run_btn:
            _render_factor_help()
            return

        with st.spinner(f'拉取 {symbol} 数据并计算因子...'):
            df = make_price_df(symbol, days)
        if df is None or df.empty:
            st.error('无法获取历史数据,请检查网络或标的代码。')
            return

        _render_single_factor_scores(symbol, df)
        st.markdown('---')
        _render_pipeline_score(symbol, df)
        st.markdown('---')
        _render_nlp_sentiment(symbol, df)


def _render_single_factor_scores(symbol: str, df: pd.DataFrame) -> None:
    st.subheader(f'{symbol} · 因子评分(最新一日)')
    try:
        from core.factor_registry import registry

        factor_rows = []
        for fname in registry.list_factors():
            try:
                fobj = registry.create(fname)
                vals = fobj.evaluate(df)
                score = float(vals.iloc[-1]) if len(vals) > 0 else 0.0
                if not np.isfinite(score):
                    score = 0.0
                factor_rows.append({'因子': fname, '得分': score})
            except Exception:
                factor_rows.append({'因子': fname, '得分': 0.0})

        df_scores = pd.DataFrame(factor_rows).sort_values('得分', ascending=False)
        fig = px.bar(
            df_scores, x='得分', y='因子', orientation='h',
            color='得分', color_continuous_scale='RdYlGn',
            color_continuous_midpoint=0,
            title='因子 Z-Score(正=偏多,负=偏空)',
        )
        fig.update_layout(height=550, margin=dict(t=40, b=10),
                          yaxis={'categoryorder': 'total ascending'})
        st.plotly_chart(fig, use_container_width=True)
    except Exception as e:
        st.error(f'因子计算失败: {e}')


def _render_pipeline_score(symbol: str, df: pd.DataFrame) -> None:
    st.subheader('多因子流水线综合评分')
    try:
        from core.factor_pipeline import DynamicWeightPipeline
        pipeline = DynamicWeightPipeline(update_freq_days=21)
        pipeline.add('RSI',            weight=0.15)
        pipeline.add('MACD',           weight=0.12)
        pipeline.add('ATR',            weight=0.08)
        pipeline.add('BollingerBands', weight=0.10)
        pipeline.add('OrderImbalance', weight=0.08)
        pipeline.add('SectorMomentum', weight=0.10)
        pipeline.add('PEPercentile',   weight=0.08)
        pipeline.add('ROEMomentum',    weight=0.08)
        pipeline.add('MarginTrading',  weight=0.07)
        pipeline.add('NorthboundFlow', weight=0.07)
        pipeline.add('MLPrediction',   weight=0.07)

        snap = load_realtime(symbol)
        price = snap.get('price', float(df['close'].iloc[-1]))
        result = pipeline.run(symbol=symbol, data=df, price=price)

        c1, c2, c3 = st.columns(3)
        c1.metric('综合评分', f'{result.combined_score:+.3f}')
        c2.metric('主信号', result.dominant_signal or 'HOLD')
        c3.metric('信号数量', len(result.signals))

        if result.signals:
            sig_rows = [{'因子': s.factor_name, '方向': s.direction,
                         '强度': f'{s.strength:.3f}', '价格': f'{s.price:.2f}'}
                        for s in result.signals]
            st.dataframe(pd.DataFrame(sig_rows), hide_index=True, use_container_width=True)

        try:
            w_dict = pipeline.current_weights()
            if w_dict:
                st.caption('当前动态权重(基于滚动 IC)')
                w_df = pd.DataFrame(list(w_dict.items()), columns=['因子', '权重'])
                st.dataframe(w_df, hide_index=True, use_container_width=True)
        except Exception:
            pass
    except Exception as e:
        st.warning(f'流水线评分失败: {e}')


def _render_nlp_sentiment(symbol: str, df: pd.DataFrame) -> None:
    st.subheader('新闻情感(NewsSentimentFactor)')
    try:
        from core.factors.nlp import NewsSentimentFactor
        # P4-2: 走 backend /data/news/<symbol>
        headlines = load_news_headlines(symbol, n=5)
        if headlines:
            st.caption('最新新闻标题:')
            for h in headlines:
                st.markdown(f'- {h}')
            f_nlp = NewsSentimentFactor(symbol=symbol, use_api=False)
            nlp_vals = f_nlp.evaluate(df)
            latest_nlp = float(nlp_vals.iloc[-1])
            sentiment_label = (
                '🟢 正面' if latest_nlp > 0.2 else
                '🔴 负面' if latest_nlp < -0.2 else '⚪ 中性'
            )
            st.metric('情感得分(Z-score)', f'{latest_nlp:+.3f}', delta=sentiment_label)
        else:
            st.info('未获取到新闻数据(网络限制或标的不支持)')
    except Exception as e:
        st.info(f'NLP 因子未激活(需配置 ANTHROPIC_API_KEY): {e}')


def _render_factor_help() -> None:
    st.info('👈 选择标的后点击「运行因子分析」')
    st.markdown("""
**因子分类说明:**

| 类别 | 因子 | 数量 |
|------|------|------|
| 价格动量 | RSI / Bollinger / MACD / ATR / OrderImbalance | 5 |
| 技术微观 | IntraVWAP / OpenGap / VolAcceleration / BidAskSpread / BuyingPressure / SectorMomentum / IndexRelativeStrength | 7 |
| 基本面 | PEPercentile / ROEMomentum / EarningsSurprise / RevenueGrowth / CashFlowQuality | 5 |
| 情绪 | MarginTrading / NorthboundFlow / ShortInterest | 3 |
| ML 预测 | MLPrediction(XGBoost Walk-Forward) | 1 |
| NLP 情感 | NewsSentiment(东财新闻 + Claude API) | 1 |
""")
