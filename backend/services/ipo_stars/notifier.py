"""
notifier.py — IPO Stars 报告推送
================================
支持飞书（Feishu）和钉钉（DingTalk）webhook 推送。
飞书优先走 Bot API（app_id + app_secret），无 credentials 时 fallback 到 webhook URL。
报告模板对应 IPO-stars.md 第 4 节。
"""

import json
import ssl
import logging
import urllib.request
import os
from typing import Optional

from .models import AnalysisReport, PricingStrategy

logger = logging.getLogger('ipo_stars.notifier')

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_FEISHU_BOT_API = "https://open.feishu.cn/open-apis"
_FEISHU_BOT_OPEN_ID = os.getenv(
    # Sinter 在小秘(bot B)下的 open_id（来自 /second-bot/logs 的 sender 字段）
    "IPO_STARS_FEISHU_OPEN_ID", "ou_8064645777b8d84fbbd7cd54d0d5e3d2"
)


class IPONotifier:
    """推送 IPO 分析报告到飞书 / 钉钉。"""

    def __init__(
        self,
        webhook_url: str = "",
        webhook_type: str = "feishu",
        feishu_app_id: str = "",
        feishu_app_secret: str = "",
    ):
        self.webhook_url = webhook_url
        self.webhook_type = webhook_type  # 'feishu' | 'dingtalk'
        # Bot API 凭证（优先从直接参数注入，否则从环境变量读）
        self.feishu_app_id = feishu_app_id or os.getenv("FEISHU_APP_ID", "")
        self.feishu_app_secret = feishu_app_secret or os.getenv("FEISHU_APP_SECRET", "")

    def send_report(self, report: AnalysisReport) -> bool:
        """渲染并推送报告，成功返回 True。"""
        if self.webhook_type == 'dingtalk':
            if not self.webhook_url:
                logger.warning('Webhook URL not configured, skipping push')
                return False
            payload = self._render_dingtalk(report)
            return self._post_webhook(payload)

        # 飞书：优先 Bot API，无 credentials 再 fallback webhook
        payload = self._render_feishu(report)
        if self.feishu_app_id and self.feishu_app_secret:
            return self._send_via_bot(report, payload)
        if self.webhook_url:
            return self._post_webhook(payload)
        logger.warning('No Feishu credentials or webhook URL configured, skipping push')
        return False

    # ─── 飞书 Bot API ─────────────────────────────────────────

    def _get_tenant_access_token(self) -> Optional[str]:
        """用 app_id + app_secret 换取 tenant_access_token。"""
        try:
            data = json.dumps({
                "app_id": self.feishu_app_id,
                "app_secret": self.feishu_app_secret,
            }, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                f"{_FEISHU_BOT_API}/auth/v3/tenant_access_token/internal",
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                result = json.loads(resp.read())
                if result.get("code") == 0:
                    return result["tenant_access_token"]
                logger.error("Failed to get tenant_access_token: %s", result)
                return None
        except Exception as e:
            logger.error("Exception getting tenant_access_token: %s", e)
            return None

    def _send_via_bot(self, report: AnalysisReport, card_payload: dict) -> bool:
        """通过 Bot API 发送交互卡片给指定用户。"""
        token = self._get_tenant_access_token()
        if not token:
            return False

        try:
            open_id = _FEISHU_BOT_OPEN_ID
            card_content = json.dumps(card_payload["card"], ensure_ascii=False)

            payload = {
                "receive_id": open_id,
                "msg_type": "interactive",
                "content": card_content,
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                f"{_FEISHU_BOT_API}/im/v1/messages?receive_id_type=open_id",
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                result = json.loads(resp.read())
                code = result.get("code")
                if code == 0:
                    logger.info("Report pushed via Bot API successfully")
                    return True
                # 10044013: 用户未启用机器人，消息投递到机器人自身的会话（仍算成功）
                if code == 10044013:
                    logger.info("Report pushed via Bot API (delivered to bot session)")
                    return True
                logger.error("Bot API returned code %s: %s", code, result)
                return False
        except Exception as e:
            logger.error("Exception sending via Bot API: %s", e)
            return False

    # ─── Webhook POST ─────────────────────────────────────────

    def _post_webhook(self, payload: dict) -> bool:
        """发送 webhook POST 请求（飞书/钉钉通用）。"""
        if not self.webhook_url:
            logger.warning("Webhook URL not configured")
            return False
        try:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                self.webhook_url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10, context=_SSL_CTX) as resp:
                status = resp.status
                if status == 200:
                    logger.info("Report pushed successfully")
                    return True
                logger.warning("Webhook returned status %d", status)
                return False
        except Exception as e:
            logger.error("Failed to push report: %s", e)
            return False

    # ─── 飞书卡片 ─────────────────────────────────────────────

    def _render_feishu(self, report: AnalysisReport) -> dict:
        """按飞书交互卡片（interactive）格式渲染。"""
        color_map = {'重点参与': 'green', '建议观察': 'orange', '放弃': 'red'}
        header_color = color_map.get(report.recommendation, 'blue')

        elements = []

        # ── 综合评估 ──
        elements.append(self._feishu_section(
            f"**综合评估：{report.recommendation}**\n"
            f"综合得分: **{report.final_score:.2f}**  |  "
            f"预测热度: {report.heat_level}  |  "
            f"控盘程度: {report.control_level}"
        ))
        elements.append({"tag": "hr"})

        # ── 评分明细 ──
        breakdown_text = ""
        for sr in report.scoring_breakdown:
            dim_label = {
                'market_sentiment': '市场情绪',
                'chips_structure': '筹码结构',
                'narrative': '主题/稀缺性',
                'valuation': '基本面/估值',
            }.get(sr.dimension, sr.dimension)
            bar = self._score_bar(sr.score)
            breakdown_text += (
                f"{dim_label}({sr.weight*100:.0f}%): "
                f"{bar} {sr.score:.2f}\n"
            )
        elements.append(self._feishu_section(
            f"**评分明细**\n{breakdown_text}"
        ))

        # ── 暗盘价预估 ──
        if report.dark_price_estimate:
            dk = report.dark_price_estimate
            dark_text = (
                f"预估区间: **${dk.low:.2f}** ~ **${dk.high:.2f}**"
                f"（中位 ${dk.mid:.2f}，溢价 {dk.premium_pct:+.1f}%）\n"
                f"置信度: {dk.confidence}\n"
            )
            for b in dk.basis:
                dark_text += f"- {b}\n"
            elements.append(self._feishu_section(
                f"**暗盘价预估**\n{dark_text}"
            ))

        # ── 挂单策略 ──
        if report.pricing_strategies:
            pricing_text = ""
            for ps in report.pricing_strategies:
                pricing_text += f"**{ps.label}**: ${ps.price:.2f} — {ps.reference}\n"
            sl = report.pricing_strategies[0].stop_loss
            pricing_text += f"止损参考: ${sl:.2f}\n"
            elements.append({"tag": "hr"})
            elements.append(self._feishu_section(
                f"**挂单策略（限价单）**\n{pricing_text}"
            ))

        # ── 关键因子 ──
        if report.key_factors:
            factor_text = '\n'.join(
                f"{i+1}. {f}" for i, f in enumerate(report.key_factors)
            )
            elements.append(self._feishu_section(
                f"**关键影响因子**\n{factor_text}"
            ))

        # ── 风险提示 ──
        if report.risk_alerts:
            risk_text = '\n'.join(f"- {r}" for r in report.risk_alerts)
            elements.append({"tag": "hr"})
            elements.append(self._feishu_section(
                f"**风险提示**\n{risk_text}"
            ))

        # ── 时间 ──
        elements.append({"tag": "hr"})
        elements.append(self._feishu_section(
            f"分析时间: {report.analyzed_at}"
        ))

        return {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"IPO Stars: {report.name} ({report.code})",
                    },
                    "template": header_color,
                },
                "elements": elements,
            },
        }

    @staticmethod
    def _feishu_section(text: str) -> dict:
        """构造飞书卡片的 Markdown section 元素。"""
        return {
            "tag": "div",
            "text": {"tag": "lark_md", "content": text},
        }

    @staticmethod
    def _score_bar(score: float, length: int = 10) -> str:
        """生成分数条形图。"""
        filled = round(score * length)
        return '█' * filled + '░' * (length - filled)

    # ─── 钉钉 Markdown ───────────────────────────────────────

    def _render_dingtalk(self, report: AnalysisReport) -> dict:
        """按钉钉 ActionCard 格式渲染。"""
        emoji_map = {'重点参与': '🟢', '建议观察': '🟡', '放弃': '🔴'}
        emoji = emoji_map.get(report.recommendation, '⚪')

        lines = [
            f"## {emoji} {report.name} ({report.code})",
            f"",
            f"### 综合评估：**{report.recommendation}**",
            f"- 综合得分: **{report.final_score:.2f}**",
            f"- 预测热度: {report.heat_level}",
            f"- 控盘程度: {report.control_level}",
            f"",
        ]

        if report.scoring_breakdown:
            lines.append("### 评分明细")
            for sr in report.scoring_breakdown:
                dim_label = {
                    'market_sentiment': '市场情绪',
                    'chips_structure': '筹码结构',
                    'narrative': '主题/稀缺性',
                    'valuation': '基本面/估值',
                }.get(sr.dimension, sr.dimension)
                bar = self._score_bar(sr.score, 8)
                lines.append(
                    f"- {dim_label}({sr.weight*100:.0f}%): "
                    f"{bar} {sr.score:.2f}"
                )
            lines.append("")

        if report.dark_price_estimate:
            dk = report.dark_price_estimate
            lines.append("### 暗盘价预估")
            lines.append(
                f"- 区间: **${dk.low:.2f}** ~ **${dk.high:.2f}**"
                f"（中位 ${dk.mid:.2f}，溢价 {dk.premium_pct:+.1f}%）"
            )
            lines.append(f"- 置信度: {dk.confidence}")
            lines.append("")

        if report.pricing_strategies:
            lines.append("### 挂单策略")
            for ps in report.pricing_strategies:
                lines.append(f"- **{ps.label}**: ${ps.price:.2f} ({ps.reference})")
            lines.append(f"- 止损: ${report.pricing_strategies[0].stop_loss:.2f}")
            lines.append("")

        if report.risk_alerts:
            lines.append("### 风险提示")
            for r in report.risk_alerts:
                lines.append(f"- {r}")

        lines.append(f"\n> 分析时间: {report.analyzed_at}")

        return {
            "msgtype": "actionCard",
            "actionCard": {
                "title": f"IPO Stars: {report.name} ({report.code})",
                "text": '\n'.join(lines),
                "btnOrientation": "1",
                "btns": [
                    {
                        "title": "查看详情",
                        "actionURL": f"https://www1.hkexnews.hk/search/titlesearch.xhtml?search={report.code}",
                    },
                ],
            },
        }
