# 发布流程

## 分支

- `main`：稳定基线。
- `phase/01-foundation`：Phase 1 工程基础。
- 后续功能使用 `feature/*`。

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
- 不包含秘密。
- 本地测试已执行并记录。
- 文档状态已更新。
- `PLANS.md` 反映真实阶段状态。
