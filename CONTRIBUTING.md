# Contributing to A-Share Quantitative Trading System

感谢贡献！

## 开发环境

```bash
git clone https://github.com/sinterwong/a-quantitative-trading.git
cd a-quantitative-trading
pip install -r requirements.txt
```

## 运行测试

```bash
# 纯标准库，无需 pytest
python tests/run_tests.py

# 或使用 pytest
pip install pytest
python -m pytest tests/ -v
```

CI 会在 Linux / Windows / macOS 上同时运行语法检查和测试。

## 代码规范

- Python 标准风格（PEP 8）
- **避免重型外部依赖** — 标准库 + flask + requests 优先
- 所有新增函数须有 docstring
- 建议添加类型注解（不做强制要求）

## 新增策略插件

1. 在 `strategies/` 目录创建 `xxx_strategy.py`
2. 继承 `BaseStrategy`（`strategies/base.py`）
3. 实现 `evaluate(self, data, i) -> dict`：

   ```python
   {
       'signal':   'buy' | 'sell' | 'hold' | 'watch_buy' | 'watch_sell',
       'strength':  0.0 ~ 1.0,
       'reason':    str,
       'meta':      dict (可选),
   }
   ```

4. 在 `strategies/__init__.py` 的注册表中注册（参考 `RSI` / `MACD` / `BollingerBand`）
5. 在 `params.json` 的 `strategies` 下添加默认参数
6. 添加测试到 `tests/`

示例：`strategies/rsi_strategy.py`

## 新增信号类型（盘中信号引擎）

在 `backend/services/signals.py` 中扩展 `SIGNAL_EMOJI` / `SIGNAL_LABEL` 字典，
并在 `evaluate_signal()` / `check_limit_status()` 中添加对应判断逻辑。

## Pull Request 流程

1. Fork 仓库并创建分支：`git checkout -b feature/my-feature`
2. 运行测试：`python tests/run_tests.py` — 必须全部通过
3. 提交并写清 commit message：`git commit -m "feat: add X"`
4. Push 并开 PR

## Issue 模板

报告时请包含：
- Python 版本（`python --version`）
- 复现步骤
- 预期 vs 实际行为
- 相关日志输出

## 行为准则

保持尊重。这是一个教育研究项目。
