# TODO — 模型层数据共振重构(2026-05-15)

> 评估基线 commit: `e5a0c37`(数据层 Gateway 统一出口已就绪)
> **状态:全部 21 项完成,1425 测试通过 · 全量无回退**
> 分支:`feat/model-layer-resonance-with-data` (待发 PR)

---

## Wave 0 — 合规闭环(P0,已完成 ✅)

> 目标:任何模型层网络请求均经 Gateway,享受熔断 + 健康度 + 多源融合保护。

### W0-1 暴露 `BALANCE_SHEET` 公开 API
- [x] `Provider` 抽象基类增加 `fetch_balance_sheet`,默认 None
- [x] `Gateway.balance_sheet(symbol)` 公开 API,走 `_merged_fetch` 字段级合并
- [x] `data_gateway.__init__` 导出 `BalanceSheet`
- [x] `_DEFAULT_TTL[Capability.BALANCE_SHEET] = 86400.0`
- [x] 测试覆盖路由 / 缓存 / 无源 / 多源合并 (4 个)

### W0-2 Regime 走 Gateway
- [x] `_fetch_index_data()` 改用 `gw.kline('sh000001', ...)`
- [x] 兼容 provider 间列名差异(date / timestamp)
- [x] `RegimeInfo.source` 默认值改为 `'gateway'`
- [x] 同步 docstring 和注释,删除所有 akshare 引用
- [x] 测试覆盖 gateway 调用 / 空返回 / timestamp 列 (3 个)

### W0-3 `MARGIN_FLOW` capability + 融资融券因子接通
- [x] 新增 `Capability.MARGIN_FLOW`
- [x] `Provider.fetch_margin_flow(symbol, start, end)`
- [x] `AkshareProvider` 实现 + `_normalize_margin` 列名归一
- [x] `Gateway.margin_flow(symbol, start, end)` 4h TTL
- [x] `MarginDataStore._fetch()` 改走 gateway,删 `import akshare`
- [x] 测试覆盖路由 / 缓存 / 因子集成 (15 个)

### W0-4 `NEWS_HEADLINES` capability + 新闻因子接通
- [x] `Capability.NEWS_HEADLINES` + `Provider.fetch_news_headlines`
- [x] `AkshareProvider.fetch_news_headlines` 包装 `stock_news_em`
- [x] `Gateway.news_headlines(symbol, n)` 30min TTL
- [x] `nlp._fetch_news_eastmoney()` 改走 gateway
- [x] 测试覆盖 gateway 调用 + 异常降级 (2 个)

---

## Wave 1 — 基本面字段红利消费(P0,已完成 ✅)

### W1-1 AkshareProvider 扩列(A股 eps_yoy/asset_yoy/dividend_yield)
- [x] `_normalize_indicator_em` 输出 eps_yoy(EPSJBHBZC) / asset_yoy(TOTALASSETSGRRATE) / dividend_yield
- [x] 顺带修复季频→日频 reindex 丢值 bug(union-reindex-ffill)
- [x] 测试 5 个新用例

### W1-2 BaostockProvider 扩列(balance sheet 日频化)
- [x] `_fetch_balance_history`(4 年所有季度)+ `fetch_fundamentals_history`
- [x] `_normalize_balance_history`:输出 debt_to_equity/current_ratio/quick_ratio 日频时序
- [x] Gateway `fundamentals_history` 改造为多 provider 列级合并(Baostock + Akshare 字段互补)
- [x] 测试 3 个新用例

### W1-3 扩展 FundamentalDataManager + pipeline_factory 白名单
- [x] white_list 从 7 列扩到 13 列
- [x] docstring 更新各字段来源
- [x] 测试拦截 `_safe_add` 验证白名单生效

### W1-4 重构 EarningsSurpriseFactor 优先消费 eps_yoy
- [x] `evaluate()` 优先 eps_yoy / 100,fallback eps_ttm 自算
- [x] 测试 3 个新用例

### W1-5 新增 FinancialHealthFactor
- [x] 合成 `-z(debt_to_equity) + z(current_ratio) + z(ocf_to_profit)`
- [x] 注册到 FactorRegistry
- [x] 测试 5 个新用例

### W1-6 新增 DividendYieldFactor
- [x] 股息率历史百分位
- [x] 注册 + 4 个测试

### W1-7 新增 AssetGrowthFactor
- [x] 反向因子(Cooper/Gulen/Schill 2008)
- [x] 极端值 clip(-50, 500)
- [x] 注册 + 5 个测试

---

## Wave 2 — 情绪因子自动化(P1,已完成 ✅)

### W2-1 `NorthboundFlowFactor` 自动 fetch
- [x] Provider 增 `fetch_north_flow_history`,AkshareProvider 实装
- [x] Gateway `north_flow_history(days)` 4h TTL
- [x] 因子层 `_get_sentiment` 自动 fetch
- [x] 测试 3 个新用例

### W2-2 新增 `SouthboundFlowFactor`(港股)
- [x] AkshareProvider 同时拉 北向 + 南向(best-effort)
- [x] `_normalize_north_history` 抽象 col_name 参数
- [x] 注册到 FactorRegistry
- [x] 测试 6 个新用例

