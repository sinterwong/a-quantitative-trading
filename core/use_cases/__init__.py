"""
core.use_cases — 业务用例层(Use Case Layer)

每个 use case 是一个"以用户故事命名"的业务流程函数,所有 caller(REST API /
Streamlit UI / Scheduler / CLI)统一通过 use case 访问业务,不再各自拼装。

设计约定
========

1. **输入/输出 dataclass**
   每个 use case 函数签名形如:

       def my_use_case(req: MyUseCaseRequest) -> MyUseCaseResponse: ...

   `MyUseCaseRequest / MyUseCaseResponse` 用 dataclass(可序列化为 dict)。

2. **业务逻辑只此一份**
   ❌ 不允许在 backend/api.py 端点内重新组装业务逻辑
   ❌ 不允许在 streamlit_app.py 内重新组装业务逻辑
   ✅ 端点 / UI / Scheduler 都调本层函数

3. **依赖方向**
   use_cases → core.domain (factors / regime / risk / portfolio / execution)
   use_cases → core.data_gateway (取数据)
   use_cases ❌ 不依赖 backend / streamlit

4. **异常处理**
   - 业务层失败抛 ``UseCaseError(message, code)`` (本模块定义)
   - HTTP 端点把 UseCaseError 映射为 4xx 响应
   - 网络/数据层失败由 use case 内部 try-except,失败时返回降级响应

5. **可测试性**
   每个 use case 至少 3 个测试:happy path / degraded data / error path
   测试在 ``tests/test_use_cases/`` 下,文件名 ``test_<use_case>.py``

Provided use cases (P2-2 ~ P2-6 逐步落地)
========================================

- ``analyze_stock``        — 单股深度分析(替代 backend.services.single_stock_analysis)
- ``intraday_signals``     — 盘中信号生成
- ``morning_workflow``     — 早盘自动化(选股 + watchlist + 早报)
- ``backtest``             — 回测
- ``compose_portfolio``    — 组合优化建议
"""

from __future__ import annotations


class UseCaseError(Exception):
    """业务用例层统一异常。

    Attributes
    ----------
    message : str
        人类可读的错误描述
    code : str
        机器可读的错误码(如 ``'INVALID_SYMBOL'`` / ``'DATA_UNAVAILABLE'``)
    """

    def __init__(self, message: str, code: str = "USE_CASE_ERROR") -> None:
        super().__init__(message)
        self.message = message
        self.code = code

    def to_dict(self) -> "dict[str, str]":
        return {"error": self.message, "code": self.code}


__all__ = ["UseCaseError"]
