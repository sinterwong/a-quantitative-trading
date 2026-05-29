# 重构计划：统一订单管线 (Unified Order Pipeline)

> 分支：`refactor/unified-order-pipeline`
> 目标：从根本上解决四路径并行下单、双线程冲突、信号与执行耦合的架构缺陷。
> 原则：**信号生成与订单执行完全分离，IntradayMonitor 是唯一流程编排者。**

---

## 问题根因

当前系统的架构缺陷不是某个 bug，而是**设计层面的职责混乱**：

```
现状：4 条独立路径可以向 Broker 提交订单

路径A: StrategyRunner._emit_signal() ──→ oms.submit_from_signal()  ← 无持仓检查/无冷却/无LLM
路径B: _check_and_push 持仓加仓循环   ──→ _submit_order_for_signal() ← 有冷却+LLM
路径C: _check_new_positions()        ──→ _submit_with_routing()     ← 有冷却+LLM
路径D: _run_exit_engine()            ──→ _submit_with_routing()     ← 有冷却

此外：StrategyRunner.run_loop() 和 IntradayMonitor._check_and_push() 同时运行
      → 同一标的每 2.5 分钟扫一次（两个线程各跑一次 run_once()）
```

### 实证（2026-05-29 日志）

| 现象 | 根因 |
|------|------|
| 600083.SH 30分钟内被买3次（54300+48900+32669股） | 路径A无防重复，每5分钟触发BUY |
| 600261.SH 被买5次（11200+10100+9100+7122+85股） | 同上 |
| 600060.SH 发14次SELL信号但无持仓 | 路径A不检查持仓状态 |
| StrategyRunner 10:39/10:50/10:50 三次重启 | 双线程冲突 |
| `_refresh_kelly_from_trades` 每5分钟报错 | pnl=None 未做 null 容错 |

---

## 目标架构

```
IntradayMonitor._check_and_push()  ← 唯一编排者
  │
  ├─ 1. StrategyRunner.run_once()      ← 纯计算：返回 scores + signals，不执行任何订单
  │     └─ 输出：RunResult[] (含 combined_score / dominant_signal)
  │
  ├─ 2. 收集所有候选信号
  │     ├─ StrategyRunner 的 BUY/SELL 信号（来自 pipeline scores）
  │     ├─ 持仓加仓信号（pipeline_score > BUY_THRESHOLD_ADD）
  │     ├─ 新仓建仓信号（pipeline_score > BUY_THRESHOLD_NEW）
  │     └─ ExitEngine 退出信号（止损/止盈/熔断）
  │
  ├─ 3. 统一过滤链（OrderGate）
  │     ├─ 持仓状态校验（BUY去重 / SELL空仓拦截）
  │     ├─ 冷却检查（统一 CooldownTracker）
  │     ├─ 风控 PreTrade 检查（RiskEngine）
  │     ├─ LLM 审核
  │     ├─ _can_trade() 模式检查
  │     └─ 分钟RSI确认
  │
  └─ 4. 统一执行
        └─ OrderGate.submit() → broker.submit_order()
```

---

## 开发任务

### Phase 1：StrategyRunner 信号/执行分离

**目标**：StrategyRunner 不再直接调用 OMS/Broker，只输出信号。

#### Task 1.1：StrategyRunner._emit_signal() 改为信号记录

**文件**：`core/strategy_runner.py`

**改动**：
- `_emit_signal()` 不再调 `self.oms.submit_from_signal()`，改为将信号存入 `self._pending_signals` 列表
- 新增 `pending_signals` 属性（线程安全），返回待处理信号列表
- 新增 `consume_signals()` 方法，读取并清空待处理信号（原子操作）

```python
# 新增属性
self._pending_signals: List[SignalRecord] = []
self._signals_lock = threading.Lock()

# _emit_signal() 改为：
def _emit_signal(self, symbol, pr, direction, price):
    candidates = [s for s in pr.signals if s.direction == direction]
    if not candidates:
        return
    top_signal = max(candidates, key=lambda s: s.strength)
    with self._signals_lock:
        self._pending_signals.append(SignalRecord(
            symbol=symbol, direction=direction, price=price,
            signal=top_signal, pipeline_result=pr,
            timestamp=datetime.now(),
        ))

# 新增
def consume_signals(self) -> List[SignalRecord]:
    with self._signals_lock:
        signals = list(self._pending_signals)
        self._pending_signals.clear()
        return signals
```

