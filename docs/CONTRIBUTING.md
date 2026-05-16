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

## 分支

| 分支 | 用途 |
|---|---|
| `master` | 稳定版本 |
| `feature/<name>` | 功能开发 |
| `fix/<name>` | bug 修复 |
| `refactor/<name>` | 重构 |

从 master 拉分支 → PR → review → 合 master。

## Commit message

```
<type>(<scope>): <what> + <why,如果非显然>

<可选详细说明>
```

type:`feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `ci`。

## 代码规范

- PEP 8
- 优先标准库 + 轻量依赖
- 新增函数加 docstring(`"""一句话用途"""` 即可)
- 类型注解非强制,推荐写

## 新增因子

1. `core/factors/` 下加 `xxx.py`,继承 `core/factors/base.Factor`
2. 实现 `evaluate(self, data) -> pd.Series`
3. 在 `core/factor_registry.py` 注册
4. `params.json`(或 `config/trading.yaml` 的 factors 段)加默认参数
5. `tests/` 加 unit test

## 新增策略

1. `core/strategies/` 下加 `xxx.py`,继承 `core/strategies/base.BaseStrategy`
2. 实现 `evaluate(self, data, i) -> dict`
3. (可选)在 `core/pipeline_factory.py` 注册
4. `tests/` 加 unit test

## 新增 API 端点

1. `backend/api.py` 加 `@app.route(...)` 装饰器,函数体只做参数解析 + 调 use case + `to_dict()`
2. use case 实现放 `core/use_cases/<xxx>.py`,定义 `Request` + `Response` dataclass
3. 重新生成 OpenAPI:`python scripts/generate_openapi.py`
4. `tests/test_api_smoke.py` 加 smoke,`tests/test_api_contract.py` 验证 schema

CI 跑 `--check` 守门,忘了 regen openapi.json 会红。

## PR

1. 跑全量 `pytest tests/ -q` 确认不回退
2. 涉及路由 → 重生成 OpenAPI
3. 涉及 schema/状态库 → 同步文档
4. 修 review 直接在分支上 push 新 commit,不接受只列问题不动手的 review
