"""
core/report_exporter.py — 回测报告 PDF 导出

功能：
  - 接受 BacktestResult（core/backtest_engine.py）生成专业 PDF 报告
  - 内容：封面摘要 / 净值曲线 / 回撤曲线 / 绩效指标表 / 交易统计 / 因子 IC
  - 图表通过 matplotlib 生成后嵌入 PDF（reportlab）
  - WFA 结果和因子 IC 数据可选传入

依赖：
  pip install reportlab   (已安装)
  matplotlib              (已在项目中使用)

用法：
    from core.report_exporter import BacktestReportExporter
    from core.backtest_engine import BacktestEngine, BacktestConfig

    result = BacktestEngine(config).run()
    exporter = BacktestReportExporter(result)
    path = exporter.export('outputs/backtest_report_20260430.pdf')
    print(f'报告已生成: {path}')

    # 携带 WFA 结果和因子 IC
    exporter.export(
        'report.pdf',
        wfa_results={'train_sharpe': [0.8, 1.1], 'test_sharpe': [0.6, 0.9]},
        factor_ic={'RSI': {'ic_mean': 0.032, 'ic_ir': 0.48}},
    )
"""

from __future__ import annotations

import io
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger('core.report_exporter')

# ---------------------------------------------------------------------------
# 颜色 / 样式常量
# ---------------------------------------------------------------------------
_BLUE  = (0.13, 0.37, 0.67)
_GREEN = (0.18, 0.60, 0.34)
_RED   = (0.80, 0.18, 0.18)
_GREY  = (0.55, 0.55, 0.55)
_WHITE = (1.0, 1.0, 1.0)
_BLACK = (0.0, 0.0, 0.0)
_LIGHT = (0.95, 0.96, 0.98)


def _color_sign(value: float):
    """正值绿色，负值红色。"""
    return _GREEN if value >= 0 else _RED


# ---------------------------------------------------------------------------
# BacktestReportExporter
# ---------------------------------------------------------------------------

