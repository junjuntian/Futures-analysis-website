# Phase 2：身份、个人 Workspace 与隔离基础

## 1. 文档状态

- 状态：用户已确认，进入 Phase 2 实施。
- 实现授权：已授权；Generator 可按本文件范围实施，不得扩大到行情、导入、交易、AI 或外部数据采集。
- 计划依据：`DEC-004`、`DEC-013`、`DEC-026`、`DEC-033`，以及产品、架构、数据库、API、安全和验收设计。
- 阶段承接：实际通过验收的 Phase 1 仅完成工程基础。`docs/DEVELOPMENT_PLAN.md` 曾把身份与审计列入 Phase 1，但这些能力尚未实现；本阶段按 Git、迁移记录和 VPS 实态承接身份、Workspace 与权限基础，不进入该文档原列的“基础目录、对象存储与导入中心”范围。

## 2. 接管与准入基线

本计划以 2026-07-24 接管核验结果为基线：

- 当前分支为 `phase/01-foundation`；接管核验时工作区干净，进入实施前存在已确认的 Phase 2 计划文档改动；HEAD 为 `95ed601`。
- Phase 1 Evaluator 最终状态为 PASS。
- `futures` VPS 上 PostgreSQL、API、Worker、Frontend、Nginx 均运行；API 与 PostgreSQL 为 healthy。
- API live、ready、version、前端首页和 Nginx 代理均返回 HTTP 200。
- 当前部署版本为 `0.1.0`，`git_sha=local`。
- 数据库只记录迁移 `202607240001`（`phase 1 foundation`）。
- `app.current_workspace_id()` 已存在；尚无正式业务表。
- 关键部署文件与本地 Git 源码一致。

进入实现前必须同时满足：

1. 用户已明确确认本计划并授权进入 Phase 2 实现。
2. Phase 1 PASS 不被新的 BLOCKER/HIGH 发现推翻。
3. Generator 开始前工作区可解释；不得覆盖用户或其他 Agent 的未提交修改。
4. 测试数据库和 VPS 部署前备份路径可用。
5. 运行时数据库角色、迁移角色和 Cookie 环境策略按本计划确认；应用运行时不得持有超级用户或迁移所有者权限。

## 3. 阶段目标

Phase 2 只建立可供后续业务模块复用的身份与租户安全底座：

1. 实现一次性首次管理员初始化，在单一事务中创建首个管理员、个人 Workspace 和 Owner 关系。
2. 实现用户名/密码登录、Cookie Session、退出、当前用户、CSRF、Session 查询与撤销。
3. 从服务端 Session 解析唯一个人 Workspace，形成不可由客户端覆盖的 `WorkspaceContext`。
4. 建立 `admin`、`analyst`、`viewer` 的权限策略基础；系统管理员身份不隐含跨 Workspace 业务读取。
5. 建立 Workspace-scoped Repository 事务模板：显式 `workspace_id` 条件与 PostgreSQL RLS 双层隔离。
6. 使第一张 Workspace 范围表在创建时启用并强制 RLS，完成真实跨 Workspace 测试。
7. 提供最小身份前端：初始化、登录、当前用户/Workspace 展示和 Session 管理。
8. 更新 OpenAPI、测试、部署和验收证据，不引入正式金融业务功能。

## 4. 范围

### 4.1 后端范围

- `identity`：用户、密码哈希、角色、Session、CSRF、登录限流接口边界。
- `workspace`：个人 Workspace、Owner 关系、`WorkspaceContext` 解析。
- `authorization`：服务端权限枚举、角色到权限的映射和用例入口守卫。
- `audit`：初始化、登录、退出、Session 撤销、权限拒绝等最小审计/安全事件。
- `database`：身份表迁移、RLS policy、最小权限角色、事务与 Repository 模板。
- `apps/api`：身份路由、Cookie、安全响应、统一错误和 OpenAPI。
- `apps/worker`：仅适配新的最小权限数据库连接；本阶段仍不领取正式业务任务。

### 4.2 API 范围

仅实现已设计的身份与个人 Workspace 接口：

