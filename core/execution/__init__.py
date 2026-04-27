"""
core/execution — 算法订单执行框架

模块：
  algo_base        : AlgoOrder 抽象基类 + OrderSlice 子单数据结构
  vwap_executor    : VWAP（成交量加权）拆单执行器
  twap_executor    : TWAP（时间加权）均匀拆单执行器
  impact_estimator : Almgren-Chriss 简化版市场冲击估算
"""
