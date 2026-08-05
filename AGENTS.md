# 开发规则

- 项目阶段以 `PLANS.md` 为准；只执行当前阶段授权的工作，不提前实现后续业务模块。
- 产品范围以 `docs/PRODUCT_REQUIREMENTS.md` 为准；未确认事项只记录到 `docs/OPEN_QUESTIONS.md`，不得自行扩展需求。
- 文档使用中文；代码标识符、API 字段和数据库字段使用英文与 `snake_case`。
- 领域计算必须确定、可复核、可追溯；AI 默认只读，不参与确定性数值计算。
- 原始文件、导入批次、计算公式和分类规则必须版本化；批量写入必须可审计、可回滚。
- 本地 Git 仓库是唯一源码源头；VPS 上不得手工编辑业务源码。
- 唯一标准发布链路为：本地开发 → GitHub 私有仓库 → Codex Cloud / GitHub Actions 编译测试 → 构建并发布 `linux/amd64` GHCR 镜像 → `futures` VPS 拉取镜像、执行迁移和最终 E2E。
- `futures` VPS 的 4 GiB 资源允许仓库级 self-hosted runner 承接受限 CI 与镜像构建；Cargo、Node 和 BuildKit 必须遵守工作流资源护栏，总编译峰值不得超过 2.5 GiB。VPS 仍不得手工编译或编辑业务源码；生产镜像必须使用 SHA 标签或 digest，禁止仅依赖 `latest`。
- 生产部署前必须备份数据库；失败时按发布记录中的上一稳定镜像 digest 回滚。
- 任何文档、日志、提交和回复都不得包含密码、API Key、Cookie、Token、主密钥或数据库明文凭据。
- 密钥不得进入 Git、镜像层、构建日志或普通环境变量文件；生产秘密只通过受控只读文件或等价秘密挂载提供。
- GitHub 操作统一使用 `github-codex` SSH 别名；除非用户明确要求，不得改用 Deploy Key、HTTPS 或临时 PAT。
- Git 仓库读写继续使用 `github-codex` SSH 别名；`gh auth status`、`gh run`、`gh workflow` 等 GitHub API/Actions 命令必须以宿主身份 `HUASHAO\a6366` 执行。
- 有效的 GitHub CLI 凭据存储在 `HUASHAO\a6366` 的 Windows Credential Manager；沙箱身份 `HUASHAO\CodexSandboxOffline` 无法读取该凭据库。不得因沙箱内 `gh auth status` 失败而执行 `gh auth login`。
- 不得输出、复制或重新生成现有 OAuth Token。
- 推送前按 `docs/ENVIRONMENT.md` 检查连接；未经用户明确要求，不得修改或删除 `origin`。
- 上下文不足或阶段完成需要交接时，按 `docs/handoffs/README.md` 创建不可覆盖的交接文档，并更新 `docs/handoffs/LATEST.md`。

## Codex Cloud、GitHub Actions 与多环境职责

- Codex Cloud 可用于独立代码审查、编译和单元测试、静态分析和安全扫描，以及与本地环境无关的并行开发任务。
- 使用 Codex Cloud 或 GitHub Actions 的前提：项目已推送到 GitHub 私有仓库；不得上传生产密钥、Cookie、数据库备份或用户数据；云端结果必须回到本地 Git 核验。
- GitHub Actions 负责权威 CI、`linux/amd64` 生产镜像构建和 GHCR 发布；Codex Cloud 可辅助编译、测试和独立审查。
- `futures` VPS 同时运行标签为 `futures-vps` 的仓库级 self-hosted runner；CI/镜像构建须受 2.5 GiB 总峰值护栏约束，Deploy 仍负责拉取已验证镜像、数据库备份与迁移、真实数据库/RLS/文件持久化和最终 E2E。Actions 门禁不得替代生产实态验收。
- GHCR 工作流和只读拉取凭据验证成功前，不得切换 `futures` VPS 当前部署方式。
- 子 Agent 连续两次卡住时，Generator 可改用新的顶层 Codex 会话接管；Evaluator 可改用新的顶层 Codex 会话或 Codex Cloud 独立审查；主 Agent 不得冒充独立 Evaluator。

## 文档索引

- 总览与边界：`README.md`、`docs/PRODUCT_REQUIREMENTS.md`
- 架构与模块：`docs/ARCHITECTURE.md`、`docs/MODULE_DESIGN.md`
- 数据与接口：`docs/DATABASE_DESIGN.md`、`docs/API_DESIGN.md`
- 专项设计：`docs/SECURITY_DESIGN.md`、`docs/IMPORT_DESIGN.md`、`docs/AI_DESIGN.md`
- 计划与验收：`PLANS.md`、`docs/DEVELOPMENT_PLAN.md`、`docs/phases/PHASE_01_FOUNDATION.md`、`docs/ACCEPTANCE_CRITERIA.md`
- 审查与交接：`docs/reviews/`、`docs/handoffs/`
- 环境与 GitHub 接入：`docs/ENVIRONMENT.md`
- 发布与部署：`docs/RELEASE_PROCESS.md`、`docs/DEPLOYMENT.md`
- 待确认事项：`docs/OPEN_QUESTIONS.md`