| 方法 | 路径 | Phase 2 语义 |
| --- | --- | --- |
| `POST` | `/api/v1/auth/bootstrap` | 仅未初始化系统可用；校验 `BOOTSTRAP_TOKEN`；原子创建首个管理员、个人 Workspace、Owner 和初始 Session |
| `POST` | `/api/v1/auth/login` | 验证凭据，撤销当前旧 Session（如有），创建并设置新 Cookie |
| `POST` | `/api/v1/auth/logout` | 校验 Session 与 CSRF，撤销当前 Session 并清除 Cookie |
| `GET` | `/api/v1/auth/me` | 返回当前用户、角色、权限摘要和当前个人 Workspace 摘要 |
| `GET` | `/api/v1/auth/csrf` | 为当前 Session 返回短期 CSRF token；不得返回服务端 secret |
| `GET` | `/api/v1/workspace` | 返回当前个人 Workspace 的只读元数据 |
| `GET` | `/api/v1/sessions` | 默认列出本人 Session；管理员可按明确权限查看身份控制面中的其他用户 Session 元数据 |
| `DELETE` | `/api/v1/sessions/{session_id}` | 本人或管理员撤销 Session；不得返回 token/hash |

约束：

- 不接受请求体、query、自定义 header 中的自由 `workspace_id`。
- 跨 Workspace 或无权访问的资源统一按不可见处理；不得通过 `403/404` 差异泄漏资源存在性。
- Bootstrap、登录失败和权限失败使用稳定、脱敏的错误码；用户名是否存在不得被错误文本或时序明显泄漏。
- OpenAPI 必须描述 Cookie、CSRF header、错误体和所有身份枚举，但不得包含秘密示例。

### 4.3 前端范围

- 初始化页：输入首个管理员用户名、密码和由部署者安全提供的 bootstrap token；token 不持久化。
- 登录页：通用错误，不显示“用户存在/不存在”差异。
- 身份状态 Store：启动时调用 `auth/me`，只保存非敏感用户、角色、权限和 Workspace 摘要。
- 路由守卫：未登录跳转登录；路由守卫只改善体验，不作为服务端授权替代。
- 当前 Workspace 只读展示；不出现 Workspace 切换器、邀请、共享或成员管理入口。
- Session 管理：显示创建时间、最近活动、过期时间和当前 Session 标记；允许撤销。
- API 客户端默认携带同源 Cookie；有副作用请求统一附加 CSRF header，并在 `401` 时清理本地身份状态。

## 5. 非目标

本阶段明确不包含：

- 交易所、品种、合约、交易日历和任何行情能力。
- 文件对象、TXT/CSV/XLS/XLSX 导入、任务队列业务、批次回滚。
- 套利、图表、成交、持仓、权益、席位。
- 外部数据采集与连接器。
- AI、`pgvector`、外部模型或任何 AI 工具。
- Workspace 共享、邀请、成员管理、Workspace 切换和多租户 SaaS。
- 用户自助注册、密码找回、MFA、完整用户管理后台；除首次管理员外，新增用户流程需后续单独确认。
- 自动交易、交易账户连接、规则驱动历史回测。
- 生产静态前端重构、完整发布流水线重构；Phase 1 的前端 dev server 和 `git_sha=local` 可作为独立 LOW 项处理，不得挤占身份隔离主线。

## 6. 架构与数据流边界

### 6.1 请求链

```text
同源浏览器请求
  → Nginx
  → Axum Cookie/Origin/CSRF 中间件
  → SessionResolver
  → UserIdentity + WorkspaceContext
  → PermissionGuard
  → begin database transaction
  → SET LOCAL current workspace
  → workspace-scoped Repository（显式 workspace_id）
  → RLS policy
  → commit/rollback
  → 脱敏响应与审计
```

边界要求：

- `WorkspaceContext` 只能由有效 Session 和服务端身份目录解析，不从客户端输入构造。
- `domain` 不依赖 Axum、SQLx、Cookie 或密码库。
- `application` 定义身份用例、权限和 Repository 端口，显式接收 `WorkspaceContext`。
- `database` 实现事务、Repository 和 RLS 设置；不得把可裸用的全局 `PgPool` 暴露给 Workspace 业务用例。
- `apps/api` 负责协议、Cookie、CSRF、Origin、限流和错误映射，不承载权限规则本身。
- 前端隐藏按钮不能替代服务端权限检查。

### 6.2 首次初始化流

