# TODO — A 股量化交易系统开发路线图

> 更新日期：2026-05-07  
> 下一目标：IPO Stars Sprint 1 数据源实现

---

### 新增完成（IPO Stars 基础架构，2026-05-06）

| 模块 | 文件 | 说明 |
|------|------|------|
| IPO Stars 数据模型 | `backend/services/ipo_stars/models.py` | IPOCandidate / ScoringResult / PricingStrategy / AnalysisReport 四个 NamedTuple |
| IPO Stars 评分引擎 | `backend/services/ipo_stars/scorer.py` | 四维评分（情绪45%/筹码25%/故事力20%/估值10%）+ 三档挂单价 + 推荐等级 |
| IPO Stars 数据库 | `backend/services/ipo_stars/db.py` | 3 张表（candidates/analyses/subscriptions）+ 完整 CRUD |
| IPO Stars 数据获取层 | `backend/services/ipo_stars/fetcher.py` | 5 个数据获取接口（抽象层，待接入具体数据源） |
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

### Sprint 1：HKEX 数据源接入 + 大盘环境（1-2 周）

> **数据源调研结论（2026-05-06）**：
> - AkShare 对港股 IPO 数据**零覆盖**，`hk_ipo_em()` 不存在，东方财富接口 `RemoteDisconnected`
> - HKEX 官网 IPO 日历 ✅ HTTP 200 可直接解析，招股书 PDF ✅ 可下载 + PyMuPDF 解析
> - 新浪恒生科技指数 `hq.sinajs.cn` ✅ HTTP 200，从 Sprint 3 前置到此处

- [x] **[P0] HKEX IPO 日历爬取** *(2026-05-07 完成)*
  - `_HKEXTableParser` HTMLParser 实现 + `fetch_upcoming_ipos()` 解析入库
  - 自动判断状态（upcoming/subscripting/allotted）
  - 5 个 fetcher 测试覆盖

- [x] **[P0] 招股书关键数据提取** *(2026-05-07 完成)*
  - `_parse_prospectus_pdf()` + `fetch_prospectus()` PyMuPDF 实现
  - 提取：发行价区间/发行规模/上市日期/保荐人/稳价人/基石占比

- [x] **[P0] 恒生科技指数 Bias 接入** *(2026-05-07 完成)*
  - `fetch_market_context()` 接入新浪 `hq.sinajs.cn/list=rt_hkHSTECH`
  - 输出：hstech_close / hstech_prev_close / hstech_change_pct

- [x] **[P1] 稳价人：当只提取 + 手动录入** *(2026-05-07 完成)*
  - 招股书 PDF 自动提取 + `POST /ipo/<code>/update` 手动补充

### Sprint 2：认购数据 & 估值锚点（1-2 周）

> **数据源调研结论（2026-05-06）**：
> - 券商公开孖展数据**全部不可用**（DNS/超时/404），根因：地域限制（香港 IP 才能访问）
> - 替代方案：上市后 HKEX 分配结果 PDF（可拿到实际超购/分配数据）

- [x] **[P0] 分配结果 PDF 解析** *(2026-05-07 完成)*
  - `_parse_allotment_pdf()` + `fetch_allotment_results()` 实现
  - 提取：最终定价/超购倍数/回拨/基石占比/发行规模

- [ ] **[P1] 富途 Open API 接入**（异步推进）
  - 申请富途开发者账号，接入 IPO 认购数据 API
  - 数据：各券商融资倍数、综合超购倍数、实时暗盘
  - 不阻塞主线开发

- [x] **[P1] 暗盘价预估模型** *(2026-05-06 完成)*
  - 文件：`backend/services/ipo_stars/scorer.py` → `estimate_dark_price_range()`
  - 方案：LLM 决策 + 结构化数据信号，降级为规则估算
  - 输出：`DarkPriceEstimate` NamedTuple（含溢价率、置信度、推算依据）
  - 集成：挂单价计算（`compute_pricing`）自动使用暗盘预估指导三档定价

- [ ] **[P1] Pre-IPO 溢价率自动计算**
  - 增强 `fetch_prospectus()` 返回 `pre_ipo_cost` 字段
  - 数据源：招股书"历史融资"章节解析（或手动录入后端接口）

### Sprint 3：行业数据 & 评分增强（1 周）

- [x] **[P1] 同行业新股首日表现统计** *(2026-05-07 完成)*
  - 新增 `db.list_sector_performance()` 按行业查询已上市标的首日表现
  - 新增 `ipo_candidates.first_day_return` 列（含旧表兼容 ALTER TABLE）
  - `service.analyze()` 自动从 DB 填充 `market_ctx['sector_ipo_performance']`

