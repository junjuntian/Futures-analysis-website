# Phase 3B 独立评审报告

评审日期：2026-07-25  
评审范围：仅 Phase 3B（解析、识别、预览、字段映射、映射模板、解析错误展示及 OpenAPI multipart 契约修复）  
评审方式：全新顶层 Codex 会话只读复评  
最终结论：**PASS**

## 1. 最终结论

- BLOCKER：0
- HIGH：0
- 未发现 Phase 3C/3D 越界实现。
- 独立 Evaluator 执行了 Git 状态、分支、最近提交、`git diff --check` 和完整差异审阅；Evaluator 本身未运行会写数据库或产生构建物的测试。
- 本地编译/测试和 `futures` VPS Docker、数据库与 E2E 证据由实现会话完成，不以独立只读审查替代真实部署验收。

## 2. 已确认的不变量

### 映射身份与预览一致性

- `202607250007` 的最终触发器为 `BEFORE INSERT OR UPDATE`，覆盖所有更新列。
- 更新映射时禁止改变 `workspace_id` 或 `import_batch_id`。
- `save_mapping` 先锁批次、再锁映射；预览状态下在同一事务内删除 staging/errors、回退到 `mapped`，再写入映射。
- 后续 UPSERT 或数据库触发器失败时整个事务回滚，不会提交部分预览失效状态。

### 模板版本冻结

- `dataset_type` 已固化到 `import_template_versions`，完成回填并设置为 NOT NULL。
- 新版本写入、读取和绑定验证均使用版本行自身的 `dataset_type`。
- 父模板已有版本后，数据库触发器禁止修改其 `dataset_type`。
- 模板版本更新、映射改绑、置空、字段/转换修改和跨 Workspace 绑定均由数据库约束拒绝。

### 迁移兼容与锁序

- `202607250006` 修复了 `202607250005` 的 `TG_OP` 大小写问题。
- `202607250007` 完整替换最终函数并重建全 UPDATE 触发器，对全新库以及已执行 005/006 的部署库最终状态一致。
- 模板版本插入和父模板更新均锁定父模板行；未发现锁序反转。

### 范围边界

- 路由仅包含 inspect、mapping、preview、errors、templates/datasets。
- 未发现 confirm、events、rollback、cancel、jobs、SSE、正式确认入库或 `import_row_changes`。

## 3. 非阻断 MEDIUM

1. 前端每次 inspect 都会覆盖当前预览并清空 errors；即使服务端返回 `preview_invalidated=false`，已持久化的预览错误仍会从前端状态消失。
2. errors API 固定 `LIMIT 500`，没有分页参数或 continuation token。
3. 数据库测试尚未直接覆盖“预览失效后映射写入失败时 staging/errors/status 全部回滚”。
4. 双连接测试覆盖并发模板绑定竞争，但尚未覆盖模板 `dataset_type` 冻结竞争。
5. 两份数据库测试脚本的迁移前置注释仍止于 006，实际测试依赖 007。

以上项目不构成 Phase 3B 的 BLOCKER/HIGH，不回退最终 PASS；后续应显式排期，避免在未授权情况下扩大到 Phase 3C/3D。

## 4. 最终证据

- 实现提交：`150194c feat: complete phase 3b import parsing preview mapping`。
- 本地：Rust fmt/test/clippy、前端 lint/test/build、`git diff --check` 通过。
- `futures` VPS：最新 API 镜像构建与部署通过，API/PostgreSQL healthy，`PHASE3B_E2E_PASS`。
- 数据库：迁移 `202607250003` 至 `202607250007` 已执行；数据库不变量测试和双连接并发首次绑定测试通过。
- 容量：最终根分区使用率 45%；PostgreSQL 数据卷、对象存储卷和当前运行镜像保留。
