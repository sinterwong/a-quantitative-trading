# 审计:`backend/services/` 模块职责盘点

> 评估日期:2026-05-15 · 分支:`refactor/architecture-cohesion`
> 重点关注:文件大小 + 是否越权(自做业务而非调 core)+ 网络绕过 Gateway

## 处置约定

- **KEEP_AS_IS**:职责清晰且单一,保持
- **SHRINK**:有越权,要瘦身(业务下沉到 `core/use_cases/`,本文件只做 wrapper)
- **SPLIT**:超大文件,按职责拆分
- **MERGE**:与其它服务功能重叠,合并
- **DEPRECATE**:已被替代,标记后删除

---

## 21 个文件清单

| 文件 | 行数 | core/ 依赖 | 网络绕过 | 处置 | 备注 |
|---|---|---|---|---|---|
| `__init__.py` | 1 | — | — | KEEP_AS_IS | |
| **`intraday_monitor.py`** | **1831** | 8 个 | ✓ akshare(stock_zh_index_spot_em) | **SPLIT** | P2-7 拆为 5 个 ≤400 行子模块 |
| **`signals.py`** | **999** | 2 个 | ✓ qt.gtimg.cn × 3 / web.ifzq.gtimg.cn | **SHRINK + 合规** | P2-3 业务下沉 + 网络改走 Gateway |
| **`single_stock_analysis.py`** | **865** | 13 个 | — | **SHRINK** | P2-2 业务下沉到 `core/use_cases/analyze_stock.py`,本文件保留 ≤30 行 wrapper |
| `portfolio.py` | 711 | 0 | ✓ qt.gtimg.cn(`q={qt_symbols}`) | SHRINK + 合规 | P4-2 网络改走 Gateway(批量行情 → `gw.quotes()`) |
| `report_sender.py` | 482 | 0 | ✓ qt.gtimg.cn × 3 | SHRINK + 合规 | 网络改走 Gateway |
| `sector_comparison.py` | 458 | 0 | ✓ qt.gtimg.cn | SHRINK + 合规 | 同上 |
| `fund_flow.py` | 453 | 0 | ✓ import akshare | SHRINK + 合规 | 改走 `gw.sectors()` |
| `performance.py` | 449 | 0 | ✓ web.ifzq.gtimg.cn(`fqkline/get`) | SHRINK + 合规 | 改走 `gw.kline()` |
| `broker.py` | 433 | 1(`core.brokers.fill_simulator`) | ✓ qt.gtimg.cn | SHRINK + 合规 | 模拟成交价 → `gw.quote()` |
| `northbound.py` | 362 | 0 | ✓ push2.eastmoney.com | SHRINK + 合规 | 改走 `gw.north_flow()` |
| `data_cache.py` | 358 | 0 | ✓ push2.eastmoney.com × 2 | **MERGE** | 与 `core/data_gateway/cache.py` 重叠,后续合并 |
| `base_fetcher.py` | 332 | 0 | — | KEEP_AS_IS | 抽象基类 |
| `fetcher_manager.py` | 266 | 0 | — | KEEP_AS_IS | 管理 `fetchers/` 实现 |
| `walkforward_persistence.py` | 192 | 0 | — | KEEP_AS_IS | WF 结果持久化 |
| `circuit_breaker.py` | 168 | — | — | KEEP_AS_IS(暂) | 被 `fetcher_manager.py` 内部使用,等 fetchers/ 合并到 data_gateway/ 时一并迁移 |
| `alert_history.py` | 165 | 0 | — | KEEP_AS_IS | 告警历史 |
| `watchlist.py` | 150 | 0 | — | KEEP_AS_IS | watchlist 持久化 |
| `strategy_loader.py` | 116 | 0 | — | KEEP_AS_IS | 加载根目录 `strategies/` |
| `fundamentals.py` | 99 | 1(`get_gateway`) | — | KEEP_AS_IS | 已合规 |
| `data_fetch_exceptions.py` | 36 | 0 | — | KEEP_AS_IS | 异常定义 |

