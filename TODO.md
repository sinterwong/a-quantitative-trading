# TODO — A 股量化交易系统开发路线图

> 更新日期：2026-05-06  
> 下一目标：IPO Stars 数据源接入

---

### 新增完成（IPO Stars 基础架构，2026-05-06）

| 模块 | 文件 | 说明 |
|------|------|------|
| IPO Stars 数据模型 | `backend/services/ipo_stars/models.py` | IPOCandidate / ScoringResult / PricingStrategy / AnalysisReport 四个 NamedTuple |
| IPO Stars 评分引擎 | `backend/services/ipo_stars/scorer.py` | 四维评分（情绪45%/筹码25%/故事力20%/估值10%）+ 三档挂单价 + 推荐等级 |
| IPO Stars 数据库 | `backend/services/ipo_stars/db.py` | 3 张表（candidates/analyses/subscriptions）+ 完整 CRUD |
| IPO Stars 数据获取层 | `backend/services/ipo_stars/fetcher.py` | 6 个数据获取接口（抽象层，待接入具体数据源） |
| IPO Stars 推送通知 | `backend/services/ipo_stars/notifier.py` | 飞书卡片 + 钉钉 Markdown 双格式 webhook 推送 |
| IPO Stars 主服务 | `backend/services/ipo_stars/service.py` | 编排获取→评分→报告→推送全流程 |
| IPO Stars API | `backend/api.py` | 5 个 `/ipo/*` 端点（candidates/analysis/analyze/subscribe/subscriptions） |
| IPO Stars 配置 | `core/config.py` + `config/trading.yaml` | IPOStarsConfig dataclass + YAML 配置段 |
| IPO Stars 测试 | `tests/test_ipo_stars.py` | 33 个测试覆盖模型/评分/DB/配置/通知/服务 |

---

## IPO Stars — 港股打新分析模块开发路线图

> **目标**：将 IPO Stars 从"基础架构骨架"推进到"可实际使用的港股打新决策工具"  
> **核心原则**：数据源逐个接入验证，每个 Sprint 交付可测试的增量功能  
> **依赖**：Phase 6-A 港股 Futu 接入完成后可加速

### Sprint 1：HKEX 数据源接入（1-2 周）

- [ ] **[P0] HKEX IPO 日历爬取**
  - 文件：`backend/services/ipo_stars/fetcher.py` → 实现 `fetch_upcoming_ipos()`
  - 数据源：HKEX 官网 IPO 日历页面（HTML 解析 / AKShare `hk_ipo_em()`）
  - 输出：自动入库 `ipo_candidates` 表，包含代码/名称/招股日期/状态
  - 调度：每日 09:00 自动拉取（接入 `backend/main.py Scheduler`）
  - 测试：mock HTML 响应 + 解析验证

- [ ] **[P0] 招股书关键数据提取**
  - 文件：`backend/services/ipo_stars/fetcher.py` → 实现 `fetch_prospectus()`
  - 数据：发行规模、招股价区间、保荐人、稳价人、基石名单
  - 数据源候选：HKEX 招股书 PDF 结构化解析 / 东方财富港股新股页
  - 输出：填充 `IPOCandidate` 全部字段（sponsor/stabilizer/cornerstone_names/cornerstone_pct）

- [ ] **[P1] 稳价人历史战绩数据库**
  - 文件：`backend/services/ipo_stars/fetcher.py` → 实现 `fetch_stabilizer_history()`
  - 方案：爬取近 2 年 HKEX 已上市 IPO 数据，按稳价人聚合首日涨跌
  - 新增表：`ipo_stabilizer_records`（稳价人/代码/上市日期/首日涨跌幅/是否护盘）
  - 输出：胜率/历史项目列表/护盘风格判断
  - 测试：数据完整性校验 + 聚合逻辑单测

### Sprint 2：实时认购数据接入（1-2 周）

- [ ] **[P0] 券商孖展倍数实时采集**
  - 文件：`backend/services/ipo_stars/fetcher.py` → 实现 `fetch_subscription_data()`
  - 数据源：富途牛牛 / 耀才 / 辉立 公开页面或 API
  - 采集：各券商融资倍数、综合超购倍数、回拨比例
  - 缓存 TTL：30 分钟（`config/trading.yaml ipo_stars.cache_ttl_subscription`）
  - 输出：更新 `ipo_candidates` 表的 `margin_multiple` / `public_offer_multiple` / `clawback_pct`

- [ ] **[P1] 认购热度加速度计算**
  - 文件：`backend/services/ipo_stars/scorer.py` → 增强 `_score_sentiment()`
  - 逻辑：记录孖展倍数时间序列，计算招股期内增速（加速 → 加分）
  - 新增表：`ipo_subscription_snapshots`（code/timestamp/margin_multiple/public_offer_multiple）
  - 可视化：时间序列趋势图（供报告嵌入）

- [x] **[P1] 暗盘价预估模型** *(2026-05-06 完成)*
  - 文件：`backend/services/ipo_stars/scorer.py` → `estimate_dark_price_range()`
  - 方案：基于超购倍数/基石占比/综合评分/大盘情绪/回拨比例 五因子推算暗盘价区间 [low, mid, high]
  - 不依赖券商暗盘实时数据，纯推算模型
  - 输出：`DarkPriceEstimate` NamedTuple（含溢价率、置信度、推算依据）
  - 集成：挂单价计算（`compute_pricing`）自动使用暗盘预估指导三档定价

