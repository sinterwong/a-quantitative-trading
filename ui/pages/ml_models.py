"""
ui/pages/ml_models.py — 🤖 ML 模型 (P4-1 阶段二)

researcher 视图:模型注册表 + Walk-Forward 训练 + 特征重要性。

⚠ 重大架构债(下个周期重构必做):
  研究类 UI 不应承担"训练"这种长任务:
  - 当前在 Streamlit 主线程同步训练 1-3 分钟,浏览器一刷新就丢
  - 应改为:UI POST /tasks/train → 返回 task_id → 轮询 GET /tasks/<id>
  - core.ml.* 直连应改为 backend 端点(/ml/registry, /ml/train, /ml/importance)

短期 fallback(本次只做结构性拆分,不改业务):
  保留 core.ml.* 直连,标注为已知债,具体迁移见 docs/UI_REFACTOR_PROPOSAL.md
"""

from __future__ import annotations

import json
import os

import pandas as pd
import plotly.express as px
import streamlit as st

from ui.data import DATA_DIR, load_trading_config, make_price_df


def render_page() -> None:
    st.title('🤖 ML 模型')
    st.caption('XGBoost Walk-Forward 训练 · 模型注册表 · 特征重要性')

    try:
        from core.ml.model_registry import ModelRegistry  # noqa: F401
        from core.ml.price_predictor import MLPredictionFactor  # noqa: F401
    except ImportError as e:
        st.error(f'ML 模块加载失败: {e}')
        return

    tab_registry, tab_train, tab_importance = st.tabs(
        ['📦 模型注册表', '🚀 训练新模型', '📊 特征重要性']
    )
    with tab_registry:
        _render_registry_tab()
    with tab_train:
        _render_train_tab()
    with tab_importance:
        _render_importance_tab()


def _render_registry_tab() -> None:
    st.subheader('已训练模型')
    models_dir = os.path.join(DATA_DIR, 'ml_models')
    if not os.path.exists(models_dir):
        st.info('模型存储目录不存在,训练后将自动创建。')
        return

    model_rows = []
    for sym_dir in sorted(os.listdir(models_dir)):
        sym_path = os.path.join(models_dir, sym_dir)
        if not os.path.isdir(sym_path):
            continue
        for model_type in sorted(os.listdir(sym_path)):
            type_path = os.path.join(sym_path, model_type)
            if not os.path.isdir(type_path):
                continue
            meta_path = os.path.join(type_path, 'meta.json')
            if os.path.exists(meta_path):
                try:
                    with open(meta_path) as mf:
                        meta = json.load(mf)
                    model_rows.append({
                        '标的': sym_dir,
                        '模型类型': model_type,
                        '版本': meta.get('version', '—'),
                        '训练样本数': meta.get('n_samples', '—'),
                        '特征数': meta.get('n_features', '—'),
                        'OOS AUC': f"{meta.get('oos_auc', 0):.3f}" if meta.get('oos_auc') else '—',
                        '训练时间': str(meta.get('trained_at', ''))[:16],
                    })
                except Exception:
                    pass
    if model_rows:
        st.dataframe(pd.DataFrame(model_rows), hide_index=True, use_container_width=True)
    else:
        st.info('暂无已训练模型。请在「训练新模型」标签页训练。')


def _render_train_tab() -> None:
    from core.ml.price_predictor import MLPredictionFactor

    st.subheader('Walk-Forward 训练配置')
    st.caption('训练窗口 252 天 / 验证窗口 63 天 / 步长 21 天(防止过拟合)')
    st.warning(
        '⚠ 当前训练在 UI 同步执行(1-3 分钟),浏览器刷新会丢失进度。'
        '生产化方案见 docs/UI_REFACTOR_PROPOSAL.md。'
    )

    cfg = load_trading_config()
    live_syms = cfg.get('live_symbols', [])
    sym_options = [s['symbol'] for s in live_syms] if live_syms else ['000001.SZ', '600519.SH']

    c1, c2 = st.columns(2)
    with c1:
        train_symbol = st.selectbox('训练标的', sym_options)
        forward_days = st.selectbox('预测周期(天)', [1, 2, 5], index=1)
    with c2:
        data_days = st.slider('历史数据长度(天)', 300, 800, 500, step=50)
        use_wf = st.checkbox('使用 Walk-Forward 验证', value=True)

    if st.button('开始训练', type='primary'):
        with st.spinner(f'拉取 {train_symbol} 数据...'):
            df_train = make_price_df(train_symbol, data_days)
        if df_train is None or len(df_train) < 100:
            st.error('历史数据不足(< 100 天),无法训练。')
            return

        with st.spinner('Walk-Forward 训练中(可能需要 1-3 分钟)...'):
            try:
                factor = MLPredictionFactor(symbol=train_symbol, forward_days=forward_days)
                wf_result = factor.fit(df_train, use_walk_forward=use_wf)

                st.success('训练完成!')
                col_r1, col_r2, col_r3 = st.columns(3)
                if hasattr(wf_result, 'oos_accuracy') and wf_result.oos_accuracy is not None:
                    col_r1.metric('OOS 准确率', f'{wf_result.oos_accuracy:.3f}')
                if hasattr(wf_result, 'oos_auc') and wf_result.oos_auc is not None:
                    col_r2.metric('OOS AUC', f'{wf_result.oos_auc:.3f}')
                if hasattr(wf_result, 'n_folds'):
                    col_r3.metric('验证折数', wf_result.n_folds)

                if hasattr(wf_result, 'fold_metrics') and wf_result.fold_metrics:
                    aucs = [w.get('auc', 0) for w in wf_result.fold_metrics]
                    fig = px.bar(
                        x=list(range(1, len(aucs) + 1)),
                        y=aucs, color=aucs,
                        color_continuous_scale='RdYlGn',
                        labels={'x': '验证折', 'y': 'OOS AUC'},
                        title='各折 OOS AUC',
                    )
                    fig.add_hline(y=0.5, line_dash='dash', line_color='gray')
                    st.plotly_chart(fig, use_container_width=True)
            except Exception as e:
                st.error(f'训练失败: {e}')


def _render_importance_tab() -> None:
    from core.ml.price_predictor import MLPredictionFactor

    st.subheader('特征重要性分析')
    cfg2 = load_trading_config()
    live_syms2 = cfg2.get('live_symbols', [])
    sym_opts2 = [s['symbol'] for s in live_syms2] if live_syms2 else ['000001.SZ']
    imp_symbol = st.selectbox('选择标的', sym_opts2, key='imp_sym')

    if st.button('加载特征重要性'):
        try:
            factor_imp = MLPredictionFactor(symbol=imp_symbol)
            if factor_imp.load():
                predictor = getattr(factor_imp, '_predictor', None)
                importance = predictor.feature_importance() if predictor is not None else pd.Series(dtype=float)
                if importance is not None and not importance.empty:
                    df_imp = pd.DataFrame({
                        '特征': importance.index, '重要性': importance.values,
                    }).head(20)
                    fig = px.bar(
                        df_imp, x='重要性', y='特征', orientation='h',
                        title=f'{imp_symbol} Top-20 特征重要性',
                        color='重要性', color_continuous_scale='Blues',
                    )
                    fig.update_layout(height=500, yaxis={'categoryorder': 'total ascending'})
                    st.plotly_chart(fig, use_container_width=True)
                else:
                    st.warning('模型未返回特征重要性(可能是非树模型)')
            else:
                st.warning(f'未找到 {imp_symbol} 的已训练模型,请先训练。')
        except Exception as e:
            st.error(f'加载失败: {e}')
