# Phase 5 Planner 契约（修订版）：套利分析

> **状态(2026-08-18):5A 与 5B 均已上线生产并大幅演进**,本文是 2026-08-05 的
> 立项契约存档。现行监控规则(分层徽标/分品种回撤档/历史信号/组合圈定)与本文
> 差异很大,以 `docs/DECISIONS.md`(DEC-062~072)与生产代码为准。

状态：用户已于 2026-08-05 确认契约并授权按第 10 节开始 5A 实施；5A 收口前不实施
5B，不恢复或修改回填。

基线：`main@b5db24e`；分支：`phase/05-spread-analytics`。

依据：`DEC-014`、`DEC-015`、`DEC-016`、`DEC-017`、`DEC-020`、
`DEC-024`、`DEC-027`、`DEC-033`、`DEC-036`、`DEC-040`、`DEC-042`，以及
`sanheapi/sanheshuju-api-analysis.md`、`sanheshuju_client.py`、
`sanheshuju_request.curl` 的本机只读分析结果(**这三个文件从未进过本仓库**,
是立项时在本机分析的三合数据接口材料,在工作树里找不到是正常的)。

视觉基线：用户随本单提供的效果图是 Phase 5 唯一视觉蓝本，其中自由价差三张、
套利监控一张。实现不得另行套用通用后台模板改变信息层级、图表语义或红涨绿跌口径；
响应式细节、错误态和无障碍状态可在不改变蓝本的前提下补齐。

## 1. 阶段目标与交付顺序

Phase 5 建立一级导航“套利分析”，包含：

1. `5A 自由价差`：首发，三禾只读 API 提供原始价差序列，平台服务端完成散户
   可交易窗口裁剪、连续轴拼接、季节图和月度矩阵重算。
2. `5B 套利监控`：5A 收口后实施，使用平台自有行情库完成组合定义、监控列表和
   详情分析；统计窗口随自有数据积累扩大。

5A 与 5B 分别提交、分别部署候选、分别通过独立 Evaluator。5A 未通过门禁时不得
以 5B 功能掩盖或绕过其问题；5B 不阻塞已经通过的 5A 独立收口。

本 Planner 单只交付本契约、`DEC-042` 和受影响的规划状态。推送后停止，等待用户
确认；未收到另单实施授权前，不创建迁移、不修改 Rust/Vue、不调用三禾生产接口、
不部署。

## 2. 已确认边界

### 2.1 本阶段包含

- 一级导航“套利分析”及二级入口“套利监控”“自由价差”。
- 5A 的双品种、双月份自由价差查询、常用组合、自定义收藏和三块分析内容。
- Rust API 内的 `SpreadSeriesProvider` 端口以及 `sanhe`、`self_hosted` 两种实现。
- 三禾白名单、服务端只读代理、限频、持久缓存、稳定错误映射和来源披露。
- 散户可交易窗口规则、版本化后处理、段界、季节重算和月度涨跌矩阵。
- 5B 的表单式组合定义、监控列表、详情页、确定性统计和自有数据来源下钻。
- ECharts 交互及浏览器端 PNG/SVG 导出；SVG 安全清洗沿用 `DEC-040`。

### 2.2 明确排除

- 不接入三禾 `/ajax/broker_positions.php`、`all_brokers.php`、`broker_dates.php`
  或任何其他席位持仓接口；席位能力留待 Phase 7 另议。
- 不做历史回测、模拟成交、自动信号、择时建议、what-if 或自动交易。
- 5A 不支持自定义系数、比价、百分比价差或任意多腿。三禾接口只返回已计算的
  两腿差值，无法用原始腿价格独立复核这些公式。
- 不把三禾 `year_data`、`stat_ret`、`stat_rate` 作为用户可见统计真值。
- 不恢复或启动 Phase 4B 回填，不改变其服务、游标、保护窗或数据；用户指令为
  保持暂停，恢复必须另行授权。
- 不建设服务端图表渲染、回测引擎或新的浏览器采集基础设施。

## 3. DEC-042 三禾只读 API 接入契约

### 3.1 数据源与允许路径

在 `data_sources` 登记：

| 字段 | 固定值 |
| --- | --- |
| `code` | `sanhe_spread_readonly` |
| `source_type` | `aggregator` |
| `base_domain` | `sanheshuju.com` |
| `authorization_status` | `user_authorized_readonly` |
| `connector_code` | `sanhe_spread_v1` |
| UI 名称 | `三禾数据` |

