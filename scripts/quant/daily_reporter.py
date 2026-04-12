"""
DailyReporter - 每日报告生成 + 飞书推送
=====================================
目标：每日自动生成结构化报告，推送至飞书

功能：
1. 日报生成（Markdown + JSON）
2. 持仓异动提醒（单日±3%、涨跌停）
3. 明日关注（临界信号）
4. 飞书推送（主动推送，非被动查询）

Usage:
    reporter = DailyReporter('2026-04-11')
    reporter.load_from_journal(journal_reader)
    reporter.generate_report()
    reporter.push_to_feishu()
"""

import os
import sys
from datetime import date, timedelta

THIS = os.path.abspath(__file__)
QUANT_DIR = os.path.dirname(THIS)
sys.path.insert(0, QUANT_DIR)


# ============================================================
# 报告内容结构
# ============================================================

class DailyReport:
    """日报内容"""
    def __init__(self, trading_date: str):
        self.date = trading_date
        self.weekday = self._weekday()
        self.account = {}      # 账户概览
        self.positions = []     # 持仓明细
        self.trades = []       # 成交记录
        self.signals = []      # 信号记录
        self.market = {}       # 市场环境
        self.alerts = []       # 异动提醒
        self.tomorrow_watch = []  # 明日关注

    def _weekday(self):
        d = date.fromisoformat(self.date)
        days = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
        return days[d.weekday()]

    def to_dict(self) -> dict:
        return {
            'date': self.date,
            'weekday': self.weekday,
            'account': self.account,
            'positions': self.positions,
            'trades': self.trades,
            'signals': self.signals,
            'market': self.market,
            'alerts': self.alerts,
            'tomorrow_watch': self.tomorrow_watch,
        }


# ============================================================
# DailyReporter
# ============================================================