1. Bootstrap token 由系统所有者本人在 futures VPS 本地安全生成，保存于 `/etc/futures-platform/secrets/bootstrap-token`，`root:root` 且权限 `0400`；通过 Docker Secret 或只读文件挂载提供给 API。请求中的 token 只能通过 `X-Bootstrap-Token` header 传入，不进入 Git、数据库、镜像、日志、普通 `.env`、URL、审计载荷或响应。
2. 先执行格式、Origin、限流和常量时间 token 校验。
3. 开启数据库事务并锁定预置的初始化状态行；锁内再次检查“未完成初始化”和“用户表为空”。
4. 应用层生成用户和 Workspace 的 UUIDv7。
5. 创建 Argon2id 密码哈希、首个 `admin` 用户、个人 Workspace、Owner membership 和管理员角色关系。
6. 设置当前事务的 Workspace，再写入 Workspace 范围审计。
7. 将初始化状态永久标记为完成，创建初始 Session，单一事务提交；初始化成功后立即使 bootstrap token 失效并删除 token 文件。
8. 任一步失败全部回滚；并发请求只有一个成功，其余返回稳定冲突。

不得只靠“查询用户数量后插入”实现并发控制，也不得在初始化失败后留下孤立用户、Workspace、membership 或 Session。

### 6.3 登录与 Session 流

1. 对用户名采用统一规范化规则并执行登录限流。
2. 无论用户名是否存在都走防枚举路径；密码使用 Argon2id 校验。
3. 成功后解析用户唯一个人 Workspace，创建新的高熵不透明 Session token。
4. 数据库仅保存 token 的不可逆摘要；明文 token 只进入 `HttpOnly` Cookie。
5. 如请求带有旧 Session，登录成功后撤销旧 Session，避免 fixation。
6. 后续请求由 token 摘要解析用户、角色和唯一 Workspace；过期、撤销、伪造 token 均视为未认证。
7. 退出或撤销立即标记失效并清除当前 Cookie；撤销其他 Session 不泄漏其 token。

## 7. 数据库与迁移规划

### 7.1 迁移文件

计划新增一个顺序迁移，建议命名：

```text
rust/migrations/202607240002_identity_workspace_security.sql
```

迁移至少包含：

- `users`
- `workspaces`
- `workspace_memberships`
- `roles`
- `user_roles`
- `sessions`
- `audit_logs`
- `system_settings` 中不可删除的 bootstrap 状态行
- 必要外键、唯一约束、检查约束和索引
- `admin`、`analyst`、`viewer` 角色种子
- RLS policy、grants 和 schema version 记录

字段与约束基线：

- 普通实体由应用层生成 UUIDv7。
- 用户名使用明确规范化后的唯一键；密码只保存带算法/参数信息的 Argon2id 编码哈希。
- MVP 中 `workspaces.owner_user_id` 唯一；`workspace_memberships.user_id` 保证一个用户只有一个个人 Workspace。
- Session 至少记录 token hash、CSRF secret hash、创建/最近活动、绝对过期、空闲过期、撤销时间和轮换关联；所有时间为 `timestamptz`。
- 审计为追加写；敏感字段、Cookie、token、密码哈希和完整请求体不得进入 JSON。
- Bootstrap 完成状态必须是持久化状态，而不是仅依赖当前用户计数。

### 7.2 身份表与 Workspace 业务表分类

- `users`、`roles`、`user_roles`、`sessions`、Workspace 解析所需的 membership 目录属于身份控制面，不依赖 Workspace RLS 来完成登录前解析；只能通过专用身份 Repository 和最小权限查询访问。
- `audit_logs` 是本阶段第一张 Workspace 范围表，必须从创建时启用 `ENABLE ROW LEVEL SECURITY` 与 `FORCE ROW LEVEL SECURITY`。
- `workspaces` 和 membership 的普通读取仍必须带解析后的用户/Workspace 条件；不得提供枚举全部 Workspace 的通用 Repository。
- 后续所有正式 Workspace 业务表必须复用本阶段模板，在首次创建时启用并强制 RLS。

此分类不降低隔离要求：身份控制面只用于“当前用户对应哪个个人 Workspace”的解析，不能成为跨 Workspace 业务查询旁路。

### 7.3 数据库角色与最小权限

RLS 验收前必须拆分：

- 迁移/对象所有者角色：只在受控迁移步骤使用，应用容器不持有其连接串。
- API/Worker 运行时角色：`NOSUPERUSER`、`NOBYPASSRLS`，不是表所有者，只获所需 schema/table/function 权限。
- 测试迁移角色与测试运行时角色：与生产权限模型一致，不能用 PostgreSQL 超级用户冒充 RLS 验收。