**验证**：
- `dry_run=True` 时只打日志，不存信号（保持现有行为）
- `dry_run=False` 时信号存入 `_pending_signals`，不调 OMS
- 单测：`run_once()` 后 `consume_signals()` 返回正确信号

#### Task 1.2：移除 StrategyRunner 对 OMS 的直接依赖

**文件**：`core/strategy_runner.py`

**改动**：
- `__init__` 中 `oms` 参数标记为 deprecated（保留兼容但不再使用）
- `_emit_signal()` 中删除 `elif self.oms is not None: self.oms.submit_from_signal(...)` 分支
- `_run_exit_engine()` 中删除直接调 OMS 的逻辑（ExitEngine 信号也走 `_pending_signals`）
- `_emit_rebalance_orders()` 同理

**验证**：
- 构造 `RunnerConfig(dry_run=False, oms=broker)` 后 `run_once()` 不产生任何 broker 交易
- 所有信号通过 `consume_signals()` 可读取

#### Task 1.3：禁用 StrategyRunner 独立 run_loop

**文件**：`quant_app/run_worker.py`

**改动**：
- `start_strategy_runner_thread()` 中不再启动 `runner.run_loop()` 线程
- 改为只创建 runner 实例 + 注入 monitor，不启动独立线程
- IntradayMonitor 通过 `runner.run_once()` 驱动信号生成

```python
# 改前：
target_fn = getattr(runner, 'run_sync', None) or runner.run_loop
runner_t = threading.Thread(target=target_fn, daemon=True, name='StrategyRunner')
runner_t.start()

# 改后：
# StrategyRunner 不再有独立线程，由 IntradayMonitor._check_and_push() 驱动
logger.info('StrategyRunner created (no independent loop, driven by IntradayMonitor)')
return None  # 无线程对象
```

**验证**：
- `ps aux | grep StrategyRunner` 无独立线程
- IntradayMonitor 每 5 分钟调 `runner.run_once()`，pipeline scores 正常更新
- 日志不再出现 `StrategyRunner started (interval=300s)` 的独立启动日志

---

### Phase 2：OrderGate 统一执行入口

**目标**：所有交易信号必须通过 OrderGate 提交，禁止绕过。

#### Task 2.1：新建 OrderGate 类

**文件**：`backend/services/order_gate.py`（新建）

**职责**：
```
OrderGate.submit(symbol, direction, price, shares, source, reason, metadata)
  │
  ├─ 1. 持仓状态校验
  │     BUY  → 查 DB，若已有持仓且 shares > 0 → 记录 skip，return rejected
  │     SELL → 查 DB，若无持仓或 shares == 0 → 记录 skip，return rejected
  │
  ├─ 2. 统一冷却检查
  │     CooldownTracker.can_fire(f'{direction}_{symbol}')
  │     → 冷却期内 → 记录 skip，return rejected
  │
  ├─ 3. 风控 PreTrade 检查
  │     RiskEngine.check(signal)
  │     → 拒绝 → 记录 skip，return rejected
  │
  ├─ 4. LLM 审核（BUY/SELL 均需）
  │     _llm_review_signal(ctx, direction)
  │     → 否决 → 记录 skip，return rejected
  │
  ├─ 5. _can_trade() 模式检查
  │     simulation → 推送飞书提示，return rejected (reason='simulation')
  │
  ├─ 6. 执行
  │     broker.submit_order(symbol, direction, shares, price, price_type='market')
  │
  └─ 7. 记录 + 推送
        记录信号日志 + 飞书推送
```

**关键设计**：
- `source` 参数标记信号来源（`'pipeline'` / `'exit_engine'` / `'add_position'` / `'new_position'` / `'rebalance'`），用于日志追踪
- `shares` 可选，不传时由 OrderGate 根据 Kelly + max_position_pct 计算
- 所有 rejected 原因记录到 `_skip_log`，可观测

#### Task 2.2：IntradayMonitor 集成 OrderGate

**文件**：`backend/services/intraday/signaling.py` + `execution.py` + `risk.py`