### Sprint 4：LLM 故事力分析（1 周）

- [x] **[P0] IPO 专用 LLM Prompt 设计** *(2026-05-07 完成)*
  - 新增 `backend/services/llm/prompts/ipo_narrative.py`
  - 三维输出：hotness / scarcity / narrative_strength / overall（0~1）
  - `scorer._llm_narrative()` 使用专用 prompt，降级为纯关键词匹配
  - 已注册到 `SYSTEM_PROMPTS['ipo_narrative']`

- [x] **[P1] 热点关键词库动态更新** *(2026-05-07 完成)*
  - 迁移到 `core/config.py IPOStarsConfig.hot_keywords` 列表
  - YAML 可配置：`config/trading.yaml ipo_stars.hot_keywords`
  - `IPOScorer.__init__` 接受 `hot_keywords` 参数

- [x] **[P2] 稀缺性评分增强** *(2026-05-07 完成)*
  - `_compute_scarcity_bonus()` 查询同行业已上市数量
  - 首股 → 1.0；1~2 只 → 0.5；3+ → 0.0
  - 集成到 `_score_narrative()` 权重 15%

### Sprint 5：报告模板 & 推送增强（1 周）

- [x] **[P0] 飞书富文本卡片升级** *(2026-05-07 完成)*
  - 升级：飞书交互卡片（`msg_type: interactive`），彩色 header + lark_md sections + hr 分割线
  - 包含：推荐等级颜色映射、评分条形图（█░）、暗盘预估、挂单策略、风险提示
  - 新增 `_feishu_section()` / `_score_bar()` 静态方法

- [x] **[P1] 定时批量分析 + 自动推送** *(2026-05-07 完成)*
  - `backend/main.py` Scheduler 新增 `_trigger_ipo_batch_analysis()`
  - 调度：每日 18:00 自动运行 `batch_analyze(push=True)`
  - 条件：`ipo_stars.enabled=true` 且 `webhook_url` 非空

- [x] **[P2] 钉钉 ActionCard 模板** *(2026-05-07 完成)*
  - 升级为 ActionCard 格式，含评分条形图、暗盘预估、风险提示
  - 底部"查看详情"按钮链接 HKEXnews

### Sprint 6：端到端集成 & 验证（1-2 周）

- [x] **[P0] 全链路集成测试** *(2026-05-07 完成)*
  - 5 个集成测试：完整流水线 / fetcher 降级 / 标的不存在 / 批量分析 / webhook 推送
  - 覆盖：正常路径 + fetcher 失败降级 + LLM 不可用降级

- [x] **[P0] 历史 IPO 回测验证** *(2026-05-07 完成)*
  - 8 只历史港股 IPO 样本（2024-2025），Spearman IC = 0.95（远超 0.15 目标）
  - 方向命中率 100%（评分高的标的实际涨幅确实高）
  - 输出：`outputs/ipo_stars/backtest.json` + CLI 报告

- [x] **[P1] Streamlit 可视化面板** *(2026-05-07 完成)*
  - 新增页面：`streamlit_app.py` → 🌟 IPO Stars 标签页
  - 内容：候选列表 + 实时分析（评分雷达图、暗盘预估、挂单策略）+ 打新结果追踪 + 订阅列表
  - 交互：输入代码 → 触发分析 → 展示完整报告

- [x] **[P2] 打新收益追踪** *(2026-05-07 完成)*
  - 新增表：`ipo_results`（code/name/predicted_score/subscribe_price/first_day_open/first_day_close/pnl_per_lot）
  - DB 层：`save_result()` / `get_result()` / `list_results()` CRUD
  - API：`GET/POST /ipo/<code>/result` + `GET /ipo/results`
  - 5 个 PnL 测试通过

---

## Backlog（无固定时间表）

### IPO Stars 延后项

- [ ] **稳价人历史战绩批量回填**
  - 原 Sprint 1 P1，降级原因：NLR Excel 无稳价人列，需逐个下载招股书提取，K 线数据源不稳定
  - 方案：待富途 API 接入后批量获取历史首日涨跌幅

- [ ] **认购热度加速度计算**
  - 原 Sprint 2 P1，降级原因：孖展时间序列依赖实时券商数据，数据源全部不可用
  - 前提：富途 Open API 或其他实时数据源可用后再实现

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
