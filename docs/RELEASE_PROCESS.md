# 发布流程

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
- 本地测试已执行并记录。
- 文档状态已更新。
- `PLANS.md` 反映真实阶段状态。

## 云端验证与镜像

CI 在 pull request、`main` push 和 `phase/**` push 上执行 Rust
fmt/clippy/test、pnpm install/lint/test/build、Compose config 和三个 Dockerfile
的 `linux/amd64` 构建检查。云端任务不连接 `futures` VPS，也不读取生产数据。

容器发布工作流只在手工触发、`phase-*-pass-*` 标签和 `v*` 正式版本标签触发。
它使用 GitHub Actions 自动提供的 `GITHUB_TOKEN` 发布 API、Worker、前端镜像，
并输出 digest。每个镜像至少包含不可变的 `sha-<完整 Git SHA>` 标签；发布标签
可以作为人类可读别名，但部署清单必须记录 SHA 标签或 digest。

## Phase 3 收口方案

Phase 3D 完成实现、完整本地门禁、`futures` VPS 最终 E2E/RLS/持久化验收和独立
Evaluator PASS 后，才允许：

1. 将 `phase/03-import-foundation` 合并到 `main`，禁止 force push。
2. 在最终 PASS 提交创建带注释的 `phase-3-pass-YYYYMMDD` 标签。
3. 如需正式语义化版本，再从同一已验收提交创建 `v*` 标签。
4. 等待标签触发的 GHCR 工作流成功，记录三个镜像 digest。
5. 经用户确认并提供只读 GHCR 拉取凭据后，再切换 `futures` VPS。

Phase 3 未完成前，`main` 保持当前稳定基线；不得提前合并开发分支。
