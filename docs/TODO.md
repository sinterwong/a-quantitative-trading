# TODO — 模型层数据共振重构(2026-05-15)

> 评估基线 commit: `e5a0c37`(数据层 Gateway 统一出口已就绪)
> 目标:消灭模型层 3 处绕过 Gateway 的网络调用,消费已就绪的新字段,扩展板块/外盘维度
> 验收:全部新增/改造功能配测试,`pytest tests/ -x -q` 不回退

---

## Wave 0 — 合规闭环(P0,必做)

> 目标:任何模型层网络请求均经 Gateway,享受熔断 + 健康度 + 多源融合保护。

### W0-1 暴露 `BALANCE_SHEET` 公开 API
- [ ] `core/data_gateway/providers/base.py`:`Provider` 抽象基类增加 `fetch_balance_sheet(symbol) -> Optional[BalanceSheet]` 方法,默认返回 `None`
- [ ] `core/data_gateway/gateway.py`:增加 `DataGateway.balance_sheet(symbol)` 公开 API,使用 `_merged_fetch` 路由
- [ ] `core/data_gateway/__init__.py`:导出 `BalanceSheet`
- [ ] `_DEFAULT_TTL` 增 `Capability.BALANCE_SHEET: 86400.0`
- [ ] 新增 `tests/test_data_gateway/test_balance_sheet.py`:验证 Baostock 路由 + 字段
- **验收**:`get_gateway().balance_sheet('sh600519')` 返回有效 `BalanceSheet`,提交一次

### W0-2 Regime 走 Gateway
- [ ] `core/regime.py:_fetch_index_data()`:删 `import akshare`,改用 `from core.data_gateway import get_gateway; get_gateway().kline('sh000001', interval='daily', days=320)`
- [ ] 处理列名差异(gateway 返回 timestamp / open / high / low / close / volume,akshare 列名为 date)
- [ ] `RegimeInfo.source` 字段:`"akshare"` → `"gateway"`(向后兼容旧值)
- [ ] `tests/test_regime.py` 用 gateway mock 替换 akshare mock
- **验收**:`get_regime()` 在 mock 下正常返回,无 akshare 直接导入,提交一次

### W0-3 新增 `Capability.MARGIN_FLOW` + 接通融资融券因子
- [ ] `core/data_gateway/capabilities.py`:增 `MARGIN_FLOW = "margin_flow"`
- [ ] `core/data_gateway/schemas.py`:增 `MarginFlow` dataclass(symbol / date / margin_balance / short_balance)
- [ ] `core/data_gateway/providers/base.py`:增 `fetch_margin_flow(symbol, start, end) -> pd.DataFrame`
- [ ] `core/data_gateway/providers/akshare.py`:实现 `fetch_margin_flow` 包装 `stock_margin_detail`,声明 capability
- [ ] `core/data_gateway/gateway.py`:增 `margin_flow(symbol, start, end)` 公开 API
- [ ] `core/factors/sentiment.py`:`MarginDataStore._fetch()` 改走 gateway,删 `import akshare`
- [ ] `tests/test_data_gateway/test_margin_flow.py`:验证 capability 路由
- **验收**:`MarginTradingFactor / ShortInterestFactor` 不再直连 akshare,提交一次

### W0-4 新增 `Capability.NEWS_HEADLINES` + 接通新闻因子
- [ ] `core/data_gateway/capabilities.py`:增 `NEWS_HEADLINES = "news_headlines"`
- [ ] `core/data_gateway/providers/base.py`:增 `fetch_news_headlines(symbol, n) -> List[str]`
- [ ] `core/data_gateway/providers/akshare.py`:实现 `fetch_news_headlines` 包装 `stock_news_em`
- [ ] `core/data_gateway/gateway.py`:增 `news_headlines(symbol, n=20)` 公开 API
- [ ] `core/factors/nlp.py:_fetch_news_eastmoney()` 改走 gateway,删 `import akshare`
- [ ] `tests/test_data_gateway/test_news_headlines.py`:验证 capability 路由
- **验收**:`NewsSentimentFactor` 不再直连 akshare,提交一次

