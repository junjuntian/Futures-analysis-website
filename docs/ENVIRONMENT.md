# 项目环境

## 本地与 GitHub

- 本地项目路径：`C:\Users\a6366\ssp\Futures analysis website`
- GitHub 用户：`junjuntian`
- GitHub 私有仓库：`https://github.com/junjuntian/Futures-analysis-website`
- Git origin：`git@github-codex:junjuntian/Futures-analysis-website.git`
- 本地 Git 仓库是唯一源码源头。

## 环境职责

- 本地 Git：唯一源码源头。所有源码、迁移、Compose、发布文档和标签都从本地
  受控修改并提交；不得以 VPS、Codex Cloud 工作区或 GitHub 网页编辑结果作为
  独立源码源头。
- GitHub 私有仓库：受控远端、协作与发布触发入口。只接收本地 Git 推送，不保存
  生产数据库、对象文件或秘密。
- GitHub Actions：权威 CI 和容器发布环境。使用 GitHub-hosted Ubuntu runner
  执行 Rust/pnpm 门禁、Compose/Dockerfile 检查，构建 `linux/amd64` API、Worker、
  前端镜像并发布到 GHCR。
- Codex Cloud：辅助编译、测试、静态分析和独立审查环境。其结果必须回到本地 Git
  核验；除非通过已批准的 GitHub Actions 发布流程，不得作为生产镜像的旁路来源。
- `futures` VPS：生产候选的最终验收环境。只拉取已发布镜像、备份和迁移真实
  PostgreSQL、验证 RLS/文件持久化、启动服务并执行最终 E2E；禁止直接修改源码，
  不再承担常规 Rust 或前端编译。

GitHub Actions 只使用自动提供的 `GITHUB_TOKEN` 向 GHCR 发布镜像，权限限定为
`contents: read` 和 `packages: write`。缓存仅包含 Cargo/pnpm/BuildKit 构建依赖，
不得缓存 `.env`、秘密目录、Cookie、storage state、数据库文件或对象存储数据。

## 唯一标准发布链路

```text
本地开发与验证
→ 推送 GitHub 私有仓库
→ Codex Cloud / GitHub Actions 编译测试
→ GitHub Actions 构建 linux/amd64 镜像
→ GitHub Actions 推送 GHCR
→ futures VPS 备份数据库、docker pull、数据库迁移和 E2E 验收
```

不得跳过 GitHub 私有仓库和云端门禁，直接把本地源码复制到 VPS 编译。GitHub
Actions 或 Codex Cloud 的通过结果不能替代 VPS 上的真实数据库迁移、RLS、
对象持久化、重启恢复和 E2E 验收。

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
- 部署引用必须使用 `sha-<完整 Git SHA>` 或 `sha256:<digest>`，不得只使用
  `latest`；每次发布记录三个镜像的完整 digest 和对应 Git SHA。
- 当前尚未向 `futures` VPS 提供 GHCR 拉取凭据，也未切换到镜像拉取部署模式。
- 只有在 GitHub Actions 实际构建成功且用户后续提供只读拉取凭据后，才能切换。

## 秘密边界

- 私钥、GHCR 拉取凭据、数据库凭据、bootstrap token、Cookie、幂等 pepper 和
  主密钥不得进入 Git、镜像层、构建参数、构建日志或普通 `.env` 文件。
- 生产秘密使用 `root:root`、`0400` 的主机文件或等价只读秘密挂载；数据库备份
  与主密钥恢复副本必须分开保存。
- 云端构建不连接生产数据库、对象存储或 `futures` VPS。
