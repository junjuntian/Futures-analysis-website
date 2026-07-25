# 开发规则

- 项目阶段以 `PLANS.md` 为准；只执行当前阶段授权的工作，不提前实现后续业务模块。
- 产品范围以 `docs/PRODUCT_REQUIREMENTS.md` 为准；未确认事项只记录到 `docs/OPEN_QUESTIONS.md`，不得自行扩展需求。
- 文档使用中文；代码标识符、API 字段和数据库字段使用英文与 `snake_case`。
- 领域计算必须确定、可复核、可追溯；AI 默认只读，不参与确定性数值计算。
- 原始文件、导入批次、计算公式和分类规则必须版本化；批量写入必须可审计、可回滚。
- 本地 Git 仓库是唯一源码源头；VPS 上不得手工编辑业务源码。
- 任何文档、日志、提交和回复都不得包含密码、API Key、Cookie、Token、主密钥或数据库明文凭据。
- 上下文不足或阶段完成需要交接时，按 `docs/handoffs/README.md` 创建不可覆盖的交接文档，并更新 `docs/handoffs/LATEST.md`。

## Codex Cloud 与多环境职责

- Codex Cloud 可用于独立代码审查、编译和单元测试、静态分析和安全扫描，以及与本地环境无关的并行开发任务。
- 使用 Codex Cloud 的前提：项目已推送到 GitHub 私有仓库；不得上传生产密钥、Cookie、数据库备份或用户数据；云端结果必须回到本地 Git 核验；云端测试不能代替 `futures` VPS 的 Docker、数据库、RLS、文件持久化和真实部署验收。
- 环境职责固定为：本地 Git 是唯一源码源头；Codex Cloud 只作为辅助开发、测试和独立审查环境；`futures` VPS 是最终构建、迁移、E2E 和部署验收环境。
- 子 Agent 连续两次卡住时，Generator 可改用新的顶层 Codex 会话接管；Evaluator 可改用新的顶层 Codex 会话或 Codex Cloud 独立审查；主 Agent 不得冒充独立 Evaluator。

## 文档索引

- 总览与边界：`README.md`、`docs/PRODUCT_REQUIREMENTS.md`
- 架构与模块：`docs/ARCHITECTURE.md`、`docs/MODULE_DESIGN.md`
- 数据与接口：`docs/DATABASE_DESIGN.md`、`docs/API_DESIGN.md`
- 专项设计：`docs/SECURITY_DESIGN.md`、`docs/IMPORT_DESIGN.md`、`docs/AI_DESIGN.md`
- 计划与验收：`PLANS.md`、`docs/DEVELOPMENT_PLAN.md`、`docs/phases/PHASE_01_FOUNDATION.md`、`docs/ACCEPTANCE_CRITERIA.md`
- 审查与交接：`docs/reviews/`、`docs/handoffs/`
- 待确认事项：`docs/OPEN_QUESTIONS.md`
