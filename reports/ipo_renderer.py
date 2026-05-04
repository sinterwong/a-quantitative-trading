"""
reports/ipo_renderer.py — IPO Stars 飞书 Markdown 报告渲染器（Phase 7）

功能：
  - 将 IPOAnalysisReport 渲染为飞书友好的 Markdown 格式
  - 支持完整报告、摘要、暗盘快报、上市后复盘等多种渲染模式
  - 通过 AlertManager 推送

用法：
    from reports.ipo_renderer import IPORenderer
    renderer = IPORenderer()
    markdown = renderer.render(report)
    alert_manager.send_markdown(markdown)
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from core.ipo_report import (
    IPOAnalysisReport,
    IPOStatistics,
    IPOPerformanceMetrics,
    IPOStockRecord,
    MarketBoard,
)

_log = logging.getLogger('ipo_renderer')


# ---------------------------------------------------------------------------
# 限价单档位
# ---------------------------------------------------------------------------

class OrderTier(Enum):
    """限价单档位。"""
    CONSERVATIVE = "保守档"   # 最低买入价，暗盘/首日低配
    BALANCED = "平衡档"        # 中性价格区间
    AGGRESSIVE = "进取档"     # 较高买入价，高配

    def __str__(self) -> str:
        return self.value


# ---------------------------------------------------------------------------
# IPOLimitOrderRec — 限价单建议（从 IPOPerformanceMetrics 构造或直接构造）
# ---------------------------------------------------------------------------

@dataclass
class IPOLimitOrderRec:
    """
    单档限价单建议。

    Attributes
    ----------
    tier : OrderTier
        档位（保守/平衡/进取）。
    entry_price : float
        建议买入价（元）。
    target_price : float
        目标价（元）。
    stop_loss : float
        止损价（元）。
    position_size : float
        仓位权重（小数）。
    expected_return : float
        预期收益率（小数）。
    confidence : float
        置信度（0~1）。
    note : str
        备注说明。
    """

    tier: OrderTier
    entry_price: float
    target_price: float
    stop_loss: float
    position_size: float = 0.0
    expected_return: float = 0.0
    confidence: float = 1.0
    note: str = ""

    @property
    def risk_reward_ratio(self) -> float:
        """风险收益比（近风险：目标收益/（ entry - stop_loss））。"""
        diff = self.target_price - self.entry_price
        loss = self.entry_price - self.stop_loss
        if loss <= 0:
            return 0.0
        return diff / loss

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tier": str(self.tier),
            "entry_price": self.entry_price,
            "target_price": self.target_price,
            "stop_loss": self.stop_loss,
            "position_size": self.position_size,
            "expected_return": self.expected_return,
            "confidence": self.confidence,
            "note": self.note,
            "risk_reward_ratio": self.risk_reward_ratio,
        }


# ---------------------------------------------------------------------------
# 主渲染器
# ---------------------------------------------------------------------------

from dataclasses import dataclass
from enum import Enum


class IPORenderer:
    """
    IPO 分析报告飞书渲染器。

    将 IPOAnalysisReport（A 股打新报告）渲染为飞书友好的 Markdown 格式，
    适用于 AlertManager 推送。提供多种渲染模式：

    - ``render()``       — 完整分析报告
    - ``render_summary()`` — 摘要版（用于通知提醒）
    - ``render_dark_pool_update()``  — 暗盘快报
    - ``render_post_listing_review()`` — 上市后复盘

    颜色通过 emoji 表达：
      - BUY 🟢    — 推荐参与
      - NEUTRAL 🟡 — 谨慎参与
      - SKIP 🔴   — 不建议参与

    飞书支持完整的 Markdown 表格，本渲染器充分利用这一特性。
    """

    # 星级映射（A 股五星 → 飞书星级 emoji）
    STAR_EMOJI = {
        1: "⭐",
        2: "⭐⭐",
        3: "⭐⭐⭐",
        4: "⭐⭐⭐⭐",
        5: "⭐⭐⭐⭐⭐",
    }

    # 评级映射
    RATING_EMOJI = {
        "BUY": "🟢 BUY",
        "NEUTRAL": "🟡 NEUTRAL",
        "SKIP": "🔴 SKIP",
    }

    # 涨跌 emoji（A 股用）
    UP_EMOJI = "📈"
    DOWN_EMOJI = "📉"

    # 板块 emoji
    BOARD_EMOJI = {
        MarketBoard.MAIN_BOARD: "🏛️ 主板",
        MarketBoard.GEM: "🚀 创业板",
        MarketBoard.STAR: "🌟 科创板",
        MarketBoard.NEEQ: "📋 北交所",
    }

    # --------------------------------------------------------------------------
    # 公开 API
    # --------------------------------------------------------------------------

    def render(self, report: IPOAnalysisReport) -> str:
        """
        渲染完整分析报告为 Markdown。

        报告结构：
          1. 标题 + 综合评级 + 置信度条
          2. 核心结论（发行价区间、预测涨幅区间、胜率）
          3. 三档限价单建议（保守/平衡/进取 × 暗盘/首日）
          4. 关键依据（板块统计 / 行业分布 / 条款 / 情绪）
          5. 风险提示
          6. 数据可信度评分

        Parameters
        ----------
        report : IPOAnalysisReport
            IPO 分析报告数据。

        Returns
        -------
        str
            飞书友好的 Markdown 格式字符串。
        """
        sections: List[str] = []

        # ── 1. 标题 + 综合评级 + 置信度 ────────────────────────────────────
        sections.append(self._render_header(report))

        # ── 2. 核心结论 ────────────────────────────────────────────────────
        sections.append(self._render_core_conclusions(report))

        # ── 3. 三档限价单建议 ───────────────────────────────────────────────
        sections.append(self._render_limit_order_section(report))

        # ── 4. 关键依据 ────────────────────────────────────────────────────
        sections.append(self._render_key_rationale(report))

        # ── 5. 风险提示 ────────────────────────────────────────────────────
        sections.append(self._render_risk_warnings(report))

        # ── 6. 数据可信度评分 ─────────────────────────────────────────────
        sections.append(self._render_data_quality(report))

        return "\n\n".join(sections)

    def render_summary(
        self, report: IPOAnalysisReport, max_chars: int = 2000
    ) -> str:
        """
        渲染摘要版（用于提醒通知）。

        截断到 ``max_chars``，保留最核心结论（评级、胜率、预期收益）。

        Parameters
        ----------
        report : IPOAnalysisReport
            IPO 分析报告。
        max_chars : int
            最大字符数（默认 2000）。

        Returns
        -------
        str
            截断后的摘要 Markdown。
        """
        parts: List[str] = []

        # 标题行
        stats = report.statistics
        if stats:
            rating_text = self._overall_rating_text(stats)
            parts.append(
                f"## 📊 IPO 打新快报 | {report.period_start} ~ {report.period_end}\n"
                f"**{rating_text}**  |  "
                f"样本 {stats.total_ipo_count} 只  |  "
                f"平均首日收益 **{stats.avg_first_day_return*100:+.2f}%**"
            )
        else:
            parts.append(f"## 📊 IPO 打新快报 | 无统计数据")

        # 星级分布
        if stats:
            parts.append(
                f"五星 {stats.star5_count}  四星 {stats.star4_count}  "
                f"三星 {stats.star3_count}  |  "
                f"破发率 {stats.loss_rate*100:.1f}%"
            )

        # 个股 top-5（按首日收益排序）
        if report.stocks:
            top = report.get_top_n(5, by="first_day_return")
            rows = []
            for s in top:
                ret = s.listing_first_day_return
                emoji = self.UP_EMOJI if ret >= 0 else self.DOWN_EMOJI
                rows.append(
                    f"| {s.stock_name} | {s.stock_code} | "
                    f"{emoji} {ret*100:+.2f}% | "
                    f"发行价 **{s.issue_price:.2f}** |"
                )
            parts.append(
                "### 🔥 首日收益 Top-5\n\n"
                "| 名称 | 代码 | 首日收益 | 发行价 |\n"
                "|------|------|----------|--------|\n"
                + "\n".join(rows)
            )

        # 合并
        text = "\n\n".join(parts)
        if len(text) > max_chars:
            text = text[:max_chars - 3] + "..."
        return text

    def render_dark_pool_update(
        self,
        stock_code: str,
        name_cn: str,
        dark_pool_price: float,
        predicted_price: float,
        adjustment_note: str,
    ) -> str:
        """
        渲染暗盘快报（暗盘行情出来后追加到原报告）。

        当暗盘价格与预测价格出现明显偏离时，触发报告更新，
        帮助投资者及时调整首日策略。

        Parameters
        ----------
        stock_code : str
            股票代码。
        name_cn : str
            股票名称。
        dark_pool_price : float
            暗盘成交价（元）。
        predicted_price : float
            暗盘前预测价格（元）。
        adjustment_note : str
            调整说明。

        Returns
        -------
        str
            飞书 Markdown 格式的暗盘快报。
        """
        diff = dark_pool_price - predicted_price
        diff_pct = (diff / predicted_price * 100) if predicted_price > 0 else 0

        if diff_pct > 0:
            direction_emoji = self.UP_EMOJI
            direction_text = "高于"
        else:
            direction_emoji = self.DOWN_EMOJI
            direction_text = "低于"

        # 颜色指示（暗盘涨幅）
        # （暗盘偏离度已在 diff_pct 中体现）

        parts = [
            "## 🌙 暗盘快报更新\n",
            f"**{name_cn}（{stock_code}）** 暗盘已出\n",
            self._render_table(
                headers=["指标", "数值"],
                rows=[
                    ["暗盘价格", f"**{dark_pool_price:.3f}** 元"],
                    ["预测价格", f"**{predicted_price:.3f}** 元"],
                    ["偏差", f"{direction_emoji} {direction_text}预测 **{diff_pct:+.2f}%**"],
                    ["调整说明", adjustment_note],
                ],
            ),
        ]

        # 警示级别
        if abs(diff_pct) > 10:
            parts.append("\n⚠️ **重大偏离，建议重新评估首日策略**")
        elif abs(diff_pct) > 5:
            parts.append("\n🔔 **注意偏差，可适度调整目标价**")

        return "\n".join(parts)

    def render_post_listing_review(
        self,
        stock_code: str,
        name_cn: str,
        predicted_return: float,
        actual_return: float,
        deviation: float,
    ) -> str:
        """
        渲染上市后复盘（首日结果出来后追加）。

        用于对比预测与实际表现，评估模型精度，持续改进。

        Parameters
        ----------
        stock_code : str
            股票代码。
        name_cn : str
            股票名称。
        predicted_return : float
            预测收益率（小数）。
        actual_return : float
            实际收益率（小数）。
        deviation : float
            偏差（actual - predicted，小数）。

        Returns
        -------
        str
            飞书 Markdown 格式的复盘报告。
        """
        actual_pct = actual_return * 100
        pred_pct = predicted_return * 100
        dev_pct = deviation * 100

        # 结果 emoji
        if actual_return > 0.05:
            result_emoji = "✅ 成功"
        elif actual_return > 0:
            result_emoji = "⚠️ 微利"
        else:
            result_emoji = "❌ 破发"

        # 偏差评估
        if abs(deviation) < 0.05:
            deviation_text = "✅ 预测准确"
        elif deviation > 0:
            deviation_text = f"📈 实际优于预测 {dev_pct:+.2f}%"
        else:
            deviation_text = f"📉 实际弱于预测 {dev_pct:+.2f}%"

        parts = [
            "## 📋 上市首日复盘\n",
            f"**{name_cn}（{stock_code}）**\n",
            f"**{result_emoji}**\n",
            self._render_table(
                headers=["项目", "数值"],
                rows=[
                    ["预测收益率", f"{pred_pct:+.2f}%"],
                    ["实际收益率", f"**{actual_pct:+.2f}%**"],
                    ["偏差", deviation_text],
                ],
            ),
        ]

        # 偏差大于 15% 时给出提示
        if abs(deviation) > 0.15:
            parts.append(
                "\n⚠️ **偏差较大，建议复盘预测逻辑**"
            )

        return "\n".join(parts)

    # --------------------------------------------------------------------------
    # 子渲染方法
    # --------------------------------------------------------------------------

    def _render_header(self, report: IPOAnalysisReport) -> str:
        """渲染报告头部：标题 + 综合评级 + 置信度条。"""
        stats = report.statistics
        if stats:
            rating_text = self._overall_rating_text(stats)
            star_count = self._star_count(stats)
            conf = self._composite_confidence(report)
            conf_bar = self._confidence_bar(conf)
        else:
            rating_text = "暂无评级"
            star_count = "—"
            conf = 0.0
            conf_bar = self._confidence_bar(0)

        # 报告时间
        time_str = report.generated_at.strftime("%Y-%m-%d %H:%M")
        period_str = f"{report.period_start} ~ {report.period_end}"

        lines = [
            f"# 🚀 IPO 打新分析报告",
            f"**{rating_text}**  {star_count}  |  置信度 {conf_bar} **{conf*100:.0f}%**\n",
            f"📅 {period_str}  |  🕐 生成于 {time_str}",
        ]

        # 元数据行（如有）
        if report.metadata:
            meta_parts = [f"{k}: {v}" for k, v in report.metadata.items()]
            lines.append(" | ".join(meta_parts))

        return "\n".join(lines)

    def _render_core_conclusions(self, report: IPOAnalysisReport) -> str:
        """渲染核心结论：发行价区间、预测涨幅、胜率等。"""
        stats = report.statistics
        if not stats:
            return "### 📌 核心结论\n\n暂无统计数据。"

        lines = ["### 📌 核心结论\n"]

        # 首日收益统计
        avg_ret = stats.avg_first_day_return
        med_ret = stats.median_first_day_return
        std_ret = stats.std_first_day_return

        ret_emoji = self.UP_EMOJI if avg_ret >= 0 else self.DOWN_EMOJI

        lines.append(
            f"**首日收益率**  {ret_emoji}\n"
            f"- 平均 **{avg_ret*100:+.2f}%**  |  中位数 **{med_ret*100:+.2f}%**  |  标准差 **{std_ret*100:.2f}%**\n"
        )

        # 胜率 / 破发率
        win_rate = 1 - stats.loss_rate
        loss_count = stats.loss_count
        limit_up = stats.limit_up_count

        lines.append(
            f"**胜率** 🏆 **{win_rate*100:.1f}%**  |  "
            f"破发 {loss_count} 只  |  涨停 {limit_up} 只  |  "
            f"样本 {stats.total_ipo_count} 只\n"
        )

        # 中签率（中位数）
        if stats.median_lot_rate > 0:
            lines.append(
                f"**中签率中位数** 📊 **{stats.median_lot_rate*100:.4f}%**  "
                f"（越低越稀缺）\n"
            )

        # 募集资金
        if stats.total_proceeds > 0:
            proceeds_str = f"{stats.total_proceeds:,.0f}"
            lines.append(f"**募集总额** 💰 **{proceeds_str}** 万元\n")

        # 板块分布表格
        if report.board_breakdown:
            board_rows = []
            for b in report.board_breakdown:
                emoji = self.BOARD_EMOJI.get(b.board, str(b.board.value))
                board_rows.append([
                    emoji,
                    str(b.ipo_count),
                    f"{b.avg_first_day_return*100:+.2f}%",
                    f"{b.median_first_day_return*100:+.2f}%",
                    f"{b.loss_rate*100:.1f}%",
                ])
            lines.append(
                "\n" + self._render_table(
                    headers=["板块", "数量", "平均收益", "中位收益", "破发率"],
                    rows=board_rows,
                )
            )

        return "\n".join(lines)

    def _render_limit_order_section(
        self, report: IPOAnalysisReport
    ) -> str:
        """渲染三档限价单建议（保守/平衡/进取 × 暗盘/首日）。"""
        lines = ["### 📋 三档限价单建议\n"]

        # 如果 report.performance 存在，从 IPOPerformanceMetrics 构造
        if report.performance:
            # 取平均置信度最高的几只股票（简化：取 report.stocks 前3只演示）
            sample_stocks = report.stocks[:3] if len(report.stocks) >= 3 else report.stocks
            for stock in sample_stocks:
                perf = report.performance.get(stock.stock_code)
                if perf:
                    recs = self._build_limit_order_recs(stock, perf)
                    lines.append(self._render_single_stock_orders(stock, recs))
        else:
            # 无 performance 数据时，演示三档框架（基于发行价）
            if report.stocks:
                sample = report.stocks[0]
                recs = self._build_default_recs(sample)
                lines.append(self._render_single_stock_orders(sample, recs))
            else:
                lines.append("*暂无限价单建议（缺少数据）*")

        return "\n".join(lines)

    def _render_single_stock_orders(
        self, stock: IPOStockRecord, recs: List[IPOLimitOrderRec]
    ) -> str:
        """渲染单只股票的三档限价单。"""
        header = f"**{stock.stock_name}（{stock.stock_code}）**  |  发行价 **{stock.issue_price:.2f}** 元\n"
        rows = []
        for rec in recs:
            tier_icon = {
                OrderTier.CONSERVATIVE: "🟢",
                OrderTier.BALANCED: "🟡",
                OrderTier.AGGRESSIVE: "🟠",
            }[rec.tier]
            conf_bar = self._confidence_bar(rec.confidence)
            rows.append([
                f"{tier_icon} {rec.tier}",
                f"**{rec.entry_price:.2f}**",
                f"**{rec.target_price:.2f}**",
                f"**{rec.stop_loss:.2f}**",
                f"{rec.position_size*100:.1f}%",
                f"{rec.expected_return*100:+.2f}%",
                f"{conf_bar} {rec.confidence*100:.0f}%",
            ])

        table = self._render_table(
            headers=["档位", "买入价", "目标价", "止损价", "仓位", "预期收益", "置信度"],
            rows=rows,
        )
        return header + table

    def _render_key_rationale(self, report: IPOAnalysisReport) -> str:
        """渲染关键依据：可比 IPO / 机构持仓 / 条款 / 情绪。"""
        lines = ["### 🔍 关键依据\n"]

        # 1. 板块条款
        if report.board_breakdown:
            best_board = max(
                report.board_breakdown,
                key=lambda b: b.avg_first_day_return,
                default=None,
            )
            if best_board:
                emoji = self.BOARD_EMOJI.get(best_board.board, "")
                lines.append(
                    f"**📑 板块条款**  {emoji} {best_board.board.value}  "
                    f"平均收益 **{best_board.avg_first_day_return*100:+.2f}%**，"
                    f"破发率 **{best_board.loss_rate*100:.1f}%**\n"
                )

        # 2. 行业分布
        if report.industry_breakdown:
            top_industries = sorted(
                report.industry_breakdown,
                key=lambda i: i.avg_first_day_return,
                reverse=True,
            )[:3]
            industry_lines = []
            for ind in top_industries:
                industry_lines.append(
                    f"- {ind.industry}：{ind.ipo_count} 只，"
                    f"平均收益 **{ind.avg_first_day_return*100:+.2f}%**"
                )
            lines.append("**🏭 行业分布**\n" + "\n".join(industry_lines) + "\n")

        # 3. 可比 IPO（首日收益 top-5）
        if report.stocks:
            comparable = report.get_top_n(5, by="first_day_return")
            comp_rows = []
            for s in comparable:
                ret = s.listing_first_day_return
                emoji = self.UP_EMOJI if ret >= 0 else self.DOWN_EMOJI
                board_emoji = self.BOARD_EMOJI.get(s.board, "")
                comp_rows.append([
                    s.stock_name,
                    s.stock_code,
                    board_emoji,
                    f"{emoji} {ret*100:+.2f}%",
                    f"PE {s.issue_pe:.1f}" if s.issue_pe > 0 else "PE N/A",
                ])
            lines.append(
                "**📊 可比 IPO（历史首日收益 Top-5）**\n" +
                self._render_table(
                    headers=["名称", "代码", "板块", "首日收益", "发行PE"],
                    rows=comp_rows,
                )
            )

        # 4. 异常标注（如有）
        if report.notes:
            note_lines = []
            for n in report.notes:
                note_lines.append(f"- ⚠️ {n}")
            lines.append("\n**📝 注意事项**\n" + "\n".join(note_lines))

        return "\n".join(lines)

    def _render_risk_warnings(self, report: IPOAnalysisReport) -> str:
        """渲染风险提示。"""
        stats = report.statistics
        lines = ["### ⚠️ 风险提示\n"]

        warnings: List[str] = []

        if stats:
            # 破发风险
            if stats.loss_rate > 0.3:
                warnings.append(
                    f"🔴 **破发风险较高**：历史破发率 **{stats.loss_rate*100:.1f}%**，"
                    f"请谨慎参与。"
                )
            elif stats.loss_rate > 0.15:
                warnings.append(
                    f"🟡 **破发风险中等**：历史破发率 **{stats.loss_rate*100:.1f}%**。"
                )

            # 首日波动风险
            if stats.std_first_day_return > 0.3:
                warnings.append(
                    f"🔴 **首日波动较大**：标准差 **{stats.std_first_day_return*100:.1f}%**，"
                    f"收益率离散程度高，请做好止损准备。"
                )
            elif stats.std_first_day_return > 0.15:
                warnings.append(
                    f"🟡 **首日波动中等**：标准差 **{stats.std_first_day_return*100:.1f}%**。"
                )

            # 极端收益风险
            if report.stocks:
                max_ret = max(s.listing_first_day_return for s in report.stocks)
                min_ret = min(s.listing_first_day_return for s in report.stocks)
                if max_ret > 1.0:
                    warnings.append(
                        f"🟠 最高首日收益达 **{max_ret*100:.1f}%**，"
                        f"存在炒作风险。"
                    )
                if min_ret < -0.2:
                    warnings.append(
                        f"🔴 最低首日亏损达 **{min_ret*100:.1f}%**，注意止损。"
                    )

        # 无数据时
        if not warnings:
            warnings.append("✅ 暂无明显风险提示（数据不足，请结合市场环境判断）。")

        # 全局免责声明
        warnings.append(
            "\n*⚠️ 本报告仅供参考，不构成投资建议。*"
        )

        lines.extend(warnings)
        return "\n".join(lines)

    def _render_data_quality(self, report: IPOAnalysisReport) -> str:
        """
        渲染数据可信度评分。

        根据报告元数据和数据完整度综合评估。
        """
        lines = ["### 📐 数据可信度评分\n"]

        # 统计样本量置信度
        stats = report.statistics
        if stats and stats.total_ipo_count > 0:
            # 样本量评分（>50 为高，>20 为中，<20 为低）
            if stats.total_ipo_count >= 50:
                sample_score = 1.0
                sample_text = "✅ 样本量充足"
            elif stats.total_ipo_count >= 20:
                sample_score = 0.7
                sample_text = "🟡 样本量中等"
            else:
                sample_score = 0.4
                sample_text = "🔴 样本量不足"

            # 中签率数据完整度
            lot_rate_coverage = 1.0
            if report.stocks:
                has_lot = sum(1 for s in report.stocks if s.lot_rate > 0)
                lot_rate_coverage = has_lot / len(report.stocks)

            # 综合评分
            quality_score = 0.6 * sample_score + 0.4 * lot_rate_coverage
            quality_bar = self._confidence_bar(quality_score)

            rows = [
                ["样本量", f"{stats.total_ipo_count} 只", sample_text],
                ["中签率覆盖率", f"{lot_rate_coverage*100:.0f}%",
                 "✅ 完整" if lot_rate_coverage > 0.8 else "🟡 部分缺失" if lot_rate_coverage > 0.5 else "🔴 数据缺失"],
                ["**综合可信度**", f"**{quality_score*100:.0f}%**", quality_bar],
            ]

            lines.append(self._render_table(
                headers=["维度", "数值", "评级"],
                rows=rows,
            ))
        else:
            lines.append("*暂无统计数据，可信度无法评估。*")

        # 免责声明
        lines.append(
            "\n*📌 数据来源：公开市场信息，实时性仅供参考。*"
        )

        return "\n".join(lines)

    def _render_table(self, headers: List[str], rows: List[List[str]]) -> str:
        """
        渲染 Markdown 表格（飞书兼容）。

        Parameters
        ----------
        headers : List[str]
            表头。
        rows : List[List[str]]
            行数据，每行应与 headers 等长。

        Returns
        -------
        str
            Markdown 表格字符串。
        """
        if not headers:
            return ""

        # 表头行
        header_line = "| " + " | ".join(headers) + " |"
        # 分隔行
        sep_line = "| " + " | ".join(["---"] * len(headers)) + " |"
        # 数据行
        data_lines = []
        for row in rows:
            if len(row) != len(headers):
                _log.warning(
                    "Row length (%d) != header length (%d), skipping row: %s",
                    len(row), len(headers), row
                )
                continue
            data_lines.append("| " + " | ".join(str(cell) for cell in row) + " |")

        return "\n".join([header_line, sep_line] + data_lines)

    def _render_comparable_ipos(self, comparable_ipos: List[IPOStockRecord]) -> str:
        """
        渲染可比 IPO 列表（用于详细报告中）。

        Parameters
        ----------
        comparable_ipos : List[IPOStockRecord]
            可比 IPO 列表。

        Returns
        -------
        str
            Markdown 格式的可比 IPO 列表。
        """
        if not comparable_ipos:
            return "*无可比 IPO 数据*"

        rows = []
        for s in comparable_ipos:
            ret = s.listing_first_day_return
            emoji = self.UP_EMOJI if ret >= 0 else self.DOWN_EMOJI
            board_emoji = self.BOARD_EMOJI.get(s.board, "")
            rows.append([
                s.stock_name,
                s.stock_code,
                board_emoji,
                f"{emoji} {ret*100:+.2f}%",
                f"发行价 **{s.issue_price:.2f}**",
                f"PE {s.issue_pe:.1f}" if s.issue_pe > 0 else "PE N/A",
                s.industry or "—",
            ])

        return self._render_table(
            headers=["名称", "代码", "板块", "首日收益", "发行价", "PE", "行业"],
            rows=rows,
        )

    def _render_signals(
        self, signals: List[Dict[str, Any]], signal_type: str = "positive"
    ) -> str:
        """
        渲染信号列表。

        Parameters
        ----------
        signals : List[Dict[str, Any]]
            信号列表，每个 dict 应包含 ``label`` 和 ``description``。
        signal_type : str
            "positive"（正向信号）或 "negative"（负向信号）。

        Returns
        -------
        str
            Markdown 格式的信号列表。
        """
        if not signals:
            return "*暂无信号*"

        emoji = "✅" if signal_type == "positive" else "⚠️"
        lines = []
        for sig in signals:
            label = sig.get("label", "未知信号")
            desc = sig.get("description", "")
            weight = sig.get("weight", "")
            weight_str = f"`{weight}` " if weight else ""
            lines.append(f"- {emoji} **{label}**：{weight_str}{desc}")

        return "\n".join(lines)

    def _confidence_bar(self, confidence: float) -> str:
        """
        生成分数条（用于可视化）。

        Parameters
        ----------
        confidence : float
            置信度，0~1。

        Returns
        -------
        str
            10格分数条，如 ``█████░░░░░``。
        """
        filled = max(0, min(10, int(confidence * 10)))
        return "█" * filled + "░" * (10 - filled)

    # --------------------------------------------------------------------------
    # 内部辅助方法
    # --------------------------------------------------------------------------

    def _overall_rating_text(self, stats: IPOStatistics) -> str:
        """根据统计生成综合评级文本。"""
        if stats.total_ipo_count == 0:
            return "暂无评级"

        composite = self._composite_score(stats)
        if composite >= 0.75:
            return self.RATING_EMOJI["BUY"]
        elif composite >= 0.45:
            return self.RATING_EMOJI["NEUTRAL"]
        else:
            return self.RATING_EMOJI["SKIP"]

    def _composite_score(self, stats: IPOStatistics) -> float:
        """
        计算综合评分（0~1）。

        规则：
          - 平均收益（权重 50%）：越高越好
          - 胜率（权重 30%）：越高越好
          - 破发率（权重 20%）：越低越好
        """
        if stats.total_ipo_count == 0:
            return 0.0

        # 收益评分（归一化到 0~1，假设合理区间 -20%~+100%）
        ret_score = (stats.avg_first_day_return - (-0.2)) / (1.0 - (-0.2))
        ret_score = max(0.0, min(1.0, ret_score))

        # 胜率评分
        win_score = 1.0 - stats.loss_rate

        # 破发率评分（越低越好）
        loss_score = 1.0 - stats.loss_rate

        return 0.5 * ret_score + 0.3 * win_score + 0.2 * loss_score

    def _star_count(self, stats: IPOStatistics) -> str:
        """根据统计生成星级文字。"""
        if stats.total_ipo_count == 0:
            return "—"

        composite = self._composite_score(stats)
        stars = int(composite * 5) + 1
        stars = max(1, min(5, stars))
        return self.STAR_EMOJI.get(stars, "⭐" * stars)

    def _composite_confidence(self, report: IPOAnalysisReport) -> float:
        """
        计算报告综合置信度。

        基于：样本量（权重 40%） + 数据完整度（权重 40%） + 元数据（权重 20%）。
        """
        stats = report.statistics
        if not stats:
            return 0.0

        # 样本量置信度
        n = stats.total_ipo_count
        sample_conf = min(1.0, n / 30.0)  # 30 只以上完全可信

        # 数据完整度（中签率 + 首日收益）
        complete = 0
        total = len(report.stocks) * 2
        if total > 0:
            complete = sum(
                1 for s in report.stocks
                if s.lot_rate > 0
            ) + sum(
                1 for s in report.stocks
                if s.listing_first_day_return != 0
            )
            completeness = complete / total
        else:
            completeness = 0.0

        # 元数据置信度
        meta_conf = 1.0 if report.metadata else 0.5

        return 0.4 * sample_conf + 0.4 * completeness + 0.2 * meta_conf

    def _build_limit_order_recs(
        self, stock: IPOStockRecord, perf: IPOPerformanceMetrics
    ) -> List[IPOLimitOrderRec]:
        """
        从 StockRecord + PerformanceMetrics 构造三档限价单建议。

        Parameters
        ----------
        stock : IPOStockRecord
            新股记录。
        perf : IPOPerformanceMetrics
            绩效指标。

        Returns
        -------
        List[IPOLimitOrderRec]
            三档限价单建议。
        """
        issue_price = stock.issue_price
        expected = perf.expected_return
        conf = perf.confidence

        # 保守档：买入价 = 发行价 × 0.95（折价5%）
        conservative_entry = issue_price * 0.95
        conservative_target = issue_price * (1 + expected * 0.8)
        conservative_stop = issue_price * 0.90

        # 平衡档：买入价 = 发行价
        balanced_entry = issue_price
        balanced_target = issue_price * (1 + expected)
        balanced_stop = issue_price * 0.92

        # 进取档：买入价 = 发行价 × 1.02（溢价2%，视市场情绪）
        aggressive_entry = issue_price * 1.02
        aggressive_target = issue_price * (1 + expected * 1.2)
        aggressive_stop = issue_price * 0.88

        return [
            IPOLimitOrderRec(
                tier=OrderTier.CONSERVATIVE,
                entry_price=conservative_entry,
                target_price=conservative_target,
                stop_loss=conservative_stop,
                position_size=perf.allocation_rate * 0.5 if perf.allocation_rate > 0 else 0.05,
                expected_return=expected * 0.8,
                confidence=conf,
                note="低吸，稳健首选",
            ),
            IPOLimitOrderRec(
                tier=OrderTier.BALANCED,
                entry_price=balanced_entry,
                target_price=balanced_target,
                stop_loss=balanced_stop,
                position_size=perf.allocation_rate if perf.allocation_rate > 0 else 0.10,
                expected_return=expected,
                confidence=conf,
                note="标准仓位，中性策略",
            ),
            IPOLimitOrderRec(
                tier=OrderTier.AGGRESSIVE,
                entry_price=aggressive_entry,
                target_price=aggressive_target,
                stop_loss=aggressive_stop,
                position_size=perf.allocation_rate * 1.5 if perf.allocation_rate > 0 else 0.15,
                expected_return=expected * 1.2,
                confidence=conf * 0.9,  # 进取档置信度略低
                note="高水位，追涨持有",
            ),
        ]

    def _build_default_recs(
        self, stock: IPOStockRecord
    ) -> List[IPOLimitOrderRec]:
        """无 perf 时，基于历史统计构造默认三档。"""
        issue_price = stock.issue_price
        # 使用中位数收益作为预期
        default_expected = stock.listing_first_day_return if stock.listing_first_day_return != 0 else 0.20

        return self._build_limit_order_recs(
            stock,
            IPOPerformanceMetrics(
                stock_code=stock.stock_code,
                allocation_rate=0.10,
                expected_return=default_expected,
                confidence=0.6,
            ),
        )
