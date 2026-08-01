# 发布流程

## 唯一标准流程

```text
本地开发
→ 推送 GitHub 私有仓库
→ Codex Cloud / GitHub Actions 编译测试
→ 构建 linux/amd64 Docker 镜像
→ 推送 GHCR
→ futures VPS docker pull、数据库迁移和 E2E 验收
```

除紧急恢复且经用户明确授权外，不允许绕过此流程。`futures` VPS 禁止直接修改
源码，不再承担常规 Rust、pnpm 或前端生产构建。

## 分支

- `main`：稳定基线。
- `phase/01-foundation`：Phase 1 工程基础。
- 当前开发分支：`phase/03-import-foundation`。
- 阶段开发使用 `phase/**`；Phase 通过最终 Evaluator 验收前不得合并到 `main`。

## 提交

使用 Conventional Commits，例如：

- `docs: finalize architecture decisions`
- `chore: initialize project foundation`
- `feat: add API and worker foundations`
- `feat: add frontend foundation`
- `chore: add container and nginx deployment`
- `ci: add project validation workflow`

## 发布前检查

- 工作区无意外文件。
- 使用 gitleaks 或等效方法扫描已跟踪文件和完整 Git 历史，不包含真实秘密。
- 本地 Rust fmt/clippy/test 和前端 install/lint/test/build 已执行并记录。
- 文档状态已更新。
- `PLANS.md` 反映真实阶段状态。
- 生产秘密未进入 Git、镜像层、构建日志、构建参数或普通 `.env`。

## 1. 测试

1. 本地完成与阶段相称的测试并提交。
2. 推送 GitHub 私有仓库。
3. GitHub Actions 在 pull request、`main` push 和 `phase/**` push 上执行 Rust
   fmt/clippy/test、pnpm install/lint/test/build、Compose config 和三个
   Dockerfile 构建检查。
4. Codex Cloud 可辅助编译、测试和独立审查，但结果必须回到本地 Git 核验。
5. 云端不得连接 `futures` VPS、生产数据库或生产对象，不得读取生产秘密。

## 2. 构建

1. 只从已推送且测试通过的确定 Git 提交构建。
2. GitHub Actions 在 GitHub-hosted Ubuntu runner 构建 API、Worker 和前端的
   `linux/amd64` 镜像。
3. API 编译期注入真实 `GIT_SHA`；所有镜像写入相同 revision label。
4. 构建缓存不得包含 `.env`、秘密目录、数据库、对象文件、Cookie 或 storage state。
5. 不把生产密钥作为 Docker build arg、ENV 或镜像层文件。

## 3. 发布 GHCR

1. 容器工作流仅由手工触发、`phase-*-pass-*` 或 `v*` 标签触发。
2. 只使用 GitHub Actions 自动提供的 `GITHUB_TOKEN`，权限限定为
   `contents: read`、`packages: write`。
3. 每个镜像必须产生 `sha-<完整 Git SHA>` 标签和完整 digest；人类可读标签只能
   作为别名，`latest` 不能作为唯一或实际部署依据。
4. 发布记录必须包含 Git SHA、工作流运行、API/Worker/前端 digest 和构建结果。

## 4. 生产部署与验收

1. 确认 GHCR 工作流成功且 VPS 的只读拉取凭据已经验证。
2. 部署前备份生产数据库，记录备份校验值和上一稳定镜像 digest。
3. 在 VPS 执行 Compose config 和 `docker pull`；不得复制源码或现场编译。
4. 使用受控迁移身份按顺序执行数据库迁移，核验迁移记录。
5. 使用已拉取镜像启动服务。
6. 在 VPS 验证真实数据库、RLS、文件持久化、重启恢复、版本接口和完整 E2E。
7. 全部通过后，才将候选 digest 登记为新的稳定版本。

GitHub Actions 或 Codex Cloud 的测试结果不能替代第 6 步。

## 5. 回滚

1. 每次生产发布前保存上一稳定 API、Worker、前端镜像的完整 digest。
2. 应用、健康检查或 E2E 失败时停止发布，把部署清单恢复到上一稳定 digest，
   重新 pull/up 并复跑 VPS 验收。
3. 如果迁移已经执行，先判断上一镜像是否兼容当前 schema；不兼容时使用已批准的
   前滚修复，或从部署前数据库备份恢复后再启动上一稳定 digest。
4. 禁止在 VPS 修改源码、禁用 RLS/约束或手工改数据来规避失败。
5. 回滚结果、所用 digest、数据库恢复点和验证证据必须写入发布记录。

## 当前切换状态

本标准自本提交起成为唯一后续发布流程，但当前不得立即切换 `futures` VPS。
只有 GHCR 工作流实际成功、三个镜像可拉取、只读凭据验证通过并经用户确认后，
才执行首次 GHCR 部署。

## Phase 3 收口方案

Phase 3D 完成实现、完整本地门禁、`futures` VPS 最终 E2E/RLS/持久化验收和独立
Evaluator PASS 后，才允许：

1. 将 `phase/03-import-foundation` 合并到 `main`，禁止 force push。
2. 在最终 PASS 提交创建带注释的 `phase-3-pass-YYYYMMDD` 标签。
3. 如需正式语义化版本，再从同一已验收提交创建 `v*` 标签。
4. 等待标签触发的 GHCR 工作流成功，记录三个镜像 digest。
5. 经用户确认并提供只读 GHCR 拉取凭据后，再切换 `futures` VPS。

Phase 3 未完成前，`main` 保持当前稳定基线；不得提前合并开发分支。
