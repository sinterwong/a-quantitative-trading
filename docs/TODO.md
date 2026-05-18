# 数据层重构路线图

> **Sprint 1 已完成**（commit 11b7e72 → ef10ed2）：G8 / G3 / G1 / G2 全部交付，
> `gw.profile(symbol)` 已可用。详见各章节末尾的「✅ 已完成」标记。
>
> **Sprint 2 已完成**（commit 883f1dd → 43d5075）：G4 / G5 全部交付。
> `ROUTING_POLICY` 元数据驱动 4 种 routing 策略；`gw.news_headlines` 现已
> 走多源去重 + 时间倒序（EM kuaixun + AkShare 财联社电报）。

---



> **核心目标**：让 `core/data_gateway/` 从「多源 failover 网关」升级为
> 「多源冗余聚合 + 信息包合一」的数据层。最终调用方只需要：
> ```python
> profile = get_gateway().profile("600519.SH")
> ```
> 就能拿到一份**信息量巨大、来源透明、字段级互补**的数据包，
> 无需关心数据源在哪、谁更可靠、谁宕机了。

本文档记录从 PR #22 合入后启动的数据层 Sprint 系列重构。

---

## Sprint 1：基础设施 + 信息包雏形

### G8 — 启用 ParquetDiskCache + TieredCache

**动机**：`cache.py:73-136` 已实现 `ParquetDiskCache`，但 `DataGateway` 只用 `MemoryCache`。
进程重启后内存缓存全失，所有冗余数据要重拉一次；多进程间也无法共享缓存。

**范围**：
- `cache.py` 增加 `TieredCache`：L1=MemoryCache（毫秒级）+ L2=ParquetDiskCache（重启不丢）
- `DataGateway.__init__` 默认注入 disk cache，路径取 `data/cache/data_gateway/`
- 选择性启用：仅 K 线 / fundamentals_history / fund_flow / margin_flow / north_flow_history / macro 落盘
  （Quote / 实时类不落盘，避免污染）
- 配置项 `TRADING_DATA_GATEWAY_CACHE_DIR` 可覆盖默认路径

**验收**：
- `tests/test_data_gateway/test_cache.py` 增加 TieredCache 单元测试
- DataGateway 集成测试：重启后 disk cache 命中、L1 失效后从 L2 回填
- 全套既有测试通过

✅ 已完成：commit 99e742c，+9 cache 单元测试 + 4 gateway 集成测试。

---

### G3 — 时序缓存改"全量+切片"

**动机**：现在缓存键含 `start/end/days/limit`：
```python
cache_key = f"fundamentals_history:{symbol}:{start}:{end}"
cache_key = f"kline:{symbol}:{interval}:{days}:{adjust}:{limit}"
```
每个切片占独立缓存槽，而 provider 实际拉的可能是同一份原始数据。这是冗余浪费。

**范围**：
- 改造 `fundamentals_history` / `kline` / `fund_flow` / `north_flow_history`
  / `margin_flow`(若未来接入时序源) 的缓存策略
- 缓存键只含**结构性参数**（symbol / interval / adjust），不含**时间窗口**
- 内部缓存"已知最长时序"DataFrame
- 在 gateway 出口处按用户参数做 `.loc[start:end]` 或 `.tail(n)` 切片
- 提供 `invalidate_history(symbol)` API 精确清除

**验收**：
- 同一 symbol 两次不同时间窗口请求，缓存命中第二次（无网络 IO）
- 切片正确：返回的 DataFrame 索引在用户请求的 [start, end] 区间内
- 不破坏现有 `MarginDataStore` / 因子层调用兼容性

✅ 已完成：commit 5954279，+7 切片复用 / 宽抓取 / 精确 invalidate 测试。

---

### G1 — K 线字段级合并（抽 _merged_history_fetch）

**动机**：`gateway.kline()` 用 `_sequential_fetch`，找到第一个非空源就返回，
完全放弃了多源对账与字段互补能力（腾讯 turnover_rate / amount 字段更全、
Baostock 复权更权威、yfinance 美股延迟更低）。