当前 Compose 的应用连接使用初始化数据库账户；Phase 2 必须改为非超级用户运行时连接，否则任何 RLS 测试都不能作为有效证据。API ready 检查只需运行时权限，不得回退到管理员连接。

### 7.4 `SET LOCAL`、Repository 与 RLS

每个 Workspace 范围用例必须：

1. 从服务端 `WorkspaceContext` 取得 `workspace_id`。
2. 开启 SQLx transaction。
3. 在该 transaction 上执行 `SET LOCAL app.current_workspace_id = ...`；参数化实现可使用等价的 `set_config(..., true)`，其作用域必须严格为当前事务。
4. Repository 方法同时显式接收 `workspace_id`，SQL `WHERE/INSERT` 仍包含该值。
5. RLS policy 使用 `USING` 与 `WITH CHECK` 校验 `app.current_workspace_id()`。
6. 在同一 transaction 上完成查询、写入和审计后提交或回滚。

禁止：

- 在连接池连接上使用持久 `SET`。
- 先查询全部数据再在内存中过滤 Workspace。
- 只依赖 RLS 而省略 Repository 条件。
- 把迁移/超级用户连接池注入普通用例。
- 允许调用方传入自由字符串冒充 `WorkspaceContext`。

连接归还池前，事务结束必须使本地设置失效；集成测试要证明连接复用不会继承上一个 Workspace。

## 8. 认证、CSRF 与 Cookie 安全要求

- 密码使用 Argon2id；初始生产参数为 memory=64MiB、iterations=3、parallelism=1；必须在本地和 futures VPS 基准测试并以集中配置记录参数版本。
- Argon2id 参数不得低于 OWASP 最低配置；参数升级时支持用户下次登录自动重新哈希。
- 密码策略：最少 15 字符，最多 128 字符；允许 Unicode、空格和密码短语；不强制大小写、数字或特殊符号组合；拒绝常见或已泄漏密码；不做不定期强制修改；登录失败必须有限速和审计。
- Session token 至少具有 256 bit 随机熵；数据库只存 token hash，比较采用常量时间路径。
- Cookie 必须 `HttpOnly`、`SameSite=Lax`，并明确 `Path=/`；非生产环境允许 `Secure=false`。
- 生产环境必须使用 HTTPS 和 `Secure=true`；生产配置不满足要求时认证服务必须拒绝启动；生产优先使用满足约束的 `__Host-` 前缀，不设置宽泛 Domain。
- futures VPS 的 HTTP 8088 仅用于临时开发和验收；所有通过 HTTP 8088 完成的认证 smoke 均标记为非生产验证。
- CSRF token 与当前 Session 绑定并有短有效期；服务端只保存 secret hash，前端通过自定义 header 发送。
- Cookie 认证的所有有副作用请求同时校验 CSRF 与同源 `Origin`/`Referer`；`SameSite` 不能作为唯一防线。
- Bootstrap 和登录没有可依赖的已认证 Session，因此不使用 Session-bound CSRF，但必须校验允许的 Origin/Referer、专用 bootstrap token（仅 bootstrap）并执行限流。
- CORS 默认同源；不得启用带凭据的通配来源。
- Session 绝对有效期为 7 天，空闲有效期为 4 小时，每个用户最多 5 个并发会话；超出上限时撤销最旧会话。
- 修改密码、敏感配置和密钥时必须要求重新认证。
- 登录、退出、撤销、权限拒绝和 bootstrap 事件记录 request ID；日志和审计统一脱敏。

## 9. 权限基础

应用层建立显式 `Permission` 枚举和集中策略，不在 handler 中散落角色字符串判断。

Phase 2 最小矩阵：

| 能力 | `admin` | `analyst` | `viewer` |
| --- | --- | --- | --- |
| 查看本人身份/Workspace | 允许 | 允许 | 允许 |
| 查看、撤销本人 Session | 允许 | 允许 | 允许 |
| 查看、撤销其他用户 Session | 允许，限身份控制面 | 拒绝 | 拒绝 |
| 管理用户、角色、密钥 | 本阶段无接口 | 无接口 | 无接口 |
| 读取其他 Workspace 业务数据 | 拒绝 | 拒绝 | 拒绝 |
| 后续业务写权限 | 预留策略 | 预留策略 | 默认拒绝 |

Bootstrap 创建的首个用户同时具有系统 `admin` 角色和其个人 Workspace 的 Owner 关系。Owner 只表达 Workspace 归属，不自动扩张为共享/邀请模型。

