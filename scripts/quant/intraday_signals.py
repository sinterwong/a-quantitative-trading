"""
盘中信号轮询脚本
==================
在交易时间段定时运行，检查持仓股票是否有RSI信号触发。

使用方法：
  python intraday_signals.py                      # 立即运行一次
  openclaw cron add "9:35 Asia/Shanghai"  -- python intraday_signals.py
  openclaw cron add "10:30 Asia/Shanghai" -- python intraday_signals.py
  openclaw cron add "13:05 Asia/Shanghai" -- python intraday_signals.py
  openclaw cron add "14:30 Asia/Shanghai" -- python intraday_signals.py

盘中RSI近似方法：
  - 不依赖分钟线数据
  - 用昨日RSI水平 + 当日价格动量 综合判断
  - 逻辑：RSI超买超卖阈值接近时 + 价格有明确方向 → 发送提醒
"""

import os, sys, urllib.request, json, ssl
from datetime import date, datetime

THIS = os.path.abspath(__file__)
QUANT_DIR = os.path.dirname(THIS)
sys.path.insert(0, QUANT_DIR)

from config_stock_pool import get_portfolio, get_strategy_config
from data_loader import DataLoader

# ============ 数据接口 ============

def _fetch_realtime(symbol):
    """获取腾讯实时行情（当前价、涨跌额、成交量）"""
    sym = symbol.lower().replace('.sh', 'sh').replace('.sz', 'sz')
    url = f'https://qt.gtimg.cn/q={sym}'
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=5, context=ctx) as resp:
            raw = resp.read().decode('gbk', errors='replace')
            fields = raw.split('~')
            if len(fields) < 50:
                return None
            # fields[3]=当前价, [4]=昨收, [5]=今开, [31]=涨跌额, [32]=涨跌幅%
            # [6]=成交量(手), [36]=成交额(元)
            return {
                'symbol': symbol,
                'price': float(fields[3]) if fields[3] else 0,
                'prev_close': float(fields[4]) if fields[4] else 0,
                'open': float(fields[5]) if fields[5] else 0,
                'chg': float(fields[31]) if fields[31] else 0,
                'pct': float(fields[32]) if fields[32] else 0,
                'volume': int(fields[6]) if fields[6] else 0,
                'turnover': float(fields[36]) if fields[36] else 0,
            }
    except Exception as e:
        print(f"[WARN] {symbol} realtime fetch failed: {e}")
        return None


def _compute_rsi(prices, period=14):
    """计算RSI"""
    if len(prices) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i-1]
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _get_intraday_rsi_estimate(symbol, rsi_period=14):
    """
    盘中RSI近似：
    用前N日收盘价RSI + 当日价格动量方向 来近似盘中RSI信号。
    不依赖分钟数据。
    """
    loader = DataLoader()
    end = date.today().strftime('%Y%m%d')
    start = (date.today().replace(year=date.today().year - 1)).strftime('%Y%m%d')
    klines = loader.get_kline(symbol, start, end)
    if not klines or len(klines) < rsi_period + 2:
        return None, None

    closes = [float(k['close']) for k in klines]
    yesterday_close = closes[-1]
    prev_rsi = _compute_rsi(closes, rsi_period)

    # 当日实时价
    snap = _fetch_realtime(symbol)
    if not snap or snap['price'] == 0:
        return prev_rsi, None

    # 当日价格动量方向（相对昨收）
    day_chg_pct = (snap['price'] - yesterday_close) / yesterday_close if yesterday_close else 0

    return prev_rsi, day_chg_pct


def check_signals():
    """检查所有持仓股票的盘中信号，返回需要提醒的列表"""
    portfolio = get_portfolio()
    alerts = []

    for sym, info in portfolio.get('stocks', {}).items():
        rsi_buy, rsi_sell = 35, 70  # 默认值
        strategy_cfg = get_strategy_config(sym)
        if strategy_cfg:
            for src_name, params, _ in strategy_cfg.get('sources', []):
                if 'RSI' in src_name and 'MACD' not in src_name:
                    rsi_buy = params.get('rsi_buy', 35)
                    rsi_sell = params.get('rsi_sell', 70)

        prev_rsi, day_chg = _get_intraday_rsi_estimate(sym)
        snap = _fetch_realtime(sym)

        if prev_rsi is None or snap is None:
            continue

        signal = None
        reason = ""

        # 超跌反弹机会：RSI < 买入阈值 且 当日价格下跌（可能继续跌，但RSI接近超卖）
        if prev_rsi <= rsi_buy and day_chg is not None:
            if day_chg < -0.01:  # 当日下跌超过1%
                signal = 'WATCH_BUY'
                reason = f"RSI={prev_rsi:.0f}<={rsi_buy}超卖区间，当日下跌{day_chg:.1%}，关注低吸机会"
            elif day_chg > 0.01:  # 当日上涨，RSI超卖但价格已开始反弹
                signal = 'RSI_BUY'
                reason = f"RSI={prev_rsi:.0f}<={rsi_buy}超卖，价格已反弹{day_chg:.1%}"

        # 超买警告：RSI > 卖出阈值 且 当日价格上涨
        elif prev_rsi >= rsi_sell and day_chg is not None:
            if day_chg > 0.01:
                signal = 'WATCH_SELL'
                reason = f"RSI={prev_rsi:.0f}>={rsi_sell}超买区间，当日上涨{day_chg:.1%}，关注止盈信号"

        # 价格大幅波动警示（不依赖RSI方向）
        if abs(day_chg) > 0.03 if day_chg else False:
            signal = 'VOLATILE'
            reason = f"价格当日波动{day_chg:.1%}，RSI={prev_rsi:.0f}"

        if signal:
            alerts.append({
                'symbol': sym,
                'name': info.get('name', sym),
                'signal': signal,
                'price': snap['price'],
                'pct': snap['pct'],
                'prev_rsi': prev_rsi,
                'day_chg': day_chg,
                'reason': reason,
            })

    return alerts


def build_message(alerts, check_time):
    """构建飞书消息"""
    if not alerts:
        return None

    lines = [f"**盘中信号提醒** ({check_time})"]
    for a in alerts:
        emoji = {'WATCH_BUY': '🔔', 'RSI_BUY': '✅', 'WATCH_SELL': '⚠️', 'VOLATILE': '💥'}.get(a['signal'], '📊')
        sign = '+' if a['pct'] > 0 else ''
        lines.append(
            f"{emoji} **{a['name']}**({a['symbol']}) "
            f"现价{a['price']:.2f} {sign}{a['pct']:.1%} | {a['reason']}"
        )
    return '\n'.join(lines)


def main():
    now = datetime.now().strftime('%H:%M')
    print(f"[{now}] Intraday signal check starting...")

    alerts = check_signals()

    if alerts:
        msg = build_message(alerts, now)
        print(msg)
        try:
            from message import send_message
            send_message(action='send', channel='feishu', message=msg)
            print("[FEISHU] Alert sent")
        except Exception as e:
            print(f"[FEISHU] Failed: {e}")
    else:
        print(f"[{now}] No signals triggered")

    return alerts


if __name__ == '__main__':
    main()