而 `fundamentals_history()` 已经实现了「按 score 降序、列级互补合并」的成熟模式
（gateway.py:618-671）——这套逻辑应该被抽出来给所有时序数据复用。

**范围**：
- 把 `fundamentals_history` 内联的列合并逻辑抽到 `DataGateway._merged_history_fetch(capability, fn_name, *args)`
- `kline` / `fund_flow` / `north_flow_history` 全部走它
- 同一日同一列多源时，按 `health × authority` 加权胜出
- 保留 `_sequential_fetch` 作为"明确不需要合并"的策略选项（如单只 SectorRanking）

**验收**：
- mock 多个 provider 给同 symbol 不同部分列的 K 线，验证合并后字段并集
- mock 多个 provider 同列不同值，验证按 health 选源
- 既有 `fundamentals_history` 测试不回归

✅ 已完成：commit 37167d2，+5 _merged_history_fetch 直接测试 + 2 kline
重构后语义验证测试。

---

### G2 — StockProfile 聚合视图 + gw.profile()

**动机**：当前调用方要写 8 行才能拼出"我对这只票知道什么"：
```python
quote = gw.quote(sym)
fund = gw.fundamentals(sym)
bs = gw.balance_sheet(sym)
margin = gw.margin_flow(sym, end=today)
fflow = gw.fund_flow(sym).tail(1)
sectors = gw.sectors()
news = gw.news_headlines(sym)
macro = {k: gw.macro(k).tail(1).iloc[0,0] for k in ('PMI','M2','CREDIT')}
```
这违背了「使用者无需关心数据源」的目标。

**范围**：
- `schemas.py` 新增 `StockProfile` dataclass：
  - 字段：quote / fundamentals / balance_sheet / margin / fund_flow_latest /
    sector_info / headlines / macro_snapshot
  - 元数据：`as_of` / `completeness`（0-1）/ `provenance`（每切片来源）
- 子快照 dataclass：`MarginSnapshot`、`FundFlowSnapshot`、`MacroSnapshot`、`SectorInfo`
- `DataGateway.profile(symbol)` 一次并发触发所有 capability 拉取，组装 StockProfile 返回
- 任意切片缺失不阻塞主流程（只影响 completeness）

**验收**：
- mock 全部 capability 返回，验证 StockProfile 字段填充正确
- mock 部分 capability 失败，验证 completeness < 1 且 provenance 记录正确
- `tests/test_data_gateway/test_gateway_profile.py` 新文件

✅ 已完成：commit TBD（本 commit），+11 集成测试。注意：profile() 使用
独立 ThreadPoolExecutor，避免与 self._executor 嵌套提交导致的死锁。

---

## Sprint 2：CapabilityPolicy 路由统一 + news 多源去重

### G4 — CapabilityPolicy 元数据声明 routing 策略

**动机**：G1 之后 gateway 有三个并列原语
（`_sequential_fetch` / `_merged_fetch` / `_merged_history_fetch`），但选用
哪个原语 + 是否 ffill + 哪些字段当作"标识列"，全部硬编码在
`gw.quote()` / `gw.kline()` 等公开方法里。新增数据类型时要手动复制
这套样板，且不同方法路由参数不一致很难一眼看清全貌。

**范围**：
- `capabilities.py` 新增 `RoutingStrategy` 枚举（failover / merge_fields /
  merge_frames / merge_lists）+ `CapabilityPolicy(strategy, skip_fields,
  ffill)` + `ROUTING_POLICY: {(Capability, fn_name): CapabilityPolicy}`
- `DataGateway._route(cap, market, fn_name, *args, **kwargs)` 统一分派器，
  根据 ROUTING_POLICY 查表后调对应底层原语；统一返回 `(value, prov_dict)`
- 所有 gw.* 公开方法把直接调用 `_sequential_fetch` / `_merged_fetch` /
  `_merged_history_fetch` 改为 `_route(...)`
- `MERGE_LISTS` 在 G4 里只占位（raise NotImplementedError），G5 实现