## 10. 任务拆分与依赖顺序

| 顺序 | 任务 | 依赖 | 交付/退出条件 |
| --- | --- | --- | --- |
| `P2-00` | 冻结配置与安全参数 | 用户确认计划 | Session/Argon2/Cookie/TLS 参数已确认；无新增业务范围 |
| `P2-01` | 迁移与数据库角色 | Phase 1 migration | 身份表、bootstrap 状态、角色种子、RLS/grant 落地；运行时角色无绕过能力 |
| `P2-02` | 领域与应用端口 | `P2-00` | User/Role/Permission/WorkspaceContext/Session 用例和稳定错误定义完成 |
| `P2-03` | Repository 与事务模板 | `P2-01`,`P2-02` | 显式 workspace 参数、事务级设置和 RLS policy 可集成测试 |
| `P2-04` | 首次初始化 | `P2-03` | 单事务、永久关闭、并发唯一成功、失败零残留 |
| `P2-05` | 登录、Session、CSRF | `P2-02`,`P2-03` | 登录/轮换/过期/撤销/CSRF/Origin/限流通过 |
| `P2-06` | 权限与审计 | `P2-03`,`P2-05` | 集中权限矩阵、不可见语义、最小审计完成 |
| `P2-07` | API 与 OpenAPI | `P2-04` 至 `P2-06` | 本阶段接口、Cookie/CSRF/error 契约一致 |
| `P2-08` | 最小身份前端 | `P2-07` | 初始化、登录、身份/Workspace、Session 页面和错误态完成 |
| `P2-09` | 安全与回归测试 | `P2-01` 至 `P2-08` | 测试矩阵通过，Phase 1 健康接口无回归 |
| `P2-10` | VPS 部署核验 | `P2-09` | 备份、迁移、非超级用户运行、健康和认证 smoke 有证据 |
| `P2-11` | Evaluator 审查 | `P2-10` | BLOCKER/HIGH 清零并最终 PASS |

Generator 不得并行跳过 `P2-01`/`P2-03` 先写业务 handler；RLS 和事务边界必须先经数据库集成测试。

## 11. 测试矩阵

### 11.1 单元测试

- 用户名规范化、密码策略和通用认证错误。
- Argon2id 哈希/校验及参数版本。
- Role → Permission 映射；未知角色默认拒绝。
- Session 绝对/空闲过期、撤销、轮换状态机。
- CSRF token 绑定、过期和常量时间校验。
- `WorkspaceContext` 不能从任意 UUID/请求 DTO 隐式构造。
- 审计脱敏：password、Cookie、token、bootstrap token 不可序列化进入事件。

### 11.2 PostgreSQL 集成测试

- 新库按顺序应用 `202607240001` 和 Phase 2 迁移。
- 迁移角色可迁移；运行时角色为 `NOSUPERUSER/NOBYPASSRLS` 且不是表所有者。
- 正确 bootstrap 创建且只创建一组 user/Workspace/Owner/role/session/audit。
- 错误 token、强制中途失败均为零持久化变更。
- 16 个以上并发 bootstrap 请求只有一个成功；完成后即使重试仍永久关闭。
- 两个测试用户各有一个个人 Workspace，服务端解析结果唯一。
- Repository 显式 workspace 条件阻止跨 Workspace UUID/BIGINT。
- 故意省略 Repository 条件的测试 SQL 仍被 RLS 隔离。
- `WITH CHECK` 阻止向其他 Workspace 写入。
- `SET LOCAL` 在 commit/rollback 后消失；连接池复用无 Workspace 状态泄漏。
- 未设置 Workspace 的范围查询返回零行或稳定错误，不得返回全表。
- 系统 `admin` 也不能读取另一个 Workspace 的审计/业务探针数据。

### 11.3 API 集成与契约测试

- Bootstrap 成功、错误 token、已关闭、并发冲突。
- 正确/错误用户名密码；错误响应不枚举账户。
- 登录轮换 Cookie；伪造、过期、撤销 Cookie 返回 `401`。
- 缺失/错误 CSRF、错误 Origin/Referer 对副作用请求返回 `403`。
- 当前用户、Workspace 和 Session 响应不包含 token hash、password hash、CSRF secret。
- 本人撤销与管理员撤销成功；analyst/viewer 撤销他人 Session 被拒绝。
- 请求中注入 `workspace_id` 不能改变上下文。
- 跨 Workspace 资源标识返回不可见语义。
- OpenAPI 与实际 Cookie、header、状态码、错误 envelope 一致。
- live/ready/version 继续通过。