**改动**：
- `_check_and_push()` 流程重组（见目标架构图）
- 删除 `_submit_order_for_signal()` 中的重复检查逻辑（冷却/LLM/风控），统一由 OrderGate 处理
- `_check_new_positions()` 中删除重复检查，改为只构造信号交给 OrderGate
- `_run_exit_engine()` 中 ExitEngine 信号统一走 OrderGate
- 持仓加仓信号统一走 OrderGate

**验证**：
- 所有交易日志统一格式：`[OrderGate] {source} {direction} {symbol} {shares} @ {price} => {status}`
- 同标的在冷却期内无论从哪个来源触发，都只执行一次

#### Task 2.3：OrderGate 冷却机制统一

**文件**：`backend/services/order_gate.py`

**改动**：
- 不再按 `new_/exit_` 前缀分裂冷却 key
- 统一用 `{direction}_{symbol}` 作为冷却 key
- 同标的同方向在冷却期内，无论来源如何，都不重复执行

**验证**：
- 600083.SH BUY 后，5 分钟内再次触发 BUY → rejected (cooldown)
- 600060.SH SELL（无持仓）→ rejected (no_position)，不进入冷却

---

### Phase 3：IntradayMonitor 流程重组

**目标**：`_check_and_push()` 成为清晰的管道，每个阶段职责单一。

#### Task 3.1：_check_and_push() 主流程重写

**文件**：`backend/services/intraday/signaling.py`

**新流程**：

```python
def _check_and_push(self, now):
    # === 阶段 1：数据准备 ===
    self._run_daily_health_check()
    self._sync_market_regime()
    
    # === 阶段 2：信号生成 ===
    # 2a. StrategyRunner 生成 pipeline scores + 候选信号
    pipeline_scores = {}
    pending_signals = []
    if self._strategy_runner is not None:
        self._strategy_runner.run_once()
        pipeline_scores = self._strategy_runner.last_scores
        pending_signals = self._strategy_runner.consume_signals()
    
    # 2b. ExitEngine 生成退出信号
    positions = self._svc.get_positions()
    exit_signals = self._generate_exit_signals(positions, pipeline_scores) if positions else []
    
    # 2c. 持仓加仓信号
    add_signals = self._generate_add_signals(positions, pipeline_scores) if positions else []
    
    # 2d. 新仓建仓信号
    new_signals = self._generate_new_signals(pipeline_scores) if self._daily_refresh else []
    
    # === 阶段 3：统一过滤 + 执行 ===
    all_signals = pending_signals + exit_signals + add_signals + new_signals
    for sig in all_signals:
        result = self._order_gate.submit(
            symbol=sig.symbol,
            direction=sig.direction,
            price=sig.price,
            shares=sig.shares,
            source=sig.source,
            reason=sig.reason,
        )
        # 推送飞书 + 记录日志
```

**验证**：
- 日志中每轮 `_check_and_push` 只有一个入口调 broker
- 信号来源在日志中清晰可追踪

#### Task 3.2：持仓加仓逻辑整合

**文件**：`backend/services/intraday/signaling.py`

**改动**：
- 将 `_check_and_push()` 中第 273-337 行的持仓加仓循环提取为 `_generate_add_signals()`
- 不再直接调 `_submit_order_for_signal()`，改为返回信号列表

#### Task 3.3：新仓建仓逻辑整合

**文件**：`backend/services/intraday/signaling.py`

**改动**：
- 将 `_check_new_positions()` 中的信号生成与执行分离
- 信号生成阶段只收集候选，执行阶段走 OrderGate
- 分钟RSI确认保留在信号生成阶段（它是信号质量过滤，不是执行检查）

---

### Phase 4：防御加固 + Bug 修复

#### Task 4.1：修复 Kelly null 崩溃

**文件**：`backend/services/intraday/risk.py` 第 138 行

**改动**：
```python
# 改前：
trades = [{'pnl': float(t.get('pnl', 0))} for t in trades_raw]

# 改后：
trades = []
for t in trades_raw:
    pnl_val = t.get('pnl', 0)
    trades.append({'pnl': float(pnl_val) if pnl_val is not None else 0.0})
```

#### Task 4.2：StrategyRunner 持仓感知（Phase 1 的安全网）

**文件**：`core/strategy_runner.py`