**验收**：
- 路由表覆盖完整：每个 (cap, fn) 都有 policy；未登记 → KeyError
- 改 policy 即改路由：monkeypatch policy.strategy 后底层原语切换
- FAILOVER 也写 `{"_provider": name}` 到 _last_provenance（弥补 G2 缺口）
- 数据网关全套 335 passed（324 旧 + 11 新），无回归

✅ 已完成：commit 883f1dd（policy 元数据）+ 811f710（_route 分派器）+
522d844（gw.* 全部切换）+ 9e91b50（11 个测试）。

---

### G5 — news_headlines 多源归一去重 + 时间排序

**动机**：当前 NEWS_HEADLINES 只有 EastMoney 一源，且其接口是"全市场快讯"
（symbol 参数被忽略）。引入第 2/3 源（如 Sina/Tencent 财经资讯）后，
需要在 `_merged_list_fetch` 里：

- 标题归一化（去前后空白、全/半角统一、去常见前后缀如「【快讯】」）
- 按归一标题 dedupe
- 若 item 含时间戳按时间倒序；否则按 provider health 顺序

**范围**：
- 评估第 2/3 源（commit 1fc2537 前调研）：
  - ✅ AkShare 财联社电报 `stock_info_global_cls` 可用（含发布日期/时间）
  - ✗ AkShare `stock_news_em` 因 PyArrow regex bug 不可用
  - 最终方案：EM kuaixun + AkShare 财联社电报 2 源
- 新增 `schemas.NewsItem(title, timestamp, source, content)`，
  `base.fetch_news_headlines` 返回类型升级到 `List[NewsItem]`
- EM 重写：解析 showtime 字段写入 timestamp + source="eastmoney"
- AkShareProvider 新增 NEWS_HEADLINES capability + fetch 财联社电报
- gateway 加 `_merged_list_fetch`：并发拉所有候选源 → `_news_dedupe_key`
  归一化标题（去 "【...】"/"[...]" ≤12 字前缀、全角空格转半角、多空白折叠、
  末尾"。"/"."去除）→ 按 score 高的源先入 seen_keys → 有 ts 的按 ts 倒序、
  缺 ts 的按 source health 紧随
- `gw.news_headlines()` 公开签名 `(symbol, n) -> List[str]` 保留，
  内部投影 NewsItem.title
- ROUTING_POLICY 把 NEWS_HEADLINES 切到 MERGE_LISTS

**验收**：
- _news_dedupe_key 参数化覆盖各归一化规则
- 跨源同事件不同写法保留 score 高的那条
- ts 倒序 + 缺 ts 排末尾
- prov_dict 按"实际入选"条数计（去重失败的源不出现）
- gw.news_headlines 出口仍 List[str]
- 既有 EM news 测试全部不破

✅ 已完成：commit 3c473b7（G5-1 NewsItem + EM）+ 1fc2537（G5-2 AkShare
财联社）+ 7969fe4（G5-3 _merged_list_fetch + policy 切换）+ 43d5075
（G5-4 20 个直接单测）。数据网关 360 用例通过。

### Sprint 3：数据质量与可观测性
- G6: 字段级矛盾检测（divergence_pct 超阈值告警）
- G7: 在 schema 上暴露 completeness / confidence / stale_seconds
- G11: provenance 累计 metrics，接入 prometheus

### Sprint 4：运维 / 工程化
- G9: 配置驱动 provider 启用/禁用（`config/trading.yaml`）
- G16: 录制回放 provider（实盘录制 fixture，CI 重放）

### Backlog（低优先）
- G10: MemoryCache LRU 改 OrderedDict 实现（O(1)）
- G12: DataFrame schema 契约校验
- G13: 表驱动 Capability 注册
- G14: 跨 capability 字段拼接（用 Tencent.fundamentals 替代当前 hack）
- G15: batch RPC 接口
- G17: `gw.diagnose()` 结构化诊断

---

## 执行原则

- **小步提交**：每个 G* 独立 commit，msg 标注 "feat(data-layer): G* ..."
- **测试先行**：每个改动配单元测试，全套测试不回归
- **向后兼容**：现有 API 签名不变，新能力以新方法暴露
- **文档同步**：完成后更新 `docs/ARCHITECTURE.md` 的 Data Gateway 章节