首期只允许 HTTPS `POST` 到以下三个固定路径：

| 路径 | 用途 | 请求字段 |
| --- | --- | --- |
| `/ajax/all_varieties.php` | 品种清单 | 无 |
| `/ajax/variety_contracts.php` | 品种可选月份 | `variety` |
| `/ajax/arbitrage_varieties.php` | 历史价差序列 | `variety1`, `code1`, `variety2`, `code2` |

月份必须是两位字符串并保留前导零。请求字段使用
`application/x-www-form-urlencoded; charset=UTF-8`。禁止客户端提交 URL、Host、
Header、Cookie、Token 或任意上游路径；服务端适配器只从经过校验的业务参数构造
固定请求。重定向必须重新校验 scheme、host 和 path，任何不在本表内的目标立即拒绝。

### 3.2 服务端代理与信任边界

```text
Vue 页面
  -> Rust /api/v1/spread-analytics/*
      -> SpreadSeriesProvider
          -> SanheSpreadSeriesProvider
              -> persistent cache
              -> global upstream throttle
              -> sanheshuju.com fixed POST endpoints
      -> retail tradable-window processor
      -> recomputed seasonal/monthly DTO
```

- 浏览器绝不直连三禾，不暴露上游地址构造、请求头或原始异常。
- 三禾数据只作为价差序列 provider；平台领域规则、窗口裁剪和统计均在 Rust 服务端。
- 上游当前无需凭证；实现不得把真实 Cookie/Token 加入代码、数据库、日志或前端。
  若未来新增鉴权，必须另行决策，不得静默启用用户会话抓取。
- 日志只记录 correlation ID、provider、endpoint code、参数哈希、缓存命中、取数时间、
  HTTP/业务状态和稳定错误码；不记录完整响应正文。

### 3.3 限频、缓存与并发

- 所有实际发往 `sanheshuju.com` 的请求之间至少间隔 `1.5s`，不按用户、接口或
  API 进程分别放宽。
- 使用 PostgreSQL 中的 provider throttle 状态和事务锁/等价跨实例机制串行化
  cache miss，保证多 API 实例和并发请求不会突破间隔或形成惊群。
- 三个接口均按“端点 + 规范化参数 + Asia/Shanghai 业务日期”缓存。同参数当日
  命中直接返回，不再次访问上游。
- 缓存必须落库，至少记录 `provider_code`、`endpoint_code`、`parameter_hash`、
  `parameters_json`、`business_date`、`fetched_at`、`http_status`、`business_code`、
  `payload_json`、`result_kind` 和响应摘要哈希。
- `parameters_json` 只含固定业务参数；`payload_json` 保存成功解析的只读响应。
  缓存表属于系统级 provider 缓存，不含 Workspace 私有数据，不启用 Workspace RLS。
- 合法空结果也缓存到当日，`result_kind=empty`；错误响应不得冒充成功缓存。过去日期
  的成功缓存不在失败时自动冒充今日结果。
- 适配器 cache miss 每次只发出一次上游请求；本层不做自动重试。调用者快速刷新、
  Worker 重试或代理重试都不得绕过同一限频/失败抑制状态。
- 失败抑制默认至少 60 秒；若上游提供更长 `Retry-After` 则采用更长时间。抑制期内
  同参数直接返回同一稳定错误和剩余 `retry_after_seconds`，不再次访问上游。

### 3.4 响应判定与稳定错误

以下情况必须区分：

| 上游结果 | 平台语义 |
| --- | --- |
| HTTP 200、`code=0`、存在有效数据 | `ok` |
| HTTP 200、`code=0`、`data` 为空或序列为空 | `empty`，合法业务结果 |
| HTTP 非 2xx、`code!=0`、超时/DNS/TLS 失败 | provider 失败 |
| JSON 非法、字段缺失、数组长度不一致、类型漂移 | provider 契约失败 |
| HTTP 401/403/429 | 专用稳定错误并停止请求，不自动重试 |

外部 API 返回稳定错误 envelope，不转发上游正文：

