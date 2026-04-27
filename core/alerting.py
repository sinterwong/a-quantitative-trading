"""
core/alerting.py — 企业微信 / 钉钉 / 邮件 告警推送系统

功能：
  - CRITICAL / WARNING / INFO 三级告警分类
  - 企业微信机器人 Webhook（最常用）
  - 钉钉机器人 Webhook（可选）
  - 邮件告警（SMTP，可选）
  - 每日收盘 P&L 汇总报告
  - 告警历史记录（本地 JSON，供 Web UI 查看）
  - 频率限制（同一告警 5 分钟内不重复）

接入方式：
  1. 企业微信：创建群机器人 → 复制 Webhook URL
     export WECHAT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
  2. 钉钉：创建群机器人（自定义类型）→ 复制 Webhook URL
     export DINGTALK_WEBHOOK_URL=https://oapi.dingtalk.com/robot/send?access_token=xxx
  3. 邮件：
     export ALERT_SMTP_HOST=smtp.163.com
     export ALERT_SMTP_USER=xxx@163.com
     export ALERT_SMTP_PASS=xxx
     export ALERT_EMAIL_TO=admin@company.com

集成点：
  - core/strategy_health.py → send_critical() 策略异常告警
  - core/risk_engine.py → send_critical() 日亏损熔断告警
  - 每日收盘后调用 send_daily_report()

用法：
    from core.alerting import AlertManager

    alert = AlertManager()
    alert.send_critical('策略 RSI-MACD 连续亏损超过 5%！')
    alert.send_warning('北向资金异常净流出 50 亿')
    alert.send_info('系统启动完成，监控中...')

    # 每日报告
    alert.send_daily_report({
        'date': '2024-01-15',
        'total_pnl': 1250.50,
        'pnl_pct': 0.023,
        'n_trades': 8,
        'positions': {'000001.SZ': {'pnl': 800, 'pct': 0.03}},
    })
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.parse
from dataclasses import asdict, dataclass, field
from datetime import datetime, date
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

logger = logging.getLogger(__name__)

# 告警历史存储目录
_ALERT_LOG_DIR = Path('data/alert_history')
_ALERT_LOG_DIR.mkdir(parents=True, exist_ok=True)

# 同一告警最小间隔（秒）
_RATE_LIMIT_SEC = 300  # 5 分钟

AlertLevel = Literal['CRITICAL', 'WARNING', 'INFO']


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class AlertRecord:
    """单条告警记录。"""
    level: str
    message: str
    timestamp: str
    channel: str          # 'wechat' / 'dingtalk' / 'email' / 'log_only'
    sent: bool
    error: str = ''

    def to_dict(self) -> Dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# 频率限制器
# ---------------------------------------------------------------------------

class _RateLimiter:
    """防止同一告警重复发送。"""

    def __init__(self, min_interval_sec: int = _RATE_LIMIT_SEC) -> None:
        self._last_sent: Dict[str, float] = {}
        self._interval = min_interval_sec

    def can_send(self, key: str) -> bool:
        last = self._last_sent.get(key, 0.0)
        return (time.time() - last) >= self._interval

    def mark_sent(self, key: str) -> None:
        self._last_sent[key] = time.time()


# ---------------------------------------------------------------------------
# 推送渠道
# ---------------------------------------------------------------------------

def _send_wechat(webhook_url: str, text: str, level: str) -> bool:
    """
    发送企业微信机器人消息（Markdown 格式）。

    消息格式：
      【CRITICAL/WARNING/INFO】
      内容...
      时间：2024-01-15 14:30:00
    """
    level_icon = {'CRITICAL': '🔴', 'WARNING': '🟡', 'INFO': '🟢'}.get(level, '⚪')
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    markdown = (
        f"**{level_icon} [{level}] 量化交易系统告警**\n\n"
        f"{text}\n\n"
        f"> 时间：{ts}"
    )

    payload = {
        'msgtype': 'markdown',
        'markdown': {'content': markdown},
    }
    return _http_post(webhook_url, payload)


def _send_dingtalk(webhook_url: str, text: str, level: str) -> bool:
    """发送钉钉机器人消息（Markdown 格式）。"""
    level_icon = {'CRITICAL': '❗', 'WARNING': '⚠️', 'INFO': 'ℹ️'}.get(level, '•')
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    payload = {
        'msgtype': 'markdown',
        'markdown': {
            'title': f'[{level}] 量化交易系统',
            'text': (
                f"### {level_icon} [{level}] 量化交易系统告警\n\n"
                f"{text}\n\n"
                f"**时间：** {ts}"
            ),
        },
    }
    return _http_post(webhook_url, payload)


def _send_email(
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    to_addr: str,
    subject: str,
    body: str,
) -> bool:
    """发送邮件告警（SMTP）。"""
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = username
        msg['To'] = to_addr

        with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
            server.login(username, password)
            server.sendmail(username, [to_addr], msg.as_string())
        return True
    except Exception as e:
        logger.error('[AlertManager] 邮件发送失败: %s', e)
        return False


def _http_post(url: str, payload: dict, timeout: int = 10) -> bool:
    """HTTP POST JSON 请求。"""
    try:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json; charset=utf-8'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode('utf-8'))
            # 企业微信：{"errcode": 0, "errmsg": "ok"}
            # 钉钉：{"errcode": 0, "errmsg": "ok"}
            errcode = result.get('errcode', result.get('ErrCode', 0))
            return int(errcode) == 0
    except Exception as e:
        logger.error('[AlertManager] HTTP POST 失败: %s', e)
        return False


# ---------------------------------------------------------------------------
# AlertManager
# ---------------------------------------------------------------------------

class AlertManager:
    """
    告警管理器（推荐使用全局单例）。

    配置优先级：构造参数 > 环境变量 > 不推送（仅记录日志）

    Parameters
    ----------
    wechat_webhook : str or None
        企业微信 Webhook URL（None 时从 WECHAT_WEBHOOK_URL 环境变量读取）
    dingtalk_webhook : str or None
        钉钉 Webhook URL（None 时从 DINGTALK_WEBHOOK_URL 环境变量读取）
    smtp_config : dict or None
        邮件配置 {'host', 'port', 'user', 'password', 'to'}
    min_level : str
        最低推送级别（'INFO' / 'WARNING' / 'CRITICAL'，默认 'WARNING'）
    rate_limit_sec : int
        同一告警频率限制（秒，默认 300）
    """

    _LEVELS = {'INFO': 0, 'WARNING': 1, 'CRITICAL': 2}

    def __init__(
        self,
        wechat_webhook: Optional[str] = None,
        dingtalk_webhook: Optional[str] = None,
        smtp_config: Optional[Dict] = None,
        min_level: str = 'WARNING',
        rate_limit_sec: int = _RATE_LIMIT_SEC,
    ) -> None:
        self.wechat_webhook = wechat_webhook or os.environ.get('WECHAT_WEBHOOK_URL', '')
        self.dingtalk_webhook = dingtalk_webhook or os.environ.get('DINGTALK_WEBHOOK_URL', '')
        self.smtp_config = smtp_config or self._load_smtp_from_env()
        self.min_level = min_level
        self._limiter = _RateLimiter(rate_limit_sec)
        self._history: List[AlertRecord] = []

    # ------------------------------------------------------------------
    # 主要接口
    # ------------------------------------------------------------------

    def send_critical(self, message: str, force: bool = False) -> bool:
        """
        发送 CRITICAL 告警（最高优先级，无论 min_level 设置均发送）。

        Parameters
        ----------
        message : str
            告警内容
        force : bool
            True = 忽略频率限制强制发送
        """
        return self._send('CRITICAL', message, force=force)

    def send_warning(self, message: str) -> bool:
        """发送 WARNING 告警。"""
        return self._send('WARNING', message)

    def send_info(self, message: str) -> bool:
        """发送 INFO 通知。"""
        return self._send('INFO', message)

    def send_daily_report(self, report: Dict[str, Any]) -> bool:
        """
        发送每日收盘 P&L 汇总报告。

        Parameters
        ----------
        report : dict，包含：
            date : str — 交易日期
            total_pnl : float — 当日总盈亏（元）
            pnl_pct : float — 当日盈亏率
            n_trades : int — 成交笔数
            positions : dict — {symbol: {'pnl': float, 'pct': float}}（可选）
            extra : dict — 其他指标（可选）

        Returns
        -------
        bool — 是否发送成功
        """
        report_date = report.get('date', date.today().isoformat())
        total_pnl = float(report.get('total_pnl', 0.0))
        pnl_pct = float(report.get('pnl_pct', 0.0))
        n_trades = int(report.get('n_trades', 0))

        pnl_sign = '+' if total_pnl >= 0 else ''
        pct_sign = '+' if pnl_pct >= 0 else ''
        status = '📈 盈利' if total_pnl >= 0 else '📉 亏损'

        lines = [
            f"**{report_date} 每日交易报告**",
            f"",
            f"**总体 P&L：** {pnl_sign}{total_pnl:.2f} 元 ({pct_sign}{pnl_pct*100:.2f}%) {status}",
            f"**成交笔数：** {n_trades}",
        ]

        # 持仓明细
        positions = report.get('positions', {})
        if positions:
            lines.append("")
            lines.append("**持仓明细：**")
            for symbol, info in list(positions.items())[:10]:  # 最多显示10个
                p = float(info.get('pnl', 0))
                pct = float(info.get('pct', 0))
                sign = '+' if p >= 0 else ''
                lines.append(f"  - {symbol}: {sign}{p:.2f} ({sign}{pct*100:.2f}%)")

        # 额外指标
        extra = report.get('extra', {})
        if extra:
            lines.append("")
            for k, v in extra.items():
                lines.append(f"**{k}：** {v}")

        message = '\n'.join(lines)

        # 日报强制发送（不受频率限制）
        return self._send('INFO', message, force=True, tag='daily_report')

    # ------------------------------------------------------------------
    # 告警历史
    # ------------------------------------------------------------------

    def get_history(
        self,
        level: Optional[str] = None,
        last_n: int = 50,
    ) -> List[AlertRecord]:
        """
        返回最近 N 条告警记录。

        Parameters
        ----------
        level : str or None
            过滤级别（None = 全部）
        last_n : int
            返回条数
        """
        records = self._history
        if level:
            records = [r for r in records if r.level == level]
        return records[-last_n:]

    def save_history(self) -> str:
        """将告警历史保存到本地 JSON。"""
        path = _ALERT_LOG_DIR / f'alerts_{date.today().isoformat()}.json'
        data = [r.to_dict() for r in self._history]
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return str(path)

    def load_history(self, date_str: Optional[str] = None) -> List[Dict]:
        """从本地 JSON 加载历史记录。"""
        date_str = date_str or date.today().isoformat()
        path = _ALERT_LOG_DIR / f'alerts_{date_str}.json'
        if not path.exists():
            return []
        try:
            with open(path, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []

    def clear_history(self) -> None:
        """清空内存告警历史。"""
        self._history.clear()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _send(
        self,
        level: str,
        message: str,
        force: bool = False,
        tag: str = '',
    ) -> bool:
        """核心发送逻辑。"""
        # 级别过滤
        if not force and self._LEVELS.get(level, 0) < self._LEVELS.get(self.min_level, 0):
            return False

        # 频率限制
        rate_key = f'{level}:{tag or message[:50]}'
        if not force and not self._limiter.can_send(rate_key):
            logger.debug('[AlertManager] 频率限制，跳过: %s', rate_key[:80])
            return False

        channel = 'log_only'
        sent = False
        error = ''

        # 推送到各渠道（按优先级）
        if self.wechat_webhook:
            try:
                sent = _send_wechat(self.wechat_webhook, message, level)
                channel = 'wechat'
            except Exception as e:
                error = str(e)

        if not sent and self.dingtalk_webhook:
            try:
                sent = _send_dingtalk(self.dingtalk_webhook, message, level)
                channel = 'dingtalk'
            except Exception as e:
                error = str(e)

        if not sent and self.smtp_config:
            try:
                subject = f'[{level}] 量化交易系统告警 {datetime.now().strftime("%H:%M")}'
                sent = _send_email(
                    smtp_host=self.smtp_config.get('host', 'smtp.163.com'),
                    smtp_port=int(self.smtp_config.get('port', 465)),
                    username=self.smtp_config.get('user', ''),
                    password=self.smtp_config.get('password', ''),
                    to_addr=self.smtp_config.get('to', ''),
                    subject=subject,
                    body=message,
                )
                channel = 'email'
            except Exception as e:
                error = str(e)

        # 始终记录日志
        log_fn = {'CRITICAL': logger.critical, 'WARNING': logger.warning}.get(level, logger.info)
        log_fn('[AlertManager][%s] %s', level, message[:200])

        # 记录历史
        record = AlertRecord(
            level=level,
            message=message[:500],
            timestamp=datetime.now().isoformat(timespec='seconds'),
            channel=channel,
            sent=sent,
            error=error,
        )
        self._history.append(record)

        # 无推送渠道时视为成功（log_only）
        log_only = not self.wechat_webhook and not self.dingtalk_webhook and not self.smtp_config
        effective_sent = sent or log_only

        # 频率限制：任何实际处理（发送成功或 log_only）都标记
        if effective_sent:
            self._limiter.mark_sent(rate_key)

        return effective_sent

    @staticmethod
    def _load_smtp_from_env() -> Optional[Dict]:
        """从环境变量加载 SMTP 配置。"""
        host = os.environ.get('ALERT_SMTP_HOST', '')
        user = os.environ.get('ALERT_SMTP_USER', '')
        password = os.environ.get('ALERT_SMTP_PASS', '')
        to = os.environ.get('ALERT_EMAIL_TO', '')

        if host and user and password and to:
            return {
                'host': host,
                'port': int(os.environ.get('ALERT_SMTP_PORT', '465')),
                'user': user,
                'password': password,
                'to': to,
            }
        return None


# ---------------------------------------------------------------------------
# 全局单例（可选）
# ---------------------------------------------------------------------------

_global_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    """获取全局 AlertManager 单例（懒初始化）。"""
    global _global_alert_manager
    if _global_alert_manager is None:
        _global_alert_manager = AlertManager()
    return _global_alert_manager


def reset_alert_manager(manager: Optional[AlertManager] = None) -> None:
    """重置全局单例（测试用）。"""
    global _global_alert_manager
    _global_alert_manager = manager