**改动**：
- `_process_symbol()` 中增加持仓感知（作为 Phase 2 OrderGate 的双保险）
- BUY 信号：若 `_collect_positions()` 中已有该标的且 shares > 0 → `action='SKIPPED', reason='already_held'`
- SSELL 信号：若无持仓 → `action='SKIPPED', reason='no_position_to_sell'`
- 注意：这只是安全网，主要防线在 OrderGate

#### Task 4.3：日志格式统一

**改动**：
- 统一交易日志格式：`[OrderGate] {source} {direction} {symbol} {shares} @ {price} => {status}`
- 信号跳过日志：`[OrderGate] SKIP {symbol} {direction}: {reason}`
- 便于 grep 和后续分析

---

### Phase 5：清理 + 测试

#### Task 5.1：清理 StrategyRunner 中的 OMS/EventBus 残留

- `__init__` 中 `oms` / `event_bus` 参数 deprecated warning
- `_emit_signal()` 中删除 OMS/EventBus 分支
- `_run_exit_engine()` 中删除 OMS 分支
- `_emit_rebalance_orders()` 中删除 OMS 分支
- `_collect_positions()` 中删除 OMS 分支（只保留 RiskEngine.book）

#### Task 5.2：清理 use_exit_engine 配置项

- `RunnerConfig.use_exit_engine` 标记 deprecated
- ExitEngine 只在 IntradayMonitor 中调用，StrategyRunner 中的 ExitEngine 代码标记 deprecated

#### Task 5.3：集成测试

- 测试 1：StrategyRunner.run_once() 不产生任何 broker 交易
- 测试 2：OrderGate 对已持有标的的 BUY 请求返回 rejected
- 测试 3：OrderGate 对无持仓标的的 SELL 请求返回 rejected
- 测试 4：同标的在冷却期内的重复请求被拒绝
- 测试 5：simulation 模式下所有请求被拒绝（不写 DB）
- 测试 6：ExitEngine 信号正确通过 OrderGate 执行
- 测试 7：Kelly 计算在 pnl=None 时不崩溃

---

## 任务依赖关系

```
Phase 1 (StrategyRunner 信号/执行分离)
  Task 1.1 (emit_signal → 记录)  ←── 独立
  Task 1.2 (移除 OMS 依赖)       ←── 依赖 1.1
  Task 1.3 (禁用独立 run_loop)   ←── 依赖 1.2

Phase 2 (OrderGate)
  Task 2.1 (新建 OrderGate)       ←── 独立
  Task 2.2 (IntradayMonitor 集成) ←── 依赖 2.1 + 1.2
  Task 2.3 (冷却统一)             ←── 依赖 2.1

Phase 3 (流程重组)
  Task 3.1 (_check_and_push 重写) ←── 依赖 2.2
  Task 3.2 (加仓逻辑整合)         ←── 依赖 3.1
  Task 3.3 (新仓逻辑整合)         ←── 依赖 3.1

Phase 4 (防御加固)
  Task 4.1 (Kelly null fix)       ←── 独立
  Task 4.2 (持仓感知安全网)        ←── 依赖 1.2
  Task 4.3 (日志统一)              ←── 依赖 2.2

Phase 5 (清理 + 测试)
  Task 5.1-5.2 (清理)             ←── 依赖 3.x 全部完成
  Task 5.3 (集成测试)              ←── 依赖 5.1
```

---

## 并行开发策略

**可并行的 task**：
- Phase 1（Task 1.1）和 Phase 2（Task 2.1）和 Phase 4（Task 4.1）可以同时开始
- Phase 1 和 Phase 2 完成后，Phase 3 可以开始
- Phase 4 Task 4.2 可以在 Phase 1 Task 1.2 完成后开始

**建议执行顺序**（串行安全路径）：
```
4.1 → 1.1 → 1.2 → 2.1 → 2.2+2.3 → 1.3 → 3.1 → 3.2 → 3.3 → 4.2+4.3 → 5.1 → 5.2 → 5.3
```

---

## 风险控制

1. **每个 Task 完成后必须可独立验证**，不依赖后续 Task
2. **Phase 1 完成后系统仍可运行**（只是信号不再直接执行，IntradayMonitor 的路径 B/C/D 仍正常工作）
3. **Phase 2 完成后做一轮 live 验证**（在盘后用 test 端点模拟）
4. **全程保持向后兼容**：API 端点不变，前端无感知
