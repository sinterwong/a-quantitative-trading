#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scripts/sensitivity_job.py — 参数敏感性分析任务

用法：
    python scripts/sensitivity_job.py \
        --symbol 000001.SZ \
        --factor RSI \
        --param1 period --p1-values 7,10,14,21,28 \
        --param2 buy_threshold --p2-values 20,25,30,35,40

输出：
    outputs/sensitivity_<symbol>.png  (Sharpe 热力图)
"""

from __future__ import annotations

import argparse
import os
import sys

# ── 路径 ────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

# 禁用代理
for _k in list(os.environ.keys()):
    if 'proxy' in _k.lower():
        del os.environ[_k]


# ── 参数解析 ─────────────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='参数敏感性分析')
    parser.add_argument('--symbol',    default='000001.SZ', help='标的代码')
    parser.add_argument('--factor',    default='RSI',
                        choices=['RSI', 'ATR', 'MACD', 'BollingerBands'],
                        help='因子/策略名称（对应 FactorRegistry）')
    parser.add_argument('--param1',    default='period',     help='参数 1 名称')
    parser.add_argument('--p1-values', default='7,10,14,21,28',
                        help='参数 1 取值列表（逗号分隔）')
    parser.add_argument('--param2',    default='buy_threshold', help='参数 2 名称')
    parser.add_argument('--p2-values', default='20,25,30,35,40',
                        help='参数 2 取值列表（逗号分隔）')
    parser.add_argument('--days',      type=int, default=500,
                        help='历史数据天数（默认 500）')
    parser.add_argument('--output-dir', default=os.path.join(BASE_DIR, 'outputs'),
                        help='热力图输出目录')
    return parser.parse_args()


def parse_values(s: str) -> list:
    """将逗号分隔字符串解析为数字列表（尽量转 int，否则 float）。"""
    result = []
    for v in s.split(','):
        v = v.strip()
        if not v:
            continue
        try:
            result.append(int(v))
        except ValueError:
            result.append(float(v))
    return result


def main() -> None:
    args = parse_args()

    p1_values = parse_values(args.p1_values)
    p2_values = parse_values(args.p2_values)

    if not p1_values or not p2_values:
        print('ERROR: 参数取值列表为空', file=sys.stderr)
        sys.exit(1)

    print(f'[sensitivity_job] 标的={args.symbol}  因子={args.factor}')
    print(f'  {args.param1} = {p1_values}')
    print(f'  {args.param2} = {p2_values}')

    # ── 拉取历史数据 ─────────────────────────────────────────────────────────
    print(f'[sensitivity_job] 拉取 {args.days} 天历史数据...')
    try:
        from core.data_layer import DataLayer
        import pandas as pd
        dl = DataLayer()
        df = dl.get_bars(args.symbol, days=args.days)
        if df is None or df.empty:
            print('ERROR: 数据获取失败', file=sys.stderr)
            sys.exit(1)
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'])
            df = df.set_index('date')
        df = df.sort_index()
        df = df[['open', 'high', 'low', 'close', 'volume']]
        print(f'[sensitivity_job] 获取到 {len(df)} 条 K 线')
    except Exception as e:
        print(f'ERROR: 数据层异常: {e}', file=sys.stderr)
        sys.exit(1)

    # ── 获取因子类 ───────────────────────────────────────────────────────────
    try:
        from core.factor_registry import registry
        factor_instance = registry.create(args.factor)
        factor_class = type(factor_instance)
    except Exception as e:
        print(f'ERROR: 因子加载失败 [{args.factor}]: {e}', file=sys.stderr)
        sys.exit(1)

    # ── 验证参数名 ───────────────────────────────────────────────────────────
    import inspect
    valid_params = list(inspect.signature(factor_class.__init__).parameters.keys())
    valid_params = [p for p in valid_params if p not in ('self', 'symbol')]
    for pname in (args.param1, args.param2):
        if pname not in valid_params:
            print(f'WARNING: 参数 "{pname}" 不在因子 {args.factor} 的参数列表中。'
                  f'可用参数: {valid_params}', file=sys.stderr)

    # ── 运行敏感性分析 ───────────────────────────────────────────────────────
    print('[sensitivity_job] 开始网格扫描...')
    try:
        from core.walkforward import SensitivityAnalyzer
        sharpe_matrix = SensitivityAnalyzer.run(
            df=df,
            symbol=args.symbol,
            factor_class=factor_class,
            param_axis1=(args.param1, p1_values),
            param_axis2=(args.param2, p2_values),
        )
    except Exception as e:
        print(f'ERROR: SensitivityAnalyzer 运行失败: {e}', file=sys.stderr)
        import traceback; traceback.print_exc()
        sys.exit(1)

    print('[sensitivity_job] 网格扫描完成，结果：')
    print(sharpe_matrix.to_string())

    # 峰值稳健度
    try:
        ratio = SensitivityAnalyzer.peak_sensitivity_ratio(sharpe_matrix)
        print(f'[sensitivity_job] 峰值稳健度: {ratio:.3f} '
              f'({"合格 ≥0.5" if ratio >= 0.5 else "不足 <0.5，可能过拟合"})')
    except Exception:
        pass

    # ── 保存热力图 ───────────────────────────────────────────────────────────
    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(args.output_dir, f'sensitivity_{args.symbol}.png')
    try:
        SensitivityAnalyzer.plot_heatmap(
            sharpe_matrix,
            output_path=out_path,
            title=f'{args.factor} Sensitivity — {args.symbol}',
            xlabel=args.param2,
            ylabel=args.param1,
        )
        print(f'[sensitivity_job] 热力图已保存: {out_path}')
    except Exception as e:
        print(f'WARNING: 热力图保存失败: {e}', file=sys.stderr)

    print('[sensitivity_job] 完成')


if __name__ == '__main__':
    main()