### 11.4 前端测试

- 未认证路由、初始化页、登录成功/失败、退出。
- `auth/me` 启动恢复和 `401` 清理。
- 所有副作用请求统一附加 CSRF header。
- Bootstrap token 不写入 localStorage、sessionStorage、Pinia 持久化或错误日志。
- 不展示 Workspace 切换、共享、邀请或业务模块入口。
- 前端角色显示不替代服务端拒绝。

## 12. 验收标准

Phase 2 只有全部满足才可标记 PASS：

1. 首次初始化满足 `DEC-026`：token 校验、单事务、管理员 + 个人 Workspace + Owner、永久关闭、并发唯一成功。
2. 密码只以 Argon2id 哈希保存；Session/CSRF/bootstrap 明文秘密不进入数据库、日志、审计、OpenAPI 示例或响应。
3. 登录、退出、轮换、过期、撤销和 CSRF/Origin 验证均有自动测试。
4. 每个用户只能解析到一个个人 Workspace；客户端不能指定、切换或猜测 Workspace。
5. 应用层权限与 Workspace 隔离生效；`admin` 不能仅凭角色读取其他 Workspace 业务数据。
6. 第一张 Workspace 范围表从创建时启用并强制 RLS。
7. 运行时 API/Worker 数据库角色不是超级用户、无 `BYPASSRLS`、不是表所有者。
8. 每个 Workspace Repository 同时具有显式 `workspace_id` 条件和事务级 RLS 上下文。
9. 跨 Workspace 读取、写入、主键枚举、遗漏 Repository 条件、连接池状态泄漏测试全部被阻止。
10. 身份前端可完成初始化、登录、查看当前 Workspace、退出和 Session 撤销，不包含后续业务入口。
11. OpenAPI、部署说明和测试证据与实现一致。
12. Phase 1 live/ready/version、Worker 生命周期、前端构建和 Nginx 代理无回归。
13. Evaluator 最终 PASS，且无未关闭 BLOCKER/HIGH。

## 13. 验收命令

具体测试文件名可由 Generator 按模块组织，但必须提供等价、可重复的命令：

```powershell
git status --short
git diff --check

Set-Location rust
cargo +stable fmt --check
cargo +stable clippy --workspace --all-targets -- -D warnings
cargo +stable test --workspace
sqlx migrate info
sqlx migrate run

Set-Location ..
pnpm install --frozen-lockfile
pnpm lint
pnpm test
pnpm build

docker compose config
docker compose --profile dev up -d --build
```

数据库证据至少包含以下只读检查：

```sql
select version, description from schema_versions order by version;
select current_user, rolsuper, rolbypassrls
from pg_roles
where rolname = current_user;
select c.relname, c.relrowsecurity, c.relforcerowsecurity
from pg_class c
where c.relname in ('audit_logs');
```

HTTP 验收使用临时 Cookie jar 和环境变量注入测试秘密；命令、终端录屏和证据不得打印 bootstrap token、密码或 Cookie。至少验证：

- `/api/v1/health/live`
- `/api/v1/health/ready`
- `/api/v1/version`
- `/api/v1/auth/bootstrap`
- `/api/v1/auth/login`
- `/api/v1/auth/me`
- `/api/v1/auth/csrf`
- `/api/v1/workspace`
- `/api/v1/sessions`
- `/api/v1/auth/logout`

## 14. futures VPS 部署核验

部署必须从本地 Git 已确认提交派生，不得在 VPS 手工编辑业务源码。

顺序：