class BacktestReportExporter:
    """
    将 BacktestResult 导出为 PDF 报告。

    Parameters
    ----------
    result : BacktestResult
        由 BacktestEngine.run() 返回的回测结果。
    title  : str
        报告标题（默认 '量化策略回测报告'）。
    """

    def __init__(self, result, title: str = '量化策略回测报告'):
        self.result = result
        self.title = title

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def export(
        self,
        output_path: str,
        wfa_results: Optional[Dict[str, List[float]]] = None,
        factor_ic: Optional[Dict[str, Dict[str, float]]] = None,
    ) -> str:
        """
        生成 PDF 报告并保存。

        Parameters
        ----------
        output_path  : 输出 PDF 路径
        wfa_results  : Walk-Forward 分析结果（可选），如
                       {'train_sharpe': [...], 'test_sharpe': [...]}
        factor_ic    : 因子 IC 数据（可选），如
                       {'RSI': {'ic_mean': 0.03, 'ic_ir': 0.45}, ...}

        Returns
        -------
        str — 实际写入的文件路径
        """
        try:
            from reportlab.lib.pagesizes import A4
            from reportlab.platypus import SimpleDocTemplate
        except ImportError:
            raise ImportError(
                "reportlab 未安装。请运行: pip install reportlab"
            )

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

        doc = SimpleDocTemplate(
            output_path,
            pagesize=A4,
            leftMargin=40, rightMargin=40,
            topMargin=40, bottomMargin=40,
            title=self.title,
        )

        story = []
        story += self._build_cover()
        story += self._build_metrics_table()
        story += self._build_equity_chart()
        story += self._build_drawdown_chart()
        story += self._build_trade_stats()
        if wfa_results:
            story += self._build_wfa_section(wfa_results)
        if factor_ic:
            story += self._build_factor_ic_table(factor_ic)

        doc.build(story)
        logger.info('PDF report exported: %s', output_path)
        return output_path

    # ------------------------------------------------------------------
    # 封面
    # ------------------------------------------------------------------

    def _build_cover(self):
        from reportlab.platypus import Paragraph, Spacer, HRFlowable
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import cm
        from reportlab.lib.enums import TA_CENTER

        styles = getSampleStyleSheet()
        elements = []

        title_style = ParagraphStyle(
            'Title', parent=styles['Title'],
            fontSize=22, textColor=_rl_color(_BLUE),
            alignment=TA_CENTER, spaceAfter=6,
        )
        sub_style = ParagraphStyle(
            'Sub', parent=styles['Normal'],
            fontSize=11, textColor=_rl_color(_GREY),
            alignment=TA_CENTER, spaceAfter=4,
        )

        elements.append(Spacer(1, 1.5 * cm))
        elements.append(Paragraph(self.title, title_style))

        r = self.result
        cfg = r.config
        date_range = ''
        if not r.equity_curve.empty:
            start = str(r.equity_curve.index[0])[:10]
            end   = str(r.equity_curve.index[-1])[:10]
            date_range = f'{start} ~ {end}'

        elements.append(Paragraph(date_range, sub_style))
        elements.append(Paragraph(
            f'生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M")}',
            sub_style,
        ))
        elements.append(Spacer(1, 0.5 * cm))
        elements.append(HRFlowable(width='100%', thickness=1,
                                    color=_rl_color(_BLUE)))
        elements.append(Spacer(1, 0.4 * cm))

        # 关键指标快速预览（2 行 4 列）
        metrics = [
            ('总收益', f'{r.total_return*100:.2f}%', r.total_return),
            ('年化收益', f'{r.annual_return*100:.2f}%', r.annual_return),
            ('夏普比率', f'{r.sharpe:.3f}', r.sharpe),
            ('最大回撤', f'{r.max_drawdown_pct*100:.2f}%', -r.max_drawdown_pct),
            ('卡玛比率', f'{r.calmar_ratio:.3f}', r.calmar_ratio),
            ('胜率', f'{r.win_rate*100:.1f}%', r.win_rate - 0.5),
            ('交易次数', str(r.n_trades), 0),
            ('因子 IC', f'{r.factor_ic:.4f}', r.factor_ic),
        ]
        elements += self._kpi_grid(metrics)
        elements.append(Spacer(1, 0.3 * cm))
        return elements

    def _kpi_grid(self, metrics):
        """生成 2 行 4 列 KPI 卡片。"""
        from reportlab.platypus import Table, TableStyle
        from reportlab.lib.units import cm

        cols = 4
        rows = (len(metrics) + cols - 1) // cols
        table_data = []
        for row in range(rows):
            row_cells = []
            for col in range(cols):
                idx = row * cols + col
                if idx < len(metrics):
                    name, val, sign = metrics[idx]
                    color = _color_sign(sign)
                    cell = f'<font size="9" color="grey">{name}</font><br/>' \
                           f'<font size="14" color="#{_hex(*color)}">{val}</font>'
                    row_cells.append(cell)
                else:
                    row_cells.append('')
            table_data.append(row_cells)

        from reportlab.platypus import Paragraph
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.enums import TA_CENTER
        cell_style = ParagraphStyle('kpi', fontSize=9, alignment=TA_CENTER)
        parsed = [[Paragraph(c, cell_style) for c in row] for row in table_data]

        col_width = (495 - 10 * (cols - 1)) / cols
        tbl = Table(parsed, colWidths=[col_width] * cols, rowHeights=1.2 * cm)
        tbl.setStyle(_kpi_table_style())
        return [tbl]

    # ------------------------------------------------------------------
    # 绩效指标表
    # ------------------------------------------------------------------

    def _build_metrics_table(self):
        from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm

        elements = [Spacer(1, 0.5 * cm)]
        header_style = ParagraphStyle('h2', fontSize=13, textColor=_rl_color(_BLUE),
                                       spaceAfter=6)
        elements.append(Paragraph('▌ 绩效指标', header_style))

        r = self.result
        data = [
            ['指标', '数值', '指标', '数值'],
            ['总收益率', f'{r.total_return*100:.2f}%', '年化收益率', f'{r.annual_return*100:.2f}%'],
            ['年化波动率', f'{r.annual_vol*100:.2f}%', '夏普比率', f'{r.sharpe:.4f}'],
            ['索提诺比率', f'{r.sortino_ratio:.4f}', '卡玛比率', f'{r.calmar_ratio:.4f}'],
            ['最大回撤', f'{r.max_drawdown_pct*100:.2f}%', '胜率', f'{r.win_rate*100:.1f}%'],
            ['盈亏比', f'{r.profit_factor:.3f}', '总交易次数', str(r.n_trades)],
            ['均持仓周期', f'{r.avg_holding_period/3600:.1f}h', '因子 IC / IR',
             f'{r.factor_ic:.4f} / {r.factor_ir:.4f}'],
        ]

        tbl = Table(data, colWidths=[120, 110, 140, 110])
        tbl.setStyle(_metrics_table_style())
        elements.append(tbl)
        return elements

    # ------------------------------------------------------------------
    # 净值曲线图
    # ------------------------------------------------------------------

    def _build_equity_chart(self):
        from reportlab.platypus import Spacer, Image, Paragraph
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm

        elements = [Spacer(1, 0.5 * cm)]
        header_style = ParagraphStyle('h2', fontSize=13, textColor=_rl_color(_BLUE),
                                       spaceAfter=6)
        elements.append(Paragraph('▌ 净值曲线', header_style))

        img_buf = self._render_equity_chart()
        if img_buf:
            elements.append(Image(img_buf, width=490, height=180))
        return elements

    def _render_equity_chart(self) -> Optional[io.BytesIO]:
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates

            eq = self.result.equity_curve
            if eq.empty:
                return None

            fig, ax = plt.subplots(figsize=(9, 3.2), dpi=120)
            ax.plot(eq.index, eq.values, color='#2163AC', linewidth=1.2, label='净值')
            ax.axhline(1.0, color='#999', linewidth=0.8, linestyle='--', alpha=0.6)
            ax.fill_between(eq.index, 1.0, eq.values,
                             where=(eq.values >= 1.0), alpha=0.12, color='#2163AC')
            ax.fill_between(eq.index, 1.0, eq.values,
                             where=(eq.values < 1.0), alpha=0.15, color='#CC2E2E')
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
            ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
            plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha='right', fontsize=7)
            ax.set_ylabel('净值', fontsize=8)
            ax.grid(True, alpha=0.25)
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            plt.tight_layout(pad=0.5)

            buf = io.BytesIO()
            fig.savefig(buf, format='png', bbox_inches='tight')
            plt.close(fig)
            buf.seek(0)
            return buf
        except Exception as e:
            logger.warning('Equity chart render failed: %s', e)
            return None

    # ------------------------------------------------------------------
    # 回撤曲线图
    # ------------------------------------------------------------------

    def _build_drawdown_chart(self):
        from reportlab.platypus import Spacer, Image, Paragraph
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm

        elements = [Spacer(1, 0.4 * cm)]
        header_style = ParagraphStyle('h2', fontSize=13, textColor=_rl_color(_BLUE),
                                       spaceAfter=6)
        elements.append(Paragraph('▌ 回撤曲线', header_style))

        img_buf = self._render_drawdown_chart()
        if img_buf:
            elements.append(Image(img_buf, width=490, height=140))
        return elements

    def _render_drawdown_chart(self) -> Optional[io.BytesIO]:
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates

            eq = self.result.equity_curve
            if eq.empty:
                return None

            roll_max = eq.cummax()
            drawdown = (eq - roll_max) / roll_max

            fig, ax = plt.subplots(figsize=(9, 2.4), dpi=120)
            ax.fill_between(drawdown.index, drawdown.values, 0,
                             color='#CC2E2E', alpha=0.55, label='回撤')
            ax.plot(drawdown.index, drawdown.values, color='#CC2E2E', linewidth=0.8)
            ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f'{y*100:.0f}%'))
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
            ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
            plt.setp(ax.xaxis.get_majorticklabels(), rotation=30, ha='right', fontsize=7)
            ax.set_ylabel('回撤', fontsize=8)
            ax.grid(True, alpha=0.25)
            ax.spines['top'].set_visible(False)
            ax.spines['right'].set_visible(False)
            plt.tight_layout(pad=0.5)

            buf = io.BytesIO()
            fig.savefig(buf, format='png', bbox_inches='tight')
            plt.close(fig)
            buf.seek(0)
            return buf
        except Exception as e:
            logger.warning('Drawdown chart render failed: %s', e)
            return None

    # ------------------------------------------------------------------
    # 交易统计
    # ------------------------------------------------------------------

    def _build_trade_stats(self):
        from reportlab.platypus import Spacer, Paragraph, Table
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm

        elements = [Spacer(1, 0.4 * cm)]
        header_style = ParagraphStyle('h2', fontSize=13, textColor=_rl_color(_BLUE),
                                       spaceAfter=6)
        elements.append(Paragraph('▌ 交易统计', header_style))

        trades = self.result.trades
        if not trades:
            elements.append(Paragraph('无交易记录', ParagraphStyle('n', fontSize=9)))
            return elements

        pnls = [t.pnl for t in trades if hasattr(t, 'pnl')]
        if not pnls:
            return elements

        pnl_arr = np.array(pnls)
        wins = pnl_arr[pnl_arr > 0]
        losses = pnl_arr[pnl_arr <= 0]

        data = [
            ['统计项', '数值', '统计项', '数值'],
            ['总交易次数', str(len(pnls)),
             '盈利次数', str(len(wins))],
            ['亏损次数', str(len(losses)),
             '胜率', f'{len(wins)/len(pnls)*100:.1f}%' if pnls else 'N/A'],
            ['平均盈利', f'{wins.mean():.2f}' if len(wins) else 'N/A',
             '平均亏损', f'{losses.mean():.2f}' if len(losses) else 'N/A'],
            ['最大单笔盈利', f'{pnl_arr.max():.2f}',
             '最大单笔亏损', f'{pnl_arr.min():.2f}'],
            ['总盈亏', f'{pnl_arr.sum():.2f}',
             '盈亏比', f'{abs(wins.mean()/losses.mean()):.3f}'
             if len(wins) and len(losses) else 'N/A'],
        ]

        tbl = Table(data, colWidths=[120, 110, 140, 110])
        tbl.setStyle(_metrics_table_style())
        elements.append(tbl)
        return elements

    # ------------------------------------------------------------------
    # WFA 结果（可选）
    # ------------------------------------------------------------------

    def _build_wfa_section(self, wfa_results: Dict[str, List[float]]):
        from reportlab.platypus import Spacer, Paragraph, Table
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm

        elements = [Spacer(1, 0.4 * cm)]
        header_style = ParagraphStyle('h2', fontSize=13, textColor=_rl_color(_BLUE),
                                       spaceAfter=6)
        elements.append(Paragraph('▌ Walk-Forward 分析', header_style))

        train_sharpe = wfa_results.get('train_sharpe', [])
        test_sharpe  = wfa_results.get('test_sharpe', [])

        if not train_sharpe:
            elements.append(Paragraph('无 WFA 数据', ParagraphStyle('n', fontSize=9)))
            return elements

        header = ['窗口'] + [f'W{i+1}' for i in range(len(train_sharpe))]
        train_row = ['训练 Sharpe'] + [f'{s:.3f}' for s in train_sharpe]
        test_row  = ['测试 Sharpe'] + [f'{s:.3f}' for s in test_sharpe[:len(train_sharpe)]]

        n_cols = len(header)
        col_w = 490 / n_cols
        tbl = Table([header, train_row, test_row], colWidths=[col_w] * n_cols)
        tbl.setStyle(_metrics_table_style())
        elements.append(tbl)
        return elements

    # ------------------------------------------------------------------
    # 因子 IC 表格（可选）
    # ------------------------------------------------------------------

    def _build_factor_ic_table(self, factor_ic: Dict[str, Dict[str, float]]):
        from reportlab.platypus import Spacer, Paragraph, Table
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import cm

        elements = [Spacer(1, 0.4 * cm)]
        header_style = ParagraphStyle('h2', fontSize=13, textColor=_rl_color(_BLUE),
                                       spaceAfter=6)
        elements.append(Paragraph('▌ 因子 IC 汇总', header_style))

        data = [['因子名称', 'IC 均值', 'IC IR', '评级']]
        for name, stats in sorted(factor_ic.items(),
                                   key=lambda x: x[1].get('ic_mean', 0), reverse=True):
            ic_mean = stats.get('ic_mean', 0.0)
            ic_ir   = stats.get('ic_ir', 0.0)
            if ic_mean > 0.03 and ic_ir > 0.5:
                grade = '★★★ 有效'
            elif ic_mean > 0.02 and ic_ir > 0.3:
                grade = '★★ 较好'
            elif ic_mean > 0:
                grade = '★ 微弱'
            else:
                grade = '✗ 无效'
            data.append([name, f'{ic_mean:.4f}', f'{ic_ir:.4f}', grade])

        tbl = Table(data, colWidths=[160, 100, 100, 110])
        tbl.setStyle(_metrics_table_style())
        elements.append(tbl)
        return elements


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _rl_color(rgb):
    from reportlab.lib.colors import Color
    return Color(*rgb)


def _hex(r, g, b) -> str:
    return '%02X%02X%02X' % (int(r*255), int(g*255), int(b*255))


def _kpi_table_style():
    from reportlab.platypus import TableStyle
    from reportlab.lib.colors import white, HexColor
    return TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), HexColor('#F0F3F8')),
        ('ROWBACKGROUND', (0, 0), (-1, -1), [HexColor('#F0F3F8'), HexColor('#E8EDF5')]),
        ('BOX', (0, 0), (-1, -1), 0.5, HexColor('#C8D0DC')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, HexColor('#C8D0DC')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ])


def _metrics_table_style():
    from reportlab.platypus import TableStyle
    from reportlab.lib.colors import HexColor, white
    return TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), HexColor('#2163AC')),
        ('TEXTCOLOR', (0, 0), (-1, 0), white),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [HexColor('#F7F9FC'), white]),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 0.4, HexColor('#D0D8E4')),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ])