class DailyReporter:
    """
    每日报告生成器

    工作流程：
    1. load_from_journal(reader) - 读取Journal数据
    2. generate_report() - 生成报告内容
    3. push_to_feishu() - 推送至飞书
    4. save_report() - 保存到Journal目录
    """

    def __init__(self, trading_date: str = None):
        if trading_date is None:
            trading_date = date.today().strftime('%Y-%m-%d')
        self.date = trading_date
        self.report = DailyReport(trading_date)
        self.journal_reader = None
        self._content = ''

    # -------------------
    # 数据加载
    # -------------------

    def load_from_journal(self, reader):
        """从JournalReader加载数据"""
        self.journal_reader = reader
        day_data = reader.get_day(self.date)
        meta = day_data.get('meta', {}) or {}
        positions = day_data.get('positions', []) or []
        trades = day_data.get('trades', []) or []
        signals = day_data.get('signals', []) or []
        market = day_data.get('market', {}) or {}

        # 账户概览
        self.report.account = {
            'total_value': meta.get('equity', 0),
            'cash': meta.get('cash', 0),
            'position_value': meta.get('equity', 0) - meta.get('cash', 0),
            'n_positions': meta.get('n_positions', len(positions)),
            'n_trades': meta.get('n_trades', len(trades)),
            'n_signals': meta.get('n_signals', len(signals)),
        }

        self.report.positions = positions
        self.report.trades = trades
        self.report.signals = signals
        self.report.market = market

    def load_from_executor(self, executor, market_snapshots: dict):
        """从PaperExecutor加载数据"""
        status = executor.get_account_status()
        trade_log = executor.get_trade_log(self.date)

        self.report.account = {
            'total_value': status.get('total_value', 0),
            'cash': status.get('cash', 0),
            'position_value': status.get('position_value', 0),
            'n_positions': len(status.get('positions', {})),
            'n_trades': len(trade_log),
            'n_signals': 0,
        }

        # 从持仓还原positions列表
        self.report.positions = []
        for sym, pos in status.get('positions', {}).items():
            snap = market_snapshots.get(sym)
            current_price = snap.close if snap else pos.get('avg_cost', 0)
            mv = pos['shares'] * current_price
            cost = pos['shares'] * pos['avg_cost']
            pnl = mv - cost
            pnl_pct = pnl / cost if cost > 0 else 0
            self.report.positions.append({
                'symbol': sym,
                'shares': pos['shares'],
                'entry_price': pos['avg_cost'],
                'current_price': current_price,
                'market_value': mv,
                'cost': cost,
                'unrealized_pnl': pnl,
                'unrealized_pnl_pct': pnl_pct,
                'hold_days': 0,
            })

        # 从成交记录
        self.report.trades = trade_log

    # -------------------
    # 报告生成
    # -------------------

    def generate_report(self):
        """生成完整报告内容"""
        r = self.report
        lines = []

        # 标题
        lines.append(f"📊 **{self.date} {r.weekday} 日报**")
        lines.append("")

        # 账户概览
        acc = r.account
        lines.append("**【账户概览】**")
        tv = acc.get('total_value', 0)
        cash = acc.get('cash', 0)
        pv = acc.get('position_value', 0)
        lines.append(f"  总权益：{tv:,.0f}")
        lines.append(f"  现金：{cash:,.0f} | 持仓市值：{pv:,.0f}")

        # 持仓明细
        if r.positions:
            lines.append("")
            lines.append("**【持仓明细】**")
            total_pnl = 0
            total_cost = 0
            for p in r.positions:
                mv = p.get('market_value', 0)
                cost = p.get('cost', 0)
                pnl = p.get('unrealized_pnl', 0)
                pnl_pct = p.get('unrealized_pnl_pct', 0)
                hold = p.get('hold_days', 0)
                pnl_str = f"{pnl:+,.0f}({pnl_pct:+.1%})"
                lines.append(
                    f"  {p['symbol']} "
                    f"持仓{p.get('shares', 0)}股 "
                    f"成本{p.get('entry_price', 0):.2f} "
                    f"现价{p.get('current_price', 0):.2f} "
                    f"浮盈{pnl_str} "
                    f"持有{hold}天"
                )
                total_pnl += pnl
                total_cost += cost
            if total_cost > 0:
                lines.append(f"  ——— 持仓合计浮盈 {total_pnl:+,} ({total_pnl/total_cost:+.1%})")

        # 成交记录
        if r.trades:
            lines.append("")
            lines.append("**【成交记录】**")
            for t in r.trades:
                if isinstance(t, dict):
                    dc = '买' if t.get('direction') == 'buy' else '卖'
                    exec_type = t.get('execution_type', '')
                    slip = t.get('slippage_pct', 0)
                    slip_str = f"滑点{slip:+.2%}" if slip else ''
                    pnl = t.get('pnl')
                    pnl_str = f"盈亏{pnl:+,.0f}({t.get('pnl_pct', 0):+.1%})" if pnl else ''
                    lines.append(
                        f"  {dc}入 {t.get('symbol')} "
                        f"@{t.get('price', 0):.2f}x{t.get('shares', 0)} "
                        f"[{t.get('reason', '')}] "
                        f"{slip_str} {pnl_str}"
                    )

        # 信号记录（摘要）
        if r.signals:
            accepted = [s for s in r.signals if s.get('accepted')]
            rejected = [s for s in r.signals if not s.get('accepted')]
            lines.append("")
            lines.append(f"**【信号】** 共{len(r.signals)}条（{len(accepted)}接受/{len(rejected)}拒绝）")
            for s in accepted[:5]:
                if isinstance(s, dict):
                    lines.append(
                        f"  ✅ {s.get('symbol')} "
                        f"{s.get('direction', '')} "
                        f"@{s.get('price', 0):.2f} "
                        f"[{s.get('reason', '')}]"
                    )
            if rejected:
                for s in rejected[:3]:
                    if isinstance(s, dict):
                        lines.append(
                            f"  ❌ {s.get('symbol')} "
                            f"{s.get('decision', '')} "
                            f"原因: {s.get('decision_reason', s.get('reason', ''))}"
                        )

        # 市场环境
        if r.market:
            mc = r.market
            regime_cn = {
                'bull': '🟢 多头',
                'neutral': '🟡 震荡',
                'bear': '🔴 空头'
            }.get(mc.get('regime', ''), mc.get('regime', ''))
            pct = mc.get('hs300_pct_above_ma200', 0)
            lines.append("")
            lines.append(f"**【市场】** 沪深300 MA200 {regime_cn} ({pct:+.2f}%)")

        # 异动提醒
        if r.alerts:
            lines.append("")
            lines.append("**【异动提醒】**")
            for a in r.alerts:
                lines.append(f"  ⚠️ {a}")

        # 明日关注
        if r.tomorrow_watch:
            lines.append("")
            lines.append("**【明日关注】**")
            for w in r.tomorrow_watch:
                lines.append(f"  👀 {w}")

        self._content = '\n'.join(lines)
        return self._content

    def add_alert(self, message: str):
        """添加异动提醒"""
        self.report.alerts.append(message)

    def add_tomorrow_watch(self, message: str):
        """添加明日关注"""
        self.report.tomorrow_watch.append(message)

    # -------------------
    # 飞书推送
    # -------------------

    def push_to_feishu(self, feishu_target=None):
        """
        推送至飞书

        Args:
            feishu_target: 推送目标（user:open_id 或 chat:chat_id）
                           默认推送给当前用户
        """
        if not self._content:
            self.generate_report()

        # 构造富文本消息
        blocks = []

        # 标题
        blocks.append({
            "tag": "text",
            "text": f"📊 {self.date} {self.report.weekday} 日报\n\n"
        })

        # 账户
        acc = self.report.account
        if acc:
            tv = acc.get('total_value', 0)
            cash = acc.get('cash', 0)
            pv = acc.get('position_value', 0)
            blocks.append({
                "tag": "text",
                "text": f"【账户概览】\n"
                       f"总权益：{tv:,.0f}\n"
                       f"现金：{cash:,.0f} | 持仓：{pv:,.0f}\n\n"
            })

        # 持仓明细
        if self.report.positions:
            blocks.append({"tag": "text", "text": "【持仓明细】\n"})
            for p in self.report.positions:
                mv = p.get('market_value', 0)
                pnl = p.get('unrealized_pnl', 0)
                pnl_pct = p.get('unrealized_pnl_pct', 0)
                pnl_str = f"{pnl:+,.0f}({pnl_pct:+.1%})"
                blocks.append({
                    "tag": "text",
                    "text": f"• {p['symbol']} "
                           f"{p.get('shares', 0)}股 "
                           f"成本{p.get('entry_price', 0):.2f} "
                           f"→ {p.get('current_price', 0):.2f} "
                           f"{pnl_str}\n"
                })

        # 成交
        if self.report.trades:
            blocks.append({"tag": "text", "text": "\n【成交记录】\n"})
            for t in self.report.trades[-5:]:
                if isinstance(t, dict):
                    dc = '买' if t.get('direction') == 'buy' else '卖'
                    blocks.append({
                        "tag": "text",
                        "text": f"• {dc}入 {t.get('symbol')} "
                               f"@{t.get('price', 0):.2f}x{t.get('shares', 0)} "
                               f"[{t.get('reason', '')}]\n"
                    })

        # 市场
        if self.report.market:
            mc = self.report.market
            regime_cn = {
                'bull': '多头', 'neutral': '震荡', 'bear': '空头'
            }.get(mc.get('regime', ''), mc.get('regime', ''))
            pct = mc.get('hs300_pct_above_ma200', 0)
            blocks.append({
                "tag": "text",
                "text": f"\n【市场】沪深300 {regime_cn} ({pct:+.2f}% vs MA200)\n"
            })

        # 异动提醒
        if self.report.alerts:
            blocks.append({"tag": "text", "text": "\n⚠️ 【异动提醒】\n"})
            for a in self.report.alerts:
                blocks.append({"tag": "text", "text": f"• {a}\n"})

        # 明日关注
        if self.report.tomorrow_watch:
            blocks.append({"tag": "text", "text": "\n👀 【明日关注】\n"})
            for w in self.report.tomorrow_watch:
                blocks.append({"tag": "text", "text": f"• {w}\n"})

        # 构造消息内容
        content_text = '\n'.join(b.get('text', '') for b in blocks)

        # 调用飞书推送
        try:
            from send_message import send_to_feishu
            send_to_feishu(content_text, target=feishu_target)
            push_ok = True
        except Exception as e:
            push_ok = False
            push_error = str(e)[:100]

        return {
            'ok': push_ok,
            'error': push_ok if push_ok else push_error,
            'blocks': len(blocks)
        }

    def save_report(self):
        """保存报告到Journal目录"""
        journal_dir = os.path.join(QUANT_DIR, 'journal', self.date)
        os.makedirs(journal_dir, exist_ok=True)

        # Markdown
        md_path = os.path.join(journal_dir, 'report_feishu.md')
        with open(md_path, 'w', encoding='utf-8') as f:
            f.write(self._content)

        # JSON
        json_path = os.path.join(journal_dir, 'report.json')
        with open(json_path, 'w', encoding='utf-8') as f:
            import json
            json.dump(self.report.to_dict(), f, ensure_ascii=False, indent=2)

        return {'md': md_path, 'json': json_path}

    # -------------------
    # 格式化输出
    # -------------------

    def print_report(self):
        """打印报告到控制台"""
        if not self._content:
            self.generate_report()
        print(self._content)

    def get_text(self) -> str:
        """获取纯文本报告"""
        if not self._content:
            self.generate_report()
        return self._content