1. 记录部署前 Git SHA、容器、镜像、健康、迁移和磁盘状态。
2. 对 PostgreSQL 做可恢复备份；备份中不得包含 bootstrap token、Cookie 或主密钥。
3. 以受控迁移角色应用 Phase 2 migration，随后移除应用容器对迁移凭据的访问。
4. API 与 Worker 使用非超级用户、`NOBYPASSRLS` 运行时角色启动。
5. 检查 PostgreSQL/API healthy，Worker/Frontend/Nginx running。
6. 检查 live、ready、version、首页、Nginx 代理和安全响应头。
7. 在一次性隔离测试数据库/临时 Compose project 中完成 bootstrap 并发与 RLS 破坏性测试。
8. 对持久 `futures` 数据库的首次 bootstrap 属于用户控制操作；没有用户明确授权和安全提供凭据时，不得擅自消耗一次性初始化入口。未完成真实 bootstrap/login smoke 时不得宣称 Phase 2 最终 PASS。
9. 扫描应用日志和审计，确认无密码、bootstrap token、Cookie、CSRF secret、数据库 URL 明文或主密钥。
10. 对服务执行 restart、stop/start 和 `up -d --no-build`，确认 Session/迁移与 ready 行为稳定。
11. 记录部署版本；`git_sha=local` 可继续作为已知 LOW，但必须明确，不能伪装为真实提交。

当前 HTTP 8088 仅可用于非生产验证。生产 Cookie/TLS 验收必须在 TLS 入口完成。

## 15. 回滚策略

- Git：每个逻辑任务独立提交，应用回退优先 `git revert` 或部署前一已知良好镜像。
- 部署：保留 Phase 1 可启动包、Compose 配置和部署前数据库备份。
- 数据库：优先前滚修复。Phase 2 表一旦包含用户、Workspace、Session 或审计数据，不得自动 drop 或用 down migration 静默删除。
- 若迁移后、首次 bootstrap 前失败：可回退 Phase 1 应用；新增表保持休眠，待修复迁移处理。
- 若 bootstrap 后失败：撤销所有 Session，回退应用，并从一致备份恢复或发布兼容的前滚修复；不得只删除首个用户来“重新打开” bootstrap。
- RLS/grant 失败视为阻断：不得通过临时授予 `SUPERUSER/BYPASSRLS` 恢复服务。应停止写入，恢复前一安全版本或修复权限迁移。
- Cookie/CSRF 回滚必须清除/撤销不兼容 Session，避免旧 token 在新旧版本间继续有效。

## 16. 风险与待确认事项

以下事项不授权 Generator 自行决定：

| 编号 | 待确认 | 建议/影响 |
| --- | --- | --- |
| `P2-CONF-001` | 已确认：绝对有效期 7 天、空闲有效期 4 小时、每用户最多 5 个并发会话、超限撤销最旧会话；敏感操作要求重新认证 | Generator 必须配置化并固化测试 |
| `P2-CONF-002` | 已确认：HTTP 8088 仅用于临时开发和验收；非生产可 `Secure=false`；生产必须 HTTPS + `Secure=true`，不满足时认证服务拒绝启动 | VPS HTTP smoke 只能作为非生产证据 |
| `P2-CONF-003` | 已确认：Argon2id memory=64MiB、iterations=3、parallelism=1；密码 15-128 字符，允许 Unicode/空格/短语，拒绝常见或已泄漏密码，不做定期强制修改 | 必须本地和 VPS 基准，参数集中配置并支持登录时重哈希 |
| `P2-CONF-004` | 已确认：操作者为系统所有者本人；token 在 VPS 本地生成，位于 `/etc/futures-platform/secrets/bootstrap-token`，`root:root` `0400`，通过 Docker Secret 或只读文件挂载，成功后失效并删除 | 不得写入 Git、数据库、镜像、日志或普通 `.env` |

这些事项不允许扩张到共享 Workspace、用户邀请或完整用户管理。graphify 的 34 条跨批次语义悬空边作为非阻塞工具限制记录；最终实现仍以原始文档、Git、测试和 VPS 实证为准。

## 17. 对未来 Generator 的实现边界

Generator 获得明确授权后仍必须遵守：

- 只实现本文件第 4 节范围；不得创建行情、导入、套利、成交、席位、采集、AI 等表、接口或页面。
- 不修改已确认业务口径；发现冲突先回报 Planner，不得在代码中自行选择。
- 不修改原始 Word 方案。
- 不使用超级用户连接作为 API/Worker 常态运行方式或 RLS 测试证据。
- 不用客户端 `workspace_id`、前端路由守卫或内存过滤替代服务端隔离。
- 不在日志、测试快照、fixture、OpenAPI、提交信息或最终回复写入真实秘密。
- 不手工修改 VPS 源码；部署包必须来自本地 Git。
- 不宣称测试、迁移、RLS 或 VPS 状态通过，除非实际执行并保留证据。
- 完成实现后必须交给 Evaluator；BLOCKER/HIGH 修复并复核 PASS 前不得提交阶段完成结论。
