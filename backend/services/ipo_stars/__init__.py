"""
ipo_stars — 港股打新分析模块 (IPO Stars)
======================================
决策辅助工具，专注新股上市前 48 小时的定价博弈分析。

核心逻辑："筹码分布为主，情绪量化为辅，估值锚点参考，机制漏洞对冲"

用法：
    from services.ipo_stars import IPOStarsService

    svc = IPOStarsService()
    candidates = svc.get_candidates(status='upcoming')
    report = svc.analyze('09696')
"""

from .service import IPOStarsService

__all__ = ['IPOStarsService']