---

## Wave 1 — 基本面字段红利消费(P0)

> 目标:把已就绪但因子未读的字段全部接通,新增 3 个学术验证因子。

### W1-1 AkshareProvider 扩列(A 股)
- [ ] `_fetch_a_share_fundamentals_history`:增加 EPSKCJB / TOTALASSETS / DIVIDENDYIELD 等列(若 AkShare 接口有提供)
- [ ] 字段输出:`eps_yoy`(从季报自算) / `asset_yoy`(从总资产自算) / `dividend_yield`(取 quote 字段)
- [ ] 文档更新 ARCHITECTURE.md 字段映射表
- **验收**:`get_gateway().fundamentals_history('sh600519')` 列包含新字段,提交一次

### W1-2 BaostockProvider 扩列(balance sheet 日频化)
- [ ] `fetch_fundamentals_history`:把 balance_data 接口的 `debt_to_equity / current_ratio / quick_ratio` 转日频前向填充
- [ ] 多季度合并按 `statDate` 排序避免乱序
- **验收**:`get_gateway().fundamentals_history('sh600519')` 列包含 balance 字段,提交一次

### W1-3 扩 `FundamentalDataManager` 白名单 + pipeline_factory 适配
- [ ] `core/pipeline_factory.py`:`available` 白名单从 7 列扩到 12 列
- [ ] 同步注释说明每列含义
- **验收**:`build_pipeline('sh600519')` 注入 financial_data 列数 >= 10,提交一次

### W1-4 重构 `EarningsSurpriseFactor`
- [ ] `core/factors/fundamental.py`:`evaluate` 中先读 `eps_yoy` 列,无则 fallback 自算
- [ ] 因子语义说明更新
- [ ] `tests/test_fundamental_factors.py` 新增 EPS YoY 直接消费用例
- **验收**:有 eps_yoy 字段时不再走自算路径,提交一次

### W1-5 新增 `FinancialHealthFactor`
- [ ] `core/factors/fundamental.py`:新因子,合成 `debt_to_equity * -1 + current_ratio + ocf_to_profit` 的 z-score
- [ ] `core/factor_registry.py:_auto_register()` 注册
- [ ] `tests/test_fundamental_factors.py` 新增因子测试
- **验收**:因子在 mock balance 数据下产出非零 z-score,提交一次

### W1-6 新增 `DividendYieldFactor`
- [ ] `core/factors/fundamental.py`:新因子,股息率历史百分位
- [ ] 注册到 registry
- [ ] 测试覆盖
- **验收**:同上,提交一次

### W1-7 新增 `AssetGrowthFactor`(反向)
- [ ] `core/factors/fundamental.py`:新因子,asset_yoy 取负(高资产扩张 = SELL)
- [ ] 注册 + 测试
- **验收**:同上,提交一次

---

## Wave 2 — 情绪因子自动化(P1)

### W2-1 `NorthboundFlowFactor` 自动 fetch
- [ ] `core/factors/sentiment.py`:增加 `_get_sentiment` 方法,默认调 `gw.north_flow()` 构建日频序列
- [ ] 历史数据从 `backend.services.data_cache.cached_kamt` 取(已有缓存层)
- [ ] 测试:无注入数据时自动 fetch 不崩溃
- **验收**:`NorthboundFlowFactor(symbol='sh600519').evaluate(df)` 自动产出非零,提交一次

### W2-2 新增 `SouthboundFlowFactor`(港股)
- [ ] `core/factors/sentiment.py`:港股 universe 专用,消费 `NorthFlow.net_south_yi`
- [ ] 注册 + 测试
- **验收**:同上,提交一次

