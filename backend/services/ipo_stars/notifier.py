"""
notifier.py — IPO Stars 报告推送
================================
支持飞书（Feishu）和钉钉（DingTalk）webhook 推送。
报告模板对应 IPO-stars.md 第 4 节。
"""

import json
import ssl
import logging
import urllib.request
from typing import Optional

from .models import AnalysisReport, PricingStrategy

logger = logging.getLogger('ipo_stars.notifier')

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


class IPONotifier:
    """推送 IPO 分析报告到飞书 / 钉钉。"""

    def __init__(self, webhook_url: str, webhook_type: str = 'feishu'):
        self.webhook_url = webhook_url
        self.webhook_type = webhook_type  # 'feishu' | 'dingtalk'

    def send_report(self, report: AnalysisReport) -> bool:
        """渲染并推送报告，成功返回 True。"""
        if not self.webhook_url:
            logger.warning('Webhook URL not configured, skipping push')
            return False

        if self.webhook_type == 'dingtalk':
            payload = self._render_dingtalk(report)
        else:
            payload = self._render_feishu(report)

        return self._post(payload)

    # ─── 飞书卡片 ─────────────────────────────────────────────

    def _render_feishu(self, report: AnalysisReport) -> dict:
        """按飞书 Webhook 富文本格式渲染。"""
        # 评分明细
        breakdown_lines = []
        for sr in report.scoring_breakdown:
            dim_label = {
                'market_sentiment': '市场情绪',
                'chips_structure': '筹码结构',
                'narrative': '主题/稀缺性',
                'valuation': '基本面/估值',
            }.get(sr.dimension, sr.dimension)
            breakdown_lines.append(
                f"  {dim_label}({sr.weight*100:.0f}%): "
                f"{sr.score:.2f} → 加权 {sr.weighted_score:.4f}"
            )

        # 挂单策略
        pricing_lines = []
        for ps in report.pricing_strategies:
            pricing_lines.append(f"  【{ps.label}】${ps.price:.2f} — {ps.reference}")
        if report.pricing_strategies:
            sl = report.pricing_strategies[0].stop_loss
            pricing_lines.append(f"  止损参考: ${sl:.2f}")

        # 暗盘预估
        dark_lines = []
        if report.dark_price_estimate:
            dk = report.dark_price_estimate
            dark_lines.append(
                f"  预估区间: ${dk.low:.2f} ~ ${dk.high:.2f}（中位 ${dk.mid:.2f}，"
                f"溢价 {dk.premium_pct:+.1f}%）"
            )
            dark_lines.append(f"  置信度: {dk.confidence}")
            for b in dk.basis:
                dark_lines.append(f"    - {b}")

        # 风险提示
        risk_lines = [f"  - {r}" for r in report.risk_alerts] if report.risk_alerts else ['  暂无']

        # 关键因子
        factor_lines = [f"  {i+1}. {f}" for i, f in enumerate(report.key_factors)]

        text = (
            f"IPO Stars 深度报告：{report.name} ({report.code})\n"
            f"\n"
            f"一、综合评估：【{report.recommendation}】\n"
            f"  综合得分: {report.final_score:.2f}\n"
            f"  预测热度: {report.heat_level}\n"
            f"  控盘程度: {report.control_level}\n"
            f"\n"
            f"二、评分明细\n"
            + '\n'.join(breakdown_lines) + '\n'
            f"\n"
            f"三、暗盘价预估\n"
            + ('\n'.join(dark_lines) if dark_lines else '  暂无足够信息') + '\n'
            f"\n"
            f"四、挂单策略（建议限价单）\n"
            + '\n'.join(pricing_lines) + '\n'
            f"\n"
            f"五、关键影响因子\n"
            + '\n'.join(factor_lines) + '\n'
            f"\n"
            f"六、风险提示\n"
            + '\n'.join(risk_lines) + '\n'
            f"\n"
            f"分析时间: {report.analyzed_at}"
        )

        return {
            "msg_type": "text",
            "content": {"text": text},
        }

    # ─── 钉钉 Markdown ───────────────────────────────────────

    def _render_dingtalk(self, report: AnalysisReport) -> dict:
        """按钉钉 Markdown 格式渲染。"""
        lines = [
            f"# IPO Stars: {report.name} ({report.code})",
            f"",
            f"## 综合评估：**{report.recommendation}**",
            f"- 综合得分: **{report.final_score:.2f}**",
            f"- 预测热度: {report.heat_level}",
            f"- 控盘程度: {report.control_level}",
            f"",
            f"## 挂单策略",
        ]

        for ps in report.pricing_strategies:
            lines.append(f"- **{ps.label}**: ${ps.price:.2f} ({ps.reference})")

        if report.pricing_strategies:
            lines.append(f"- 止损: ${report.pricing_strategies[0].stop_loss:.2f}")

        lines.append("")
        lines.append("## 风险提示")
        for r in report.risk_alerts:
            lines.append(f"- {r}")

        lines.append(f"\n> 分析时间: {report.analyzed_at}")

        return {
            "msgtype": "markdown",
            "markdown": {
                "title": f"IPO Stars: {report.name}",
                "text": '\n'.join(lines),
            },
        }

    # ─── HTTP POST ────────────────────────────────────────────

    def _post(self, payload: dict) -> bool:
        """发送 webhook POST 请求。"""
        try:
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                status = resp.status
                if status == 200:
                    logger.info('Report pushed successfully')
                    return True
                logger.warning('Webhook returned status %d', status)
                return False
        except Exception as e:
            logger.error('Failed to push report: %s', e)
            return False