### Sprint 3：大盘环境 & 估值锚点（1 周）

- [ ] **[P0] 恒生科技 Bias + VIX 接入**
  - 文件：`backend/services/ipo_stars/fetcher.py` → 实现 `fetch_market_context()`
  - 数据：HSTECH 近 5 日收盘价 → 计算乖离率；HSI VIX
  - 数据源：AKShare `index_zh_a_hist()` 或 Tencent Finance
  - 输出：`market_ctx` dict 供评分引擎使用

- [ ] **[P1] 同行业新股首日表现统计**
  - 逻辑：按二级行业分类，查询近 3 只同行业 IPO 的首日涨跌幅
  - 数据：来自 `ipo_stabilizer_records` 表（Sprint 1 已建）
  - 输出：填充 `market_ctx['sector_ipo_performance']`

- [ ] **[P1] Pre-IPO 溢价率自动计算**
  - 增强 `fetch_prospectus()` 返回 `pre_ipo_cost` 字段
  - 数据源：招股书"历史融资"章节解析（或手动录入后端接口）
  - 新增 API：`POST /ipo/<code>/update` — 手动补充 Pre-IPO 成本等字段

### Sprint 4：LLM 故事力分析（1 周）

- [ ] **[P0] IPO 专用 LLM Prompt 设计**
  - 文件：`backend/services/llm/prompts/` → 新增 `ipo_narrative` prompt
  - 输入：行业关键词 + 公司名 + 业务亮点摘要
  - 输出：热点匹配度（0~1）、稀缺性判断（是否港股该赛道首股）、叙事强度评估
  - 测试：3 个 mock case（热门AI股/传统制造/赛道首股）

- [ ] **[P1] 热点关键词库动态更新**
  - 当前：硬编码在 `scorer.py` 的 `hot_keywords` 列表
  - 升级：迁移到 `config/trading.yaml ipo_stars.hot_keywords` 或独立 JSON 文件
  - 可选：通过 LLM 自动从近期财经新闻中提取热点词

- [ ] **[P2] 稀缺性评分增强**
  - 查询 `ipo_candidates` 历史数据判断是否"港股同赛道首股"
  - 若是首股 → 稀缺性加分 0.3；第 2~3 只 → 加分 0.1；更多 → 不加分
  - 集成到 `_score_narrative()` 子因子

### Sprint 5：报告模板 & 推送增强（1 周）

- [ ] **[P0] 飞书富文本卡片升级**
  - 当前：纯文本 `msg_type: text`
  - 升级：飞书交互卡片（`msg_type: interactive`），支持折叠/展开、评分仪表盘
  - 模板：严格对齐 `IPO-stars.md` 第 4 节报告模版
  - 包含：综合评估 emoji、挂单价表格、风险标红

- [ ] **[P1] 定时批量分析 + 自动推送**
  - 文件：`backend/main.py` → 注册 Scheduler 任务
  - 调度：每日 18:00 自动运行 `batch_analyze(push=True)`
  - 范围：所有 `status=subscripting` 的标的
  - 条件：`ipo_stars.enabled=true` 且 `webhook_url` 非空

- [ ] **[P2] 钉钉 ActionCard 模板**
  - 当前：钉钉 Markdown 格式
  - 升级：ActionCard 格式，底部加"查看详情"/"订阅提醒"按钮

### Sprint 6：端到端集成 & 验证（1-2 周）

- [ ] **[P0] 全链路集成测试**
  - 测试场景：HKEX 数据拉取 → 入库 → 评分 → 报告生成 → webhook 推送
  - 工具：pytest + VCR（录制 HTTP 响应）或 mock server
  - 覆盖：正常路径 + fetcher 失败降级 + LLM 不可用降级

- [ ] **[P0] 历史 IPO 回测验证**
  - 方法：导入 2024-2025 年已上市港股 IPO 数据
  - 验证：评分与实际首日表现的相关性（Spearman IC）
  - 目标：IC > 0.15（评分高的标的首日涨幅确实更高）
  - 输出：`outputs/ipo_stars/backtest_2024_2025.json` + 散点图

- [ ] **[P1] Streamlit 可视化面板**
  - 新增页面：`streamlit_app.py` → IPO Stars 标签页
  - 内容：候选列表 + 评分雷达图 + 挂单价区间图 + 历史分析记录
  - 交互：输入代码 → 触发实时分析 → 展示报告

- [ ] **[P2] 打新收益追踪**
  - 新增表：`ipo_results`（code/subscribe_price/first_day_open/first_day_close/pnl）
  - 功能：上市后自动拉取首日表现，对比预测评分
  - API：`GET /ipo/<code>/result` — 查询打新结果
  - 用途：模型效果追踪与权重校准

---

## Backlog（无固定时间表）
### 策略研究

- [ ] **强化学习策略框架**
  - 环境：`gymnasium` 标准化 RL 环境（状态=因子值，动作=仓位比例）
  - 算法：PPO（近端策略优化），适合连续动作空间
  - 风险：RL 样本效率低，需大量历史数据

- [ ] **期权对冲组合**
  - 条件：需要期权交易权限（50ETF 期权）
  - 思路：持多头 + 买入认沽期权作为尾部风险对冲
  - 依赖：期权定价模型（Black-Scholes / local vol）

- [ ] **高频因子（1分钟级）**
  - 当前：所有因子基于日线
  - 目标：接入分钟级订单流数据，计算实时 VWAP 偏离因子
  - 前提：TimescaleDB 分钟数据存储完成
