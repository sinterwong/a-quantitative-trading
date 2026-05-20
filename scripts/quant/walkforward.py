"""
scripts/quant/walkforward.py — Walk-Forward Analysis 薄封装

实际实现在 core/walkforward.py，本文件仅做向后兼容的再导出，
供历史脚本 import 不报错。新代码请直接 from core.walkforward import …。
"""

from core.walkforward import WalkForwardAnalyzer, SensitivityAnalyzer, WFAWindowResult  # noqa: F401

__all__ = ['WalkForwardAnalyzer', 'SensitivityAnalyzer', 'WFAWindowResult']