### 子目录

| 子目录 | 文件数 | 处置 |
|---|---|---|
| `channels/` (discord/feishu/telegram) | 3 | KEEP_AS_IS,告警通道 |
| `fetchers/` (akshare/sina/tencent/tencent_hk) | 4 | **MERGE** — 与 `core/data_gateway/providers/` 高度重叠,后续合并 |
| `llm/` (service / factory / cache) | 5+ | KEEP_AS_IS,LLM 适配层独立 |
| `ipo_stars/` | 1 dir | KEEP_AS_IS,IPO 打新独立功能 |

---

## 处置统计

| 处置 | 数量 | 占比 |
|---|---|---|
| KEEP_AS_IS | 9 + 子目录 channels/llm/ipo_stars | ~50% |
| SHRINK | 8 (含 3 个超大文件) | ~40% |
| SPLIT | 1 (intraday_monitor) | ~5% |
| MERGE | 2 (data_cache → gateway,fetchers → providers) | ~10% |
| DEPRECATE | 0 (经验证 circuit_breaker 仍被 fetcher_manager 使用) | 0% |

---

## 关键架构债

### 1. 三个超大文件总计 4695 行(占 services/ 总量 ~52%)
- `intraday_monitor.py` 1831 行 → 必须 P2-7 拆分
- `signals.py` 999 行 → 必须 P2-3 业务下沉
- `single_stock_analysis.py` 865 行 → 必须 P2-2 业务下沉

### 2. 8 个文件直接网络绕过 Gateway

| 文件 | 接口 | 用途 | 修复路径 |
|---|---|---|---|
| `portfolio.py` | `qt.gtimg.cn` | 批量行情 | `gw.quotes(symbols)` |
| `signals.py` | `qt.gtimg.cn / web.ifzq.gtimg.cn` | 实时 / K线 | `gw.quote()` / `gw.kline()` |
| `report_sender.py` | `qt.gtimg.cn` × 3 | 报告中的实时价 | `gw.quote()` |
| `sector_comparison.py` | `qt.gtimg.cn` 批量 | 板块对比 | `gw.quotes()` |
| `broker.py` | `qt.gtimg.cn` | 模拟成交参考价 | `gw.quote()` |
| `fund_flow.py` | `import akshare` | 资金流 | `gw.sectors()` |
| `performance.py` | `web.ifzq.gtimg.cn` | 基准曲线 | `gw.kline()` |
| `northbound.py` | `push2.eastmoney.com` | 北向 | `gw.north_flow()` |
| `data_cache.py` | `push2.eastmoney.com` × 2 | KAMT 缓存 | 评估是否还需要(`gw.north_flow_history()` W2-1 已就绪) |

### 3. 两处重复实现

- `backend/services/circuit_breaker.py`(168 行)vs `core/circuit_breaker.py` — 被 fetcher_manager 内部使用,与 fetchers/ 子目录一起迁移
- `backend/services/data_cache.py`(358 行)vs `core/data_gateway/cache.py`
- `backend/services/fetchers/*`(4 文件)vs `core/data_gateway/providers/*`

---

## 第一批可立即处理项(本次 P1-4)

**结论:无 0 风险删除项**。

经 grep 验证:
- `backend/services/circuit_breaker.py` 被 `fetcher_manager.py` 通过相对导入
  (`from .circuit_breaker import CircuitBreaker`)使用,**不能立即删**。
  待 fetchers/ 子目录合并到 `core/data_gateway/providers/` 时一并迁移。

---

## 后续动作时序

1. **P1-4**:验证并删除 `backend/services/circuit_breaker.py`(如果确认无引用)
2. **P2-2/P2-3/P2-7**:三大超大文件业务下沉到 use case 层
3. **P4-2** 周期:8 个文件的网络绕过统一改走 Gateway
4. **后续**:`data_cache` / `fetchers` 子目录合并到 `core/data_gateway/`
