# 项目环境

## 本地与 GitHub

- 本地项目路径：`C:\Users\a6366\ssp\Futures analysis website`
- GitHub 用户：`junjuntian`
- GitHub 私有仓库：`https://github.com/junjuntian/Futures-analysis-website`
- Git origin：`git@github-codex:junjuntian/Futures-analysis-website.git`
- 本地 Git 仓库是唯一源码源头。

## 环境职责

- 本地 Git：唯一源码源头，负责受控修改、提交、标签和发布输入。
- GitHub Actions / Codex Cloud：使用 GitHub-hosted Ubuntu runner 执行编译、
  单元测试、静态检查、无生产数据的 Compose/Docker 构建和辅助审查；不得
  连接 `futures` VPS，不得使用生产数据库、生产对象或生产秘密。
- `futures` VPS：唯一的迁移、真实数据库、RLS、文件持久化、恢复演练和最终
  E2E/部署验收环境；不得在 VPS 手工修改业务源码。

GitHub Actions 只使用自动提供的 `GITHUB_TOKEN` 向 GHCR 发布镜像，权限限定为
`contents: read` 和 `packages: write`。缓存仅包含 Cargo/pnpm/BuildKit 构建依赖，
不得缓存 `.env`、秘密目录、Cookie、storage state、数据库文件或对象存储数据。

## SSH 接入

- SSH Host 别名：`github-codex`
- SSH 配置文件：`C:\Users\a6366\.ssh\config`
- 私钥路径：`C:\Users\a6366\.ssh\github_codex_ed25519`
- 已验证命令：`ssh -T git@github-codex`
- 已验证结果：GitHub 返回 `Hi junjuntian!`，认证成功。

私钥内容绝不得写入项目、Git、聊天、日志或文档。

## 长期操作规则

- 本项目所有 GitHub 操作统一使用 `github-codex` SSH 别名。
- 不改用 Deploy Key、HTTPS 或临时 PAT，除非用户明确要求。
- 新会话执行 push 前必须先运行：

  ```powershell
  git remote -v
  ssh -T git@github-codex
  ```

- 未经用户明确要求，不得修改或删除 `origin`。

## GHCR 接入状态

- 镜像前缀：`ghcr.io/junjuntian/futures-analysis-website`
- API、Worker、前端分别使用 `-api`、`-worker`、`-frontend` 后缀。
- 部署引用使用 `sha-<完整 Git SHA>` 或 digest，不依赖 `latest`。
- 当前尚未向 `futures` VPS 提供 GHCR 拉取凭据，也未切换到镜像拉取部署模式。
- 只有在 GitHub Actions 实际构建成功且用户后续提供只读拉取凭据后，才能切换。