| `error.code` | HTTP | 含义 |
| --- | ---: | --- |
| `spread_provider_unavailable` | 503 | 上游网络/5xx/超时 |
| `spread_provider_rate_limited` | 503 | 上游 429；可带服务端控制的 `retry_after_seconds` |
| `spread_provider_forbidden` | 503 | 上游 401/403 或未来授权变化 |
| `spread_provider_contract_changed` | 502 | JSON 或字段契约发生不兼容变化 |

错误 envelope 保持平台既有 `code/message/correlation_id` 结构；面向用户的 message
不得猜测三禾状态。适配器失败后保留稳定 provider 边界，将来切换到自有数据实现时
不改变页面主 DTO。

## 4. 可插拔序列提供者

应用层定义 `SpreadSeriesProvider`，不得让 use case 依赖三禾字段名：

| 操作 | 输入 | 规范化输出 |
| --- | --- | --- |
| `list_varieties` | provider context | provider variety code/name/market/symbol |
| `list_contract_months` | provider variety | 两位月份字符串及取数元数据 |
| `load_series` | 两腿品种与月份、`price_basis` | 日期、差值、两腿实际合约代码、来源元数据 |

两种实现：

- `sanhe`：5A 首发启用，只接受双腿减法；使用三禾三个固定只读接口。
- `self_hosted`：使用平台 `market_prices`、`contracts` 和版本化交易日历计算；等待
  akshare 自有数据回填恢复并达到所需覆盖后用于自由价差无缝替换，也直接服务 5B。

领域 DTO 必须包含：

```text
provider = sanhe | self_hosted
source_code
source_display_name
source_type
fetched_at
data_cutoff_at
price_basis
provider_algorithm_version
```

5A 页面固定显示“数据来源：三禾数据”和取数时间。不得仅显示平台名称、交易所名称
或把聚合源伪装成自有行情。未来切换 provider 时只改变响应元数据和真实来源标签，
不改变页面三块分析 DTO 的语义。

## 5. 散户可交易窗口引擎

### 5.1 输入验证与合约解析

对 `data.dates[i]` 与 `data.spreads[i]` 严格一一配对：

- 日期必须是 ISO `trade_date`，`value` 必须是有限十进制值。
- `from_code`、`to_code` 规范化后必须能解析到本库 `contracts`；匹配使用交易所、
  品种和完整合约代码，不能只按末两位月份猜测。
- `contracts.delivery_month` 是确定窗口的必需字段；缺失或歧义点进入可追溯
  `quality_issues`，不得进入用户统计。
- 三禾未返回原始腿价格，5A 的 `price_basis=upstream_spread`，不得错误标记为
  `close` 或 `settlement`。响应同时如实声明 `raw_leg_prices_available=false`。

### 5.2 分段与窗口规则

相邻点的 `(from_code, to_code)` 相同则属于同一候选段；任一实际合约变化即新段。
每段按以下规则计算：

1. 起点：该合约对在上游序列中两腿均有有效数据的首个交易日。由于三禾只返回
   已计算差值，单点是否有效以日期、值和两腿实际代码同时有效为准。
2. 较早腿：比较两腿 `delivery_month`，月份更早者；月份相同时两腿共同约束。
3. 默认止点：较早腿交割月前一个自然月的最后一个交易日，交易日以该腿交易所的
   已选版本化日历为准。
4. 品种覆盖：`retail_trade_window_rules` 可按交易所/品种配置相对交割月、自然月
   位置、月内第/倒数第 N 个交易日或明确规则参数。命中多个规则时按精确品种优先、
   交易所默认次之；规则版本不可原地改写。
5. 若两腿规则都给出止点，取较早止点；止点之后数据全部剔除。
6. 若起点晚于止点，该段为合法不可交易段，不输出观测，仅返回质量原因。

规则输出必须记录 `window_algorithm_version`、`window_rule_version`、
`calendar_version_ids`、候选/保留/剔除点数和每段起止原因。首版算法键固定为
`retail_window_v1`；任何口径变化必须新版本重算，不能静默改历史。

### 5.3 连续轴拼接与段界

- 窗口外数据先剔除，再按保留交易日升序拼接成单一类目轴；年度/换段之间不插入
  虚构空日，也不把下一段首日错配到上一段末日。
- 每个输出点保留真实 `trade_date`、`value`、`from_code`、`to_code`、`segment_id`
  和来源引用。
- 每个段首输出 `segment_boundaries`，包含前后合约、边界日期和 `contract_roll`
  原因；前端以细灰虚线标记并支持悬停说明。
