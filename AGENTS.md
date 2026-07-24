# 开发规则

- 项目阶段以 `PLANS.md` 为准；只执行当前阶段授权的工作，不提前实现后续业务模块。
- 产品范围以 `docs/PRODUCT_REQUIREMENTS.md` 为准；未确认事项只记录到 `docs/OPEN_QUESTIONS.md`，不得自行扩展需求。
- 文档使用中文；代码标识符、API 字段和数据库字段使用英文与 `snake_case`。
- 领域计算必须确定、可复核、可追溯；AI 默认只读，不参与确定性数值计算。
- 原始文件、导入批次、计算公式和分类规则必须版本化；批量写入必须可审计、可回滚。
- 本地 Git 仓库是唯一源码源头；VPS 上不得手工编辑业务源码。
- 任何文档、日志、提交和回复都不得包含密码、API Key、Cookie、Token、主密钥或数据库明文凭据。
- 上下文不足或阶段完成需要交接时，按 `docs/handoffs/README.md` 创建不可覆盖的交接文档，并更新 `docs/handoffs/LATEST.md`。

## 文档索引

- 总览与边界：`README.md`、`docs/PRODUCT_REQUIREMENTS.md`
- 架构与模块：`docs/ARCHITECTURE.md`、`docs/MODULE_DESIGN.md`
- 数据与接口：`docs/DATABASE_DESIGN.md`、`docs/API_DESIGN.md`
- 专项设计：`docs/SECURITY_DESIGN.md`、`docs/IMPORT_DESIGN.md`、`docs/AI_DESIGN.md`
- 计划与验收：`PLANS.md`、`docs/DEVELOPMENT_PLAN.md`、`docs/phases/PHASE_01_FOUNDATION.md`、`docs/ACCEPTANCE_CRITERIA.md`
- 审查与交接：`docs/reviews/`、`docs/handoffs/`
- 待确认事项：`docs/OPEN_QUESTIONS.md`
