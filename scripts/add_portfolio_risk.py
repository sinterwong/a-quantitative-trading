"""
S3-T1: 添加组合熔断到 intraday_monitor.py
"""

import re

path = r'C:\Users\sinte\.openclaw\workspace\quant_repo\backend\services\intraday_monitor.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

# ── 1. 在 __init__ 末尾添加实例变量 ───────────────────────
# 找 __init__ 最后的属性赋值
init_end = content.find('# LLM 新闻情绪服务')
if init_end == -1:
    print('WARNING: could not find LLM news sentiment block')

# 在 self._sentiment_cache 之后添加
old_init_tail = "        self._sentiment_cache: dict = {}\n        self._sentiment_cache_date: str = ''"
new_init_tail = """        self._sentiment_cache: dict = {}
        self._sentiment_cache_date: str = ''
        # 组合熔断追踪
        self._peak_equity: float = 0.0
        self._risk_warn_fired: bool = False   # 8% 熔断已触发（当天不重复推送）
        self._risk_stop_fired: bool = False   # 12% 熔断已触发
        # 组合风控参数
        self._dd_warn: float = 0.08    # 8% 回撤警告
        self._dd_stop: float = 0.12    # 12% 回撤清仓"""

if old_init_tail in content:
    content = content.replace(old_init_tail, new_init_tail, 1)
    print('[OK] Added instance variables to __init__')
else:
    print('[WARN] Init tail not found, trying alternate')
    # Try without type hints
    idx = content.find('self._sentiment_cache_date')
    print('  sentiment_cache_date at idx:', idx)

# ── 2. 在 _check_and_push 方法末尾（循环开始处）插入组合风控调用 ──
# 在 "positions = self._svc.get_positions()" 之后return之前插入
# 找这个 pattern
pattern_get_pos = "        if not positions:\n            logger.debug('No positions, skipping signal check')\n            return\n\n        # 使用 WFA"
if pattern_get_pos in content:
    # 在 return 之后、# 使用 WFA 之前，插入组合风控检查
    # 注意：组合风控需要positions，所以插在这里
    content = content.replace(
        "        if not positions:\n            logger.debug('No positions, skipping signal check')\n            return\n\n        # 使用 WFA",
        "        if not positions:\n            logger.debug('No positions, skipping signal check')\n            self._peak_equity = self._svc.get_portfolio_summary().get('total_equity', 0) or self._peak_equity\n            self._risk_warn_fired = False\n            self._risk_stop_fired = False\n            return\n\n        # ── 组合熔断检查 ─────────────────────────────────\n        self._check_portfolio_risk(positions)\n\n        # 使用 WFA",
        1
    )
    print('[OK] Inserted portfolio risk check after positions')
else:
    print('[WARN] get_positions pattern not found')

# ── 3. 在 _check_take_profits 之前添加新方法 ──
new_method = '''
    def _check_portfolio_risk(self, positions: list):
        """
        组合层面回撤熔断。

        规则：
          回撤 > 8%  → 推送紧急警告（当天不重复）
          回撤 > 12% → 全量清仓 + 推送

        逻辑：
          1. 用当前 total_equity 与历史 peak_equity 对比
          2. 更新 peak（权益新高时重置）
          3. 只在 broker 模式下执行（不 broker 只推送）
        """
        try:
            summary = self._svc.get_portfolio_summary(refresh_prices_now=True)
        except Exception as e:
            logger.warning('get_portfolio_summary failed: %s', e)
            return

        current_equity = summary.get('total_equity', 0)
        if not current_equity or current_equity <= 0:
            return

        # 更新峰值
        if current_equity > self._peak_equity:
            self._peak_equity = current_equity
            self._risk_warn_fired = False   # 权益新高，重置警告状态
            self._risk_stop_fired = False
            logger.debug('Portfolio peak updated: %.2f', self._peak_equity)
            return  # 新高时不检查风控

        drawdown = (self._peak_equity - current_equity) / self._peak_equity
        drawdown_pct = drawdown * 100
        now_str = datetime.now().strftime('%H:%M')

        # ── 12% 熔断：全量清仓 ──────────────────────────────
        if drawdown >= self._dd_stop and not self._risk_stop_fired:
            self._risk_stop_fired = True
            logger.warning('Portfolio cascade stop triggered! DD=%.1f%% equity=%.2f peak=%.2f',
                          drawdown_pct, current_equity, self._peak_equity)

            msg = (
                f'[EMERGENCY] 组合回撤熔断！\n'
                f'  当前回撤: {drawdown_pct:.1f}% (阈值{self._dd_stop*100:.0f}%)\n'
                f'  当前权益: {current_equity:.2f}\n'
                f'  峰值权益: {self._peak_equity:.2f}\n'
                f'  时间: {now_str}\n'
                f'  触发: 全量清仓！'
            )
            self._deliver_alert(msg)

            if self._broker:
                for pos in positions:
                    sym = pos.get('symbol')
                    shares = pos.get('shares', 0)
                    if shares > 0:
                        self._submit_market_sell(sym, shares, reason='portfolio_cascade_stop')
            return

        # ── 8% 警告：收紧至 50% ─────────────────────────────
        if drawdown >= self._dd_warn and not self._risk_warn_fired:
            self._risk_warn_fired = True
            logger.warning('Portfolio drawdown warning! DD=%.1f%% equity=%.2f peak=%.2f',
                           drawdown_pct, current_equity, self._peak_equity)

            msg = (
                f'[WARNING] 组合回撤警告\n'
                f'  当前回撤: {drawdown_pct:.1f}% (阈值{self._dd_warn*100:.0f}%)\n'
                f'  当前权益: {current_equity:.2f}\n'
                f'  峰值权益: {self._peak_equity:.2f}\n'
                f'  时间: {now_str}\n'
                f'  建议: 立即收紧仓位至 50%'
            )
            self._deliver_alert(msg)

            if self._broker:
                for pos in positions:
                    sym = pos.get('symbol')
                    shares = pos.get('shares', 0)
                    if shares > 0:
                        half = shares // 2
                        if half > 0:
                            self._submit_market_sell(sym, half, reason='portfolio_risk_reduce')
            return

        if drawdown >= self._dd_warn:
            logger.debug('Portfolio DD=%.1f%% (warning already fired)', drawdown_pct)
        else:
            logger.debug('Portfolio DD=%.1f%% (safe)', drawdown_pct)


'''

# Find the line "    def _check_take_profits(self, positions, now: datetime):"
insert_point = content.find('    def _check_take_profits(self, positions, now: datetime):')
if insert_point != -1:
    content = content[:insert_point] + new_method + content[insert_point:]
    print('[OK] Added _check_portfolio_risk method')
else:
    print('[WARN] _check_take_profits not found')

# ── 4. Save ──────────────────────────────────────────
with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print('[DONE] intraday_monitor.py updated')
