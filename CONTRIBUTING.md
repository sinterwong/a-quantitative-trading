# Contributing to A-Share Quantitative Trading System

Thank you for your interest in contributing!

## Development Setup

```bash
git clone https://github.com/sinterwong/a-quantitative-trading.git
cd a-quantitative-trading
pip install -r requirements.txt
```

## Running Tests

```bash
# No external dependencies required
python tests/run_tests.py

# Or with pytest
pip install pytest
python -m pytest tests/ -v
```

## Code Style

- Python standard style (PEP 8)
- **No heavy external dependencies** — use only stdlib + `requests`
- All new functions should have docstrings
- Type hints are appreciated but not required

## Adding a New Signal Source

1. Create `scripts/quant/strategies/strategy_yourname.py`
2. Inherit from `SignalSource` base class in `signal_generator.py`
3. Implement `evaluate(self, i) -> dict` returning:
   ```python
   {'signal': 'buy'|'sell'|'hold', 'strength': 0.0-1.0, 'reason': str}
   ```
4. Add tests in `tests/test_signal_generator.py`
5. Update `params.json` with default parameters

## Pull Request Process

1. Fork the repo and create a branch: `git checkout -b feature/my-feature`
2. Run `python tests/run_tests.py` — all tests must pass
3. Commit with a clear message: `git commit -m "feat: add X"`
4. Push and open a PR

## Reporting Issues

Please include:
- Python version (`python --version`)
- Steps to reproduce
- Expected vs actual behavior
- Log output if available

## Code of Conduct

Be respectful. This is an educational project.
