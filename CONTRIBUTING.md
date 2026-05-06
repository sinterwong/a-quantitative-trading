# 贡献指南

感谢参与本项目。

---

## 开发环境

```bash
git clone https://github.com/sinterwong/a-quantitative-trading.git
cd a-quantitative-trading
pip install -r requirements.txt
```

运行测试：

```bash
python tests/run_tests.py
# 或
pytest tests/ -v
```

---

## 分支规范

| 分支 | 用途 |
|------|------|
| `master` | 稳定版本，始终可部署 |
| `feature/xxx` | 功能开发 |
| `fix/xxx` | Bug 修复 |

**协作流程：**

```
feature/xxx 分支 → Pull Request → review → 合并到 master
```

Sir 偏好全面 review 后直接在分支上修复再测，不接受只列问题不动手的 review。

---

## 代码规范

- Python 标准风格（PEP 8）
- 优先标准库 + 轻量依赖，重型依赖需说明
- 所有新增函数须有 docstring
- 建议添加类型注解（不强制）

---

## 新增因子

1. 在 `core/factors/` 创建 `xxx_factor.py`
2. 继承 `Factor` 基类
3. 实现 `evaluate()` 方法
4. 在 `core/factor_registry.py` 中注册
5. 在 `params.json` 的 `factors` 下添加默认参数
6. 添加测试到 `tests/`

---

## 新增策略

1. 在 `core/strategies/` 创建 `xxx_strategy.py`
2. 继承 `BaseStrategy`
3. 实现 `evaluate(self, data, i) -> dict`
4. 在 `core/pipeline_factory.py` 中注册（可选）
5. 添加测试

---

## Pull Request 规范

1. 从 `master` 创建分支：`git checkout -b feature/my-feature`
2. 完成开发，运行测试确保通过
3. 提交并写清 commit message（推荐格式：`feat:` / `fix:` / `docs:` / `refactor:`）
4. Push 并开 PR

---

## 提交信息格式

```
<type>: <简短描述>

<可选详细说明>
```

类型：`feat` / `fix` / `docs` / `refactor` / `test` / `chore`
