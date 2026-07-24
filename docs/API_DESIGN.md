# API 设计

## 1. 基线

- REST 前缀：`/api/v1`。
- 内容类型：JSON 使用 `application/json`；文件上传使用 `multipart/form-data`。
- 契约来源：Rust 类型与 Utoipa 生成 OpenAPI；生成结果必须进入契约测试。
- Cookie Session 用于浏览器登录，不向前端暴露数据库或 AI 凭据。
- 服务端从 Cookie Session 解析唯一的个人 Workspace；所有业务 API 隐式绑定当前 `workspace_id`。
- MVP 不提供客户端指定 Workspace、切换 Workspace、邀请成员或共享资源的接口。
- 普通查询使用 REST；后台任务进度和 AI 流式回复使用 SSE；noVNC 使用 WebSocket。

## 2. 通用规范

### 2.1 响应

成功单资源：

```json
{
  "data": {},
  "meta": {
    "request_id": "..."
  }
}
```

列表：

```json
{
  "data": [],
  "page": {
    "next_cursor": null,
    "has_more": false
  },
  "meta": {
    "request_id": "..."
  }
}
```

错误：

```json
{
  "error": {
    "code": "validation_error",
    "message": "请求参数无效",
    "fields": [
      {
        "field": "trade_date",
        "code": "invalid_date",
        "message": "日期格式无效"
      }
    ]
  },
  "meta": {
    "request_id": "..."
  }
}
```

错误 `message` 面向用户且脱敏；内部堆栈只进入受控日志。

### 2.2 HTTP 语义

| 状态码 | 使用场景 |
| --- | --- |
| `200` | 查询或同步操作成功 |
| `201` | 资源创建成功 |
| `202` | 后台任务已接受 |
| `204` | 无响应体的成功操作 |
| `400` | 语法或参数错误 |
| `401` | 未登录或 session 失效 |
| `403` | 权限或 CSRF 校验失败 |
| `404` | 资源不存在或调用者不可见 |
| `409` | 唯一键、状态或版本冲突 |
| `413` | 上传超限 |
| `422` | 业务校验失败 |
| `429` | 频率或配额超限 |
| `503` | 可恢复依赖不可用 |

### 2.3 分页、排序和过滤

- 大列表使用不透明 cursor，不暴露内部主键编码。
- 排序字段使用白名单，例如 `sort=trade_date:desc`。
- 日期区间使用 `from`、`to`，明确包含性。
- 限制最大 `page_size`，具体值在容量确认后确定。

### 2.4 幂等与并发

- 文件确认、回滚、任务创建和外部回调支持 `Idempotency-Key`。
- 幂等键的服务端作用域为 `(workspace_id, operation, idempotency_key)`。
- 可编辑资源返回 `ETag` 或 `row_version`；更新使用 `If-Match` 或请求字段。
- 冲突返回 `409 conflict`，不得最后写入者静默覆盖。

## 3. 鉴权接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/auth/login` | 验证凭据、轮换 session、设置 Cookie |
| `POST` | `/api/v1/auth/logout` | 撤销当前 session |
| `GET` | `/api/v1/auth/me` | 当前用户和权限 |
| `POST` | `/api/v1/auth/bootstrap` | 仅系统无用户时可用，校验 `BOOTSTRAP_TOKEN` 并创建首个管理员、个人 Workspace 和 Owner 关系 |
| `GET` | `/api/v1/workspace` | 当前个人 Workspace 的只读元数据 |
| `GET` | `/api/v1/auth/csrf` | 获取短期 CSRF token 或初始化双重提交机制 |
| `GET` | `/api/v1/sessions` | 管理员或本人查看有效 session |
| `DELETE` | `/api/v1/sessions/{session_id}` | 撤销 session |

所有有副作用的方法校验 CSRF token、`Origin`/`Referer` 和权限。Cookie 的 `SameSite` 不是唯一 CSRF 防线。

`workspace_id` 不从请求体、query 或自定义 header 中接受。请求中的资源 UUID/BIGINT 即使存在，只要不属于当前 Workspace，一律按不可见资源处理。

## 4. 导入接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/imports` | 创建批次并上传文件 |
| `GET` | `/api/v1/imports/{import_id}` | 状态、计数和参数 |
| `POST` | `/api/v1/imports/{import_id}/inspect` | 识别格式、工作表、编码和表头 |
| `PUT` | `/api/v1/imports/{import_id}/mapping` | 保存字段映射与转换参数 |
| `POST` | `/api/v1/imports/{import_id}/preview` | 生成预览与行级错误 |
| `POST` | `/api/v1/imports/{import_id}/confirm` | 锁定参数并提交后台任务 |
| `POST` | `/api/v1/imports/{import_id}/cancel` | 取消尚未提交或可安全停止的任务 |
| `POST` | `/api/v1/imports/{import_id}/rollback` | 回滚已成功批次 |
| `GET` | `/api/v1/imports/{import_id}/errors` | 分页读取错误与冲突 |