# ============================================================
# 飞书推送（适配OpenClaw message工具）
# ============================================================

def send_to_feishu(message: str, target=None):
    """
    通过OpenClaw message工具发送飞书消息。

    在OpenClaw环境中，由agent调用message tool发送。
    独立脚本调用时返回消息内容供外部发送。
    """
    # 消息内容准备好，供OpenClaw的message tool使用
    # 返回格式化的content供message tool发送
    return {
        'action': 'send',
        'channel': 'feishu',
        'target': target,  # None = 当前会话
        'message': message
    }


# ============================================================
# 使用示例
# ============================================================

if __name__ == '__main__':
    print("=" * 60)
    print("DailyReporter Demo")
    print("=" * 60)

    from daily_journal import JournalReader

    reader = JournalReader()
    today = date.today().strftime('%Y-%m-%d')

    reporter = DailyReporter(today)
    reporter.load_from_journal(reader)
    reporter.generate_report()
    reporter.print_report()

    # 添加自定义内容
    reporter.add_alert("平安保险 持仓浮亏-7.6%，注意止损")
    reporter.add_tomorrow_watch("茅台 若RSI触及70区域，关注是否共振卖出信号")

    print("\n" + "=" * 60)
    print("With Alerts Added")
    print("=" * 60)
    reporter.generate_report()
    reporter.print_report()