- 连续轴不得保留“三禾错误统计时段”的灰色空档数据；效果图中的灰色不可交易区
  只可用于解释被删除区间，主统计序列不含其中点。

### 5.4 季节叠年图

季节图完全由裁剪后的点重算，不读取三禾 `dates2/year_data`：

- 服务端以段的可交易周期生成可跨年有序的日历轴；轴从两腿有效数据的实际起点开始，
  到散户止点结束。跨年周期必须保持例如 `09-01 … 12-31, 01-01 … 08-31`
  的业务顺序，不按字符串重新排序。
- 每个窗口年度一条线；不存在的日历日为 `null`，不得插值或前向填充。
- 当前窗口年度线加粗，历史年度使用弱化颜色；年份图例可点选显隐。
- 响应提供每年的样本起止、点数、缺失数、段与规则版本，页面悬停可追溯实际日期。

### 5.5 月度涨跌矩阵

月度矩阵完全由裁剪后的点重算，不读取三禾 `stat_ret/stat_rate`：

- 单元格值为该窗口年度、该自然月内“最后一个有效点减第一个有效点”；不足两个有效点
  时为不可计算 `null`。
- 正值为红涨，负值为绿跌，零值使用中性色；任何完全位于窗口外的月份显示“—”并留白。
- 底部上涨占比为该月可计算年度中 `monthly_delta > 0` 的数量除以可计算年度数量；
  分母为零时显示“—”。零值不计入上涨数但保留在分母。
- 当前未结束月份可显示基于截至 `data_cutoff_at` 的暂算值，DTO 必须标记
  `is_partial=true`，UI 提示“截至当前数据”。
- 完整矩阵显示所有可用历史年度，而不是只显示效果图中的示例三年。

统计算法首版键固定为 `spread_window_stats_v1`。数据库最终金融值使用 decimal；
展示精度由品种 tick/数值范围决定，不改变底层值。

### 5.6 必需边界测试

至少覆盖：

- 同年窗口、跨年窗口和 12 月到次年 1 月的轴顺序。
- 一腿晚于另一腿上市，起点取两腿都有效的首日。
- 实际合约代码换段、两腿不同日换月、重复/乱序日期和日期/价差数组长度不一致。
- 默认“交割月前一月末交易日”、品种规则覆盖、两腿止点取较早者。
- 节假日月末、闰年、交易日历修订和缺失日历。
- 起点晚于止点、整月不可交易、月内仅一个点、零涨跌、当前月部分数据。
- 过零点正负线段插值，不把段界两侧连接成一条假线。

## 6. 数据模型契约

实施阶段新增或兼容扩展下列表，最终以迁移评审为准；本 Planner 单不创建迁移：

| 表 | 关键字段与约束 |
| --- | --- |
| `spread_provider_cache` | 系统级；provider/endpoint/parameter_hash/business_date 唯一；保存成功或合法空结果及 `fetched_at` |
| `spread_provider_throttles` | 系统级；provider 唯一；保存 `last_requested_at`/抑制状态，供跨实例锁定 |
| `retail_trade_window_rule_versions` | Workspace 或系统规则版本、状态、生效期、算法参数；不可变 |
| `retail_trade_window_rules` | 版本内交易所/品种选择器与止点规则；精确品种优先 |
| `spread_provider_series` | Workspace 查询/计算批次、provider/source、参数、取数时间、原始响应摘要、算法版本 |
| `spread_provider_observations` | 日期、值、实际两腿代码、段、保留状态与排除原因；BIGINT identity |
| `spread_window_segments` | 候选/有效起止、规则/日历版本、点数和边界原因 |
| `spread_favorites` | Workspace 自定义收藏；规范化两腿参数唯一；RLS 与审计 |

三禾 provider 观测不伪造 `spread_observation_legs.market_price_id`。5A 可通过专用 provider
表保存“上游只给差值、无原始腿价格”的事实；只有 `self_hosted` 计算才能写入带原始
腿价格下钻的既有 `spread_observations`/`spread_observation_legs` 语义。

缓存和 throttle 是系统级外部 provider 基础设施，不包含 Workspace 私有选择或用户
标识；收藏、查询批次和派生分析属于 Workspace 业务数据，必须启用 RLS，所有唯一键
含 `workspace_id`。

## 7. API 契约

