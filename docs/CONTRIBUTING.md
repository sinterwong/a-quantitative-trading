# 贡献

## 环境

```bash
git clone https://github.com/sinterwong/a-quantitative-trading.git
cd a-quantitative-trading
conda create -n quant-trading python=3.11
conda activate quant-trading
pip install -r requirements.txt
pytest tests/ -q
```

可选启用 pre-commit：

```bash
pip install pre-commit
pre-commit install
```

本地 `git commit` 时自动跑 ruff + mypy（strict 模块），不通过则拒绝提交。

## 工具链

| 工具 | 用途 |
|---|---|
| ruff | lint，规则见 `pyproject.toml`（E722 / F / B 安全规则 / UP031 / UP032） |
| mypy | 类型检查，CI strict 列表见 `pyproject.toml [[tool.mypy.overrides]]` |
| pytest | 测试框架，`pytest-cov` 跑覆盖率 |
| freezegun | 时间敏感测试用 `@freeze_time` 替代 `time.sleep` |
| pre-commit | 本地 hook，ruff + mypy + 通用文件检查 |

CI 在 `.github/workflows/ci.yml`：Linux × {3.10, 3.11, 3.12} + Windows 3.11。每个 PR 跑全量 pytest + ruff + mypy + OpenAPI 同步检查 + 旧 data_source 回流检查。

## 分支

| 分支 | 用途 |
|---|---|
| `master` | 稳定版本 |
| `feature/<name>` | 功能开发 |
| `fix/<name>` | bug 修复 |
| `refactor/<name>` | 重构 |
| `chore/<name>` | 工程化 / 文档 |

从 master 拉分支 → PR → review → 合 master。

## Commit message

```
<type>(<scope>): <what> + <why,如果非显然>

<可选详细说明>

Co-Authored-By: ...
```

type：`feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `ci` / `style`。

scope 例：`api / data-gateway / brokers / oms / risk / use_cases / config / ci`。

## 代码规范

- PEP 8，line-length 100
- f-string 替代 `%` / `.format`（ruff `UP031` / `UP032` 强制）
- 禁止裸 `except:`（ruff `E722`）
- 新增函数加 docstring
- 类型注解：strict 模块必须，存量代码逐步整改

## 新增因子

1. `core/factors/` 下加 `xxx.py`，继承 `core/factors/base.Factor`
2. 实现 `evaluate(self, data) -> pd.Series` 和 `signals(self, fv, price) -> List[Signal]`
3. 在 `core/factor_registry.py` 注册
4. `config/trading.yaml` 的 `strategies.*.factors` 段加默认参数
5. `tests/` 加 unit test

## 新增策略

1. `core/strategies/` 下加 `xxx.py`，继承 `core/strategies/base.BaseStrategy`
2. 实现 `evaluate(self, data, i) -> dict`
3. （可选）在 `core/pipeline_factory.py` 注册
4. `tests/` 加 unit test

## 新增 use case

1. `core/use_cases/<xxx>.py` 定义 `XxxRequest` + `XxxResponse` dataclass + `xxx(req)` 函数
2. 业务失败抛 `UseCaseError(message, code)`
3. `tests/test_use_cases/test_xxx.py` 至少 3 个用例：happy / degraded data / error path
4. （新模块默认进入 mypy strict）在 `pyproject.toml [[tool.mypy.overrides]]` 加入

## 新增 API 端点

1. 在 `backend/api_routes/<resource>.py` 的 Blueprint 上加 `@<bp>.route(...)`
2. 端点函数体只做：参数解析 → 调 use case → 序列化响应
3. broker / risk_engine / idempotency_store 通过 `from backend import api_deps` 访问
4. 重新生成 OpenAPI：`python scripts/generate_openapi.py`
5. `tests/test_api_smoke.py` 加 smoke，`tests/test_api_contract.py` 验证 schema

CI 跑 `--check` 守门，忘了 regen `openapi.json` 会红。

## 新增 Provider（数据源）

1. `core/data_gateway/providers/<xxx>.py` 继承 `core/data_gateway/providers/base.Provider`
2. 在 `declare()` 中声明 capability + market 矩阵 + priority_hint
3. 在 `core/data_gateway/capabilities.py:ROUTING_POLICY` 检查路由策略
4. 在 `core/data_gateway/gateway.py:_build_default_gateway()` 注册
5. `tests/test_data_gateway/test_provider_<xxx>.py` 加单测

CI 阻断旧 data_source 模块（`core.quote_data_source` / `core.tencent_quote_source` 等）回流，新代码必须走 gateway。

## PR

1. 跑全量 `pytest tests/ -q` 确认不回退
2. `ruff check .` 0 警告
3. 涉及路由 → 重生成 OpenAPI
4. 涉及 schema / 状态库 → 同步文档
5. 修 review 直接在分支上 push 新 commit，不接受只列问题不动手的 review