### W2-3 NewsSentimentFactor 入 pipeline
- [x] 条件:symbol 非空 + MINIMAX_API_KEY 存在
- [x] 权重 0.05(防 LLM 单点失败)
- [x] 测试 3 个新用例

---

## Wave 3 — 板块层(P1,已完成 ✅)

### W3-1 新增 `SectorFlowFactor`
- [x] 新建 `core/factors/sector.py`
- [x] `SectorFlowStore`:日频持久化 sectors() 快照
- [x] `SectorFlowFactor`:消费板块 net_flow z-score
- [x] 注册到 FactorRegistry
- [x] 测试 13 个

### W3-2 新增 `SectorBreadthFactor`
- [x] `SectorBreadthStore`:对每个板块取 sector_constituents,算涨家占比
- [x] `SectorBreadthFactor`:z-score(rolling_mean(breadth - 0.5))
- [x] 注册 + 6 个测试

### W3-3 重构 `SectorRotationStrategyV2`
- [x] 弃用硬编码 ETF 列表,universe 由 `gw.sectors()` 实时发现
- [x] 板块打分:combined(z(flow) + change_pct) / flow-only / perf-only
- [x] `latest_signal()` 输出 top 板块 + 成分股 buy 列表
- [x] V1 类向后兼容
- [x] 7 个测试

### W3-4 DynamicStockSelector 加 net_flow 维度
- [x] `calc_flow_momentum_score(bk_code, today_net_flow, window=5)` 通过 SectorFlowStore 取历史
- [x] 当日 z-score 折算 ±10 分 bonus
- [x] 与 sentiment_bonus 同量级,不挤占现有 perf/flow rank
- [x] 4 个测试

---

## Wave 4 — Regime 多维度化(P2,已完成 ✅)

### W4-1 Regime 引入 VIX 分位
- [x] `gw.kline('^VIX', days=300)` 拉 1 年历史
- [x] `_fetch_vix_percentile()` 计算百分位(0-100)
- [x] `RegimeInfo.vix_percentile` 字段
- [x] 逻辑:VIX 分位 >= 80 在非 BULL/BEAR 且 ATR 未到阈值时强制 VOLATILE
- [x] BULL/BEAR 信号优先级高于 VIX
- [x] 6 个测试

### W4-2(暂缓)新增 `OvernightOverseasFactor`
> 风险较大,等 W4-1 实盘验证后单独立项

---

## Wave 5 — Pipeline / ML 装配(P2,已完成 ✅)

### W5-1 FeatureStore 解锁外部数据因子
- [x] `external_data` 参数 + `_FACTOR_DATA_REQUIREMENTS` 映射
- [x] financial_data / sentiment_data / sector_flow_data / breadth_data 等键
- [x] 注入对应字段时自动从 `_SKIP_IN_DEFAULT` 解锁
- [x] `_extract_factor_features` 用注入数据构造因子
- [x] 4 个测试

### W5-2 pipeline 重平衡 + 数据质量感知降权
- [x] 权重:技术 0.55 / 基本面 0.30(8 因子)/ 宏观 0.05 / 情绪 0.05 / 保留 0.05
- [x] 基本面层新加 EarningsSurprise / FinancialHealth / DividendYield / AssetGrowth
- [x] `fundamental_quality_mult`:financial_data 缺失时基本面因子权重 ×0.5
- [x] 3 个测试

---

## 总览

**新增/扩展能力**:
- Capability: 3 个新增(BALANCE_SHEET, MARGIN_FLOW, NEWS_HEADLINES)
- Provider 方法: 5 个新增(fetch_balance_sheet, fetch_margin_flow, fetch_news_headlines,
  fetch_north_flow_history, BaostockProvider.fetch_fundamentals_history)
- Gateway 公开 API: 4 个新增(balance_sheet, margin_flow, news_headlines, north_flow_history)
- 因子: 5 个新增(FinancialHealth, DividendYield, AssetGrowth, SouthboundFlow,
  SectorFlow, SectorBreadth),总因子数从 18 升到 22
- Strategy: SectorRotationStrategyV2 数据驱动版

**测试**:
- 全量 1425 通过 + 35 subtests,无回退
- 新增/修改测试 ~100 个

**合规**:
- 因子层 / Regime 层 3 处绕过 Gateway 的网络调用全部消灭
- 任何 model 层网络请求都享受熔断 + 健康度 + 缓存保护

---

## 验证清单(每个 commit)

- [x] 关联单元测试通过(`pytest tests/test_xxx.py -x -q`)
- [x] 全量测试不回退(`pytest tests/ -q`,最终 1425 passed)
- [x] commit message 简洁说明"做了什么 / 为什么"

---

## 不在本次范围

- 外部信号生成器 `core/external_signal.py` 中的 akshare/yfinance 绕过(单独周期)
- backend/services/* 的腾讯/东财直连(API 服务层,非模型层)
- `core/level2.py` Level2 因子(数据层未就绪)
- ML 模型架构升级(XGBoost → LightGBM)
- Pairs Trading 策略
- OvernightOverseasFactor(W4-2 暂缓)