所有 API 位于 `/api/v1`，从 session 解析 Workspace，不接受客户端 `workspace_id`：

### 7.1 5A 自由价差

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET` | `/spread-analytics/providers/sanhe/varieties` | 当日缓存的品种清单 |
| `GET` | `/spread-analytics/providers/sanhe/varieties/{variety}/months` | 当日缓存的可选月份 |
| `POST` | `/spread-analytics/free-spread/query` | 查询、裁剪并返回三块重算结果 |
| `GET` | `/spread-analytics/favorites` | 当前 Workspace 收藏 |
| `POST` | `/spread-analytics/favorites` | 新建收藏 |
| `DELETE` | `/spread-analytics/favorites/{favorite_id}` | 删除收藏 |

`free-spread/query` 请求只接受 provider enum、两个品种标识和两个月份。响应包含：

```text
source
query
quality
algorithm_versions
continuous_series { points, segment_boundaries, current_value }
seasonal_series { axis, years }
monthly_matrix { years, months, up_ratios }
```

写接口沿用既有导入链的检查顺序：Axum 先完成路径、查询串和 JSON 请求体提取；进入
handler 后依次校验 session 认证、Origin、CSRF、权限。因此结构合法的匿名请求优先返回
401，结构合法且 session 有效但缺少/错误 CSRF 的请求返回 403；结构不合法的 JSON 可在
安全检查前由 extractor 返回 400/422，不能用作认证或 CSRF 顺序断言。

合法空结果使用 HTTP 200，三块数据为空且 `quality.status=empty`；provider 错误使用
第 3.4 节稳定 envelope。来源、取数时间、数据截止时间、规则/算法/日历版本不得省略。

### 7.2 5B 套利监控

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `GET/POST` | `/spreads` | 列表/表单式创建组合定义 |
| `GET/PATCH` | `/spreads/{spread_id}` | 详情/受版本控制的修改 |
| `POST` | `/spreads/{spread_id}/recalculate` | 新计算批次，不覆盖旧版本 |
| `GET` | `/spread-monitor` | 监控行 DTO |
| `GET` | `/spreads/{spread_id}/analytics` | 统计卡、主图、叠年线 |

实现必须扩展既有 OpenAPI/Utoipa 单一契约源，前端客户端与测试夹具从同一 schema
校验。组合公式版本变化只能新建版本和计算批次，不得重写历史结果。

## 8. 页面与交互契约

### 8.1 全局

- 桌面为主，复用现有登录、Workspace、错误提示和权限框架。
- 一级导航显示“套利分析”，子项顺序固定为“套利监控”“自由价差”。
- 涨为红、跌为绿；价格/价差正负线同样正红负绿。颜色之外必须辅以数值、符号、
  线型或文字，不能仅凭颜色表达。
- 图表使用 ECharts。PNG/SVG 在前端导出，SVG 下载前执行安全清洗；不调用服务端渲染。
- 空、加载、provider 失败、数据不足和算法排除状态必须各自呈现，禁止把错误画成零线。

### 8.2 自由价差（5A 首发）

页首按蓝本排列四个选择器：第一品种、第一月份、第二品种、第二月份，以及“查看”
按钮。品种清单来自缓存的 `all_varieties`，月份分别来自缓存的
`variety_contracts`。常用组合 pills 至少覆盖蓝本中的卷螺差、豆棕差、油粕比、
玻璃-纯碱、焦煤 9-1；具体参数作为前端/服务端受测配置，不硬编码到图表组件。
用户可创建和删除 Workspace 自定义收藏。`油粕比` 作为蓝本中的快捷标签保留，但
三禾响应没有可独立复核的原始腿价格且 `basis` 字段公开语义未确认；5A 只能把结果
标记为 `upstream_spread`，不得据标签宣称已计算比价。实施前必须用固定三禾 fixture
确认该 preset 的返回语义；若不能确认，pill 保持可见但禁用并说明“上游口径待确认”，
不得用普通差值冒充比价。

区块 1“拼接连续走势”：

- 标题显示品种/月份组合；右侧显示“仅散户可交易窗口”、当前值和真实来源。
- 正值红线、负值绿线，在零点线性插入交点后拆段；不得以渐变近似。
- 当前值为橙色水平虚线和标签；零轴为灰色实线。
- 段界为细灰虚线，悬停说明前后实际合约；段与段之间不得画连接假线。

区块 2“季节叠年图”：

- 横轴只覆盖服务端返回的可交易窗口日历轴。
- 当前年份加粗，历史年份弱化；图例可点选年份显隐。
- tooltip 显示窗口年度、实际日期、实际合约、值和样本状态。

区块 3“月度涨跌矩阵”：

- 行为完整历史年度，列为 1–12 月，红涨绿跌。
- 不可交易或不可计算月显示“—”并留白，当前部分月有明确暂算提示。
- 底部显示按第 5.5 节重算的上涨占比。

### 8.3 套利监控（5B）

监控列表列固定包含组合、当前价差、日变动、统计窗口分位、Z 值、近 60 个有效
交易日 sparkline：

- 统计窗口目标可配置为 5 年，但有效窗口只能使用自有库实际可用样本；标题同时显示
  `data_cutoff_at`、有效起止和样本数，不得在只有 17 日数据时声称“5 年分位”。
- 经验分位首版为 `count(value <= current) / sample_count`；Z 值使用同一窗口样本均值
  和样本标准差。样本不足 2 或标准差为 0 时返回 `null` 并显示“数据不足/序列恒定”。
- 分位 `>=90%` 的行用浅红极端高位底色，`<=10%` 用浅绿极端低位底色；中间区间
  不着极端底色。阈值进入算法参数并随版本记录。
- 日变动为最后两个有效交易日之差；不足两个点为 `null`。sparkline 最多取最近
  60 个有效交易日，不用空日补齐。
- 点击任意行进入详情页。详情包含样本/均值/标准差/分位/Z 值等统计卡、正负主图和
  裁剪口径一致的季节叠年线。
- “新建组合”为表单式定义并执行品种、合约、系数、常数、价格口径和日期对齐校验；
  5B 使用 `self_hosted`，每个结果可下钻到本库原始腿价格和来源。

## 9. 质量、权限与可追溯

- 5A 查询和收藏绑定当前 Workspace；公共三禾缓存不能泄露哪个用户查询过什么。
- 5B 的定义、公式、计算批次、观测和统计全部启用 Workspace RLS；增加跨 Workspace
  API、repository 和直接 SQL 测试。
- 每个统计 DTO 必须有 `source_code`、`data_cutoff_at`、样本区间/数、排除点数、
  `price_basis`、日历版本、窗口规则版本和算法版本。
- 三禾序列只能追溯到 provider 响应、参数哈希和取数时间；UI 明示无原始腿价。
  自有序列必须继续下钻到 `market_prices` 和每条腿。
- 金融值数据库使用既定 numeric 精度；Rust 领域/数据库边界使用十进制定点，统计浮点
  只允许在明确误差范围内使用并以测试固定。
- 收藏创建/删除、组合定义/版本变更、窗口规则发布和重算均产生脱敏审计记录。

## 10. 实施提交序列（确认后的另单）

建议顺序如下；可按依赖进一步拆分，不得混入 Phase 6/7/8：

1. `docs: confirm phase 5 spread analytics contract`。
2. `feat(phase-5a): add sanhe provider cache and window schema`。
3. `feat(phase-5a): add readonly sanhe spread provider`。
4. `feat(phase-5a): add retail tradable window analytics`。
5. `feat(phase-5a): add free spread page`。
6. `test(phase-5a): add provider window and visual gates`。
7. 5A 独立 Evaluator、修复、复核和收口。
8. `feat(phase-5b): add self hosted spread monitor`。
9. `test(phase-5b): add monitor statistics and visual gates`。
10. 5B 独立 Evaluator、修复、复核和收口。

## 11. 5A 验收门禁

### 11.1 确定性与契约

- 三个且仅三个三禾路径可出网；前端网络记录中不存在到三禾的直连请求。
- 并发 cache miss 下实际请求间隔均 `>=1.5s`，同参数当日重复查询命中持久缓存。
- HTTP 200 + `code=0` + 空 data/空序列返回合法空 UI；网络错误、429、401/403、
  JSON 漂移分别映射稳定错误，自动上游请求次数为 1。
- 固定 fixture 的窗口裁剪、段界、季节轴、月度 delta 和上涨占比与人工参考完全一致。
- 跨年、晚上市、规则覆盖和第 5.6 节所有边界单测通过。
- 任何用户可见季节/月度结果都不能来自上游 `year_data/stat_ret/stat_rate`；测试用污染
  这些字段证明结果不变。

### 11.2 UI 与视觉

- 桌面截图逐区块对照用户蓝本：四选择器、pills/收藏、连续走势、季节叠年、矩阵的
  层级、留白、线型和红涨绿跌语义一致。
- 过零插值、段界断线、当前值虚线、年份点选、不可交易月“—”均有组件/浏览器测试。
- 页面始终显示“数据来源：三禾数据”和取数时间；空/错/数据不足不会显示为 0。
- PNG/SVG 可下载，SVG 清洗测试拒绝 script、事件属性、外部资源和危险 URL。

### 11.3 安全与运行

- SSRF fixture 覆盖任意 URL、非 HTTPS、非白名单 host/path、重定向和 DNS 重绑定拒绝。
- 缓存无 Cookie/Token/Workspace 私有数据；日志和 API 错误无上游正文或秘密。
- RLS、收藏越权、CSRF、RBAC、OpenAPI 契约和既有全量回归通过。
- CI、不可变镜像、VPS E2E 和回滚证据齐全后才交给独立 Evaluator。

## 12. 5B 验收门禁

- 组合公式、原始腿、价差、日变动、经验分位、Z 值和 sparkline 可由固定自有库
  fixture 复算；公式版本变化不覆盖旧结果。
- 只有 17 日样本时 UI 如实显示实际区间和样本数；随着每日增量增加，同一算法按新
  `data_cutoff_at` 重算，不声称不存在的五年覆盖。
- `>=90%`/`<=10%` 极端行、空/恒定/缺腿/非同步日状态与详情下钻正确。
- 监控列表、表单式组合定义和详情页逐项对照用户蓝本；PNG/SVG 门禁与 5A 相同。
- 自有 provider 不访问三禾，结果来源指向实际 `market_prices.source_id`；跨 Workspace
  RLS、审计和追溯通过。
- 5B 独立 Evaluator 必须在 5A 收口后单独给出 PASS；主 Agent 不冒充 Evaluator。

## 13. 验证命令与证据要求

确认后的实现至少执行并记录实际摘要：

```bash
cd rust && cargo +stable fmt --check
cd rust && cargo +stable clippy --workspace --all-targets -- -D warnings
cd rust && cargo +stable test --workspace
pnpm lint
pnpm test
pnpm build
git diff --check <phase-5-base>..HEAD
docker compose config --quiet
docker compose -f docker-compose.yml -f docker-compose.production.yml config --quiet
```

Evaluator 证据按 `docs/ACCEPTANCE_CRITERIA.md` 记录 requirement/test/environment/fixture/
expected/actual/evidence/reviewer/reviewed_at，并额外记录 provider、参数哈希、取数时间、
算法/规则/日历版本、样本范围、排除规则和视觉截图。

## 14. 部署、回滚与并行任务护栏

- 本 Planner 单不部署。确认后的 5A/5B 部署必须避开北京时间每日采集与保护窗口；
  至少禁止在 `16:30–22:30` 发起发布、迁移或 E2E，避免 17:30/21:30 cron。
- 发布前只读核验 collector、回填服务和数据库活动；不得为了 Phase 5 停止、恢复、
  重启或改写 Phase 4B。用户当前指令为回填保持暂停，恢复必须另单授权。
- 数据库迁移仅做向前兼容；回滚应用时禁用新入口并切回上一稳定镜像 digest，不执行
  破坏性 down。provider 缓存可保留审计，不手工删除。
- 若三禾接口变更或不可用，5A 返回稳定 provider 错误；不得临时扩大路径、提高频率、
  接入席位接口、添加凭证或用旧数据伪装新鲜结果。

## 15. 完成定义

- 当前 Planner 单：本契约、`DEC-042` 和规划状态完成一致性检查，分支已推送，然后
  停止并等待用户明确确认。
- Phase 5A：第 11、13、14 节全部通过，独立 Evaluator `BLOCKER=0`、`HIGH=0`
  并给出 PASS，才可收口。
- Phase 5B：第 12、13、14 节全部通过，独立 Evaluator `BLOCKER=0`、`HIGH=0`
  并给出 PASS，才可收口。
- 任一门禁未通过时不得把阶段标记为完成；三禾席位、回测/信号/what-if、回填恢复
  和 Phase 6 以后能力继续保持未实施。