`confirm` 和 `rollback` 必须带幂等键。上传成功不等于正式数据已写入。`rollback` 先执行全批次依赖检查，存在后续修改或引用时返回 `409 rollback_conflict`；不提供部分回滚接口，纠错通过新建补偿批次完成。

## 5. 业务接口

### 5.1 基础目录与行情

- `/api/v1/exchanges`
- `/api/v1/instruments`
- `/api/v1/contracts`
- `/api/v1/trading-calendars`
- `/api/v1/market-prices`
- `/api/v1/data-sources`

### 5.2 套利

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/spreads` | 创建套利定义 |
| `POST` | `/api/v1/spreads/{spread_id}/formula-versions` | 新建公式版本 |
| `POST` | `/api/v1/spreads/{spread_id}/recalculate` | 创建重算任务 |
| `GET` | `/api/v1/spreads/{spread_id}/series` | 按公式版本查询序列 |
| `GET` | `/api/v1/spreads/{spread_id}/statistics` | 查询统计与样本口径 |
| `GET` | `/api/v1/spreads/{spread_id}/seasonality` | 查询季节性 |

序列响应必须包含 `formula_version_id`、`price_basis`、`calendar_version_id`、`sample_count`、`data_cutoff_at` 和来源摘要。

允许的 `price_basis` 至少包含 `close`、`settlement`、`trade`。行情图默认 `close`；日终持仓、未实现盈亏和权益默认 `settlement`；已实现盈亏固定使用 `trade`。

### 5.3 成交与持仓

- `/api/v1/accounts`
- `/api/v1/trade-fills`
- `/api/v1/trade-groups`
- `/api/v1/position-snapshots`
- `/api/v1/daily-equity`
- `/api/v1/portfolio/performance`

绩效响应必须返回 `pairing_method=fifo`、`price_basis`、手续费合计和费用参数版本。原始 `trade_fills` 不提供覆盖式 `PUT/PATCH`；纠错使用冲销/补偿成交接口并引用原记录。

### 5.4 席位

- `/api/v1/seat-entities`
- `/api/v1/seat-aliases`
- `/api/v1/seat-classification-versions`
- `/api/v1/seat-classifications`
- `/api/v1/seat-positions`
- `/api/v1/seat-statistics`

分类响应必须包含 `classification`、`confidence`、`evidence`、`valid_from`、`valid_to`。

## 6. 图表、连接器与 AI

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/charts/preview` | 返回前端图表配置与数据 |
| `POST` | `/api/v1/charts/render` | 创建静态图任务 |
| `GET` | `/api/v1/charts/{chart_id}/image` | 获取已生成图像 |
| `POST` | `/api/v1/browser-sessions` | 创建授权站点会话 |
| `POST` | `/api/v1/browser-sessions/{session_id}/novnc-token` | 创建与当前应用 session 绑定的短期 noVNC 访问 token |
| `DELETE` | `/api/v1/browser-sessions/{session_id}` | 撤销并清除会话 |
| `POST` | `/api/v1/extraction-jobs` | 对已配置的白名单数据源连接器创建提取任务 |
| `GET` | `/api/v1/extraction-jobs/{job_id}/preview` | 查看提取结果并确认 |
| `POST` | `/api/v1/ai/chat` | 创建或继续对话 |
| `POST` | `/api/v1/ai/providers/{provider_id}/test` | 管理员测试提供商 |
| `GET` | `/api/v1/ai/usage` | 查询用量和费用 |

MVP 不接受任意外部 URL。API 只接受当前 Workspace 下已配置的 `data_source_id` 和连接器操作；服务端验证连接器、域名白名单与授权状态。第一批启用交易所公开数据源，三禾连接器在授权范围确认前返回禁用状态。

连接器提取阶段使用固定优先级：官方 API、网络请求、HTML 表格、下载文件、OCR。包含 OCR 的结果必须进入 `waiting_for_user`，经人工确认接口审计通过后才能提交正式导入。

`/api/v1/extraction-jobs` 对应数据库 `extraction_jobs` 元数据表；异步执行由 `job_queue` 承载。API 路径中的 `{job_id}` 使用 `extraction_jobs.id`，响应中可同时返回底层 `job_queue_id` 供运维排查。

## 7. 任务与进度

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v1/jobs/{job_id}` | 状态、进度、尝试次数、错误 |
| `POST` | `/api/v1/jobs/{job_id}/cancel` | 请求取消 |
| `GET` | `/api/v1/jobs/{job_id}/events` | SSE 进度事件流 |

状态统一为 `queued`、`running`、`waiting_for_user`、`succeeded`、`failed`、`cancelled`、`dead_letter`。

任务查询和事件流始终按 session 对应的 `workspace_id` 过滤；不得仅以可猜测的 `job_id` 查询。

## 8. OpenAPI 和契约验证

- OpenAPI 必须描述鉴权 Cookie、CSRF header、错误体、幂等 header 和所有枚举。
- 接口实现、前端客户端和测试夹具使用同一契约来源。
- CI 比较生成的 OpenAPI，未评审的破坏性变更不得合并。
- API 版本升级只在无法兼容时进行；`/api/v1` 内新增可选字段不视为破坏性变更。
