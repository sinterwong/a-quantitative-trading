"""下午3点收盘日报任务"""
import os, sys
from datetime import date, timedelta
THIS = os.path.abspath(__file__)
sys.path.insert(0, os.path.dirname(THIS))
from daily_reporter import DailyReporter
from daily_journal import JournalReader
from config_stock_pool import get_portfolio, get_news_focus, get_policy_calendar

TODAY = date.today().strftime('%Y-%m-%d')
YESTERDAY = (date.today() - timedelta(days=1)).strftime('%Y-%m-%d')

def main():
    check = TODAY
    reader = JournalReader()
    day = reader.get_day(check)
    meta = day.get('meta') or {}
    if not (meta and meta.get('trading_date')):
        check = YESTERDAY
        day = reader.get_day(check)
        meta = day.get('meta') or {}

    reporter = DailyReporter(check)
    if meta and meta.get('trading_date'):
        reporter.load_from_journal(reader)
    reporter.generate_report()

    pf = get_portfolio()
    for sym, info in (pf.get('stocks') or {}).items():
        news = get_news_focus(sym)
        pol = get_policy_calendar(sym)
        if news or pol:
            parts = []
            if news:
                parts.append("消息:" + news[0])
            if pol:
                parts.append("政策:" + pol[0])
            reporter.add_tomorrow_watch(sym + " (" + info.get('name', '') + "): " + " | ".join(parts))

    reporter.generate_report()
    text = reporter.get_text()
    print(text)

    try:
        from message import send_message
        r = send_message(action='send', channel='feishu', message=text)
        print("[FEISHU] sent:", r)
    except Exception as e:
        print("[FEISHU] failed:", e)

if __name__ == '__main__':
    main()