### W2-3 `NewsSentimentFactor` 入 pipeline
- [ ] `core/pipeline_factory.py`:情绪层加 NewsSentiment(权重 0.05),依赖 MINIMAX_API_KEY 时启用
- [ ] 衰减保护:连续 5 次零值自动剔除
- **验收**:`pipeline.factor_names` 含 NewsSentiment,提交一次

---

## Wave 3 — 板块层(P1)

### W3-1 新增 `SectorFlowFactor`
- [ ] 新建 `core/factors/sector.py`(避免 sentiment.py 过载)
- [ ] 输入 symbol,查 `gw.sectors()` 查找所属板块的 `net_flow`
- [ ] 板块归属缓存(进程内,1h TTL)
- [ ] 注册 + 测试
- **验收**:测试覆盖板块查找 / 滚动 z-score,提交一次

### W3-2 新增 `SectorBreadthFactor`
- [ ] `core/factors/sector.py`:板块内涨家占比,通过 `gw.sector_constituents()` 取成分股 change_pct
- [ ] 注册 + 测试
- **验收**:同上,提交一次

### W3-3 重构 `SectorRotationStrategyV2`
- [ ] `core/strategies/sector_rotation.py`:替换硬编码 `DEFAULT_SECTOR_ETFS`,默认 `universe = gw.sectors()` 真实板块
- [ ] 保留原硬编码作为 fallback(无网络时)
- [ ] 测试:mock gw 后策略可生成 signals
- **验收**:WFA 测试通过,提交一次

### W3-4 `DynamicStockSelector` 加 net_flow 维度
- [ ] `scripts/dynamic_selector.py`:打分逻辑增加板块资金流维度
- [ ] 权重可配置(默认 0.15)
- **验收**:选股结果不显著恶化,提交一次

---

## Wave 4 — Regime 多维度化(P2)

### W4-1 Regime 引入 VIX 分位
- [ ] `core/regime.py`:`detect_regime()` 增加可选 `vix_snapshot`,通过 `gw.market_index('VIX')` 读取
- [ ] VIX 252 日分位 > 80% → 强制 VOLATILE
- [ ] `RegimeInfo` 增 `vix_percentile: float = 0.0`
- [ ] 测试:mock VIX 后 regime 判定如期
- **验收**:VIX 高位时进入 VOLATILE,提交一次

### W4-2(暂缓)新增 `OvernightOverseasFactor`
> 风险较大,等 W4-1 验证后单独立项

---

## Wave 5 — Pipeline / ML 装配(P2)

### W5-1 FeatureStore 解锁外部数据因子
- [ ] `core/ml/feature_store.py`:`_SKIP_IN_DEFAULT` 在因子构造参数有数据时自动剔除
- [ ] 测试覆盖
- **验收**:ML 训练特征数显著增加,提交一次

### W5-2 pipeline 重平衡 + 数据质量感知
- [ ] `core/pipeline_factory.py`:基本面层从 4 个扩到 7-8 个,权重重平衡
- [ ] 利用 `gw.provenance()` 检测主源健康度,不健康时自动降权
- **验收**:`pipeline.factor_names` 数量从 ~10 增至 ~14,提交一次

---

## 验证清单(每个 commit)

- [ ] 关联单元测试通过(`pytest tests/test_xxx.py -x -q`)
- [ ] 全量测试不回退(`pytest tests/ -x -q --timeout=60`)
- [ ] commit message 简洁说明"做了什么 / 为什么"
- [ ] 如涉及 schema/API 变更,同步更新 `docs/ARCHITECTURE.md`

---

## 不在本次范围

- 外部信号生成器 `core/external_signal.py` 中的 akshare/yfinance 绕过(单独周期)
- backend/services/* 的腾讯/东财直连(API 服务层,非模型层)
- `core/level2.py` Level2 因子(数据层未就绪)
- ML 模型架构升级(XGBoost → LightGBM)
- Pairs Trading 策略
