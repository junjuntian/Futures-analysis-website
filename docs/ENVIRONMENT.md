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
- GitHub Actions：权威 CI 和容器发布环境。仓库级 self-hosted runner（标签
  `futures-vps`）执行 Rust/pnpm/Python 门禁、Compose/Dockerfile 检查，构建
  `linux/amd64` API、Worker、前端、Collector 镜像并发布到 GHCR；Cargo、Node、
  PostgreSQL 测试服务与 BuildKit 均设置资源护栏，总编译峰值不超过 2.5 GiB。
- Codex Cloud：辅助编译、测试、静态分析和独立审查环境。其结果必须回到本地 Git
  核验；除非通过已批准的 GitHub Actions 发布流程，不得作为生产镜像的旁路来源。
- `futures` VPS：4 GiB 生产候选验收环境，并承载上述仓库级 self-hosted runner。
  Actions 可在资源护栏内执行 CI 与镜像构建；部署路径只拉取已发布镜像、备份和
  迁移真实 PostgreSQL、验证 RLS/文件持久化并执行最终 E2E。禁止直接修改源码，
  禁止绕过 Actions 手工编译或构建生产镜像。

GitHub Actions 只使用自动提供的 `GITHUB_TOKEN` 向 GHCR 发布镜像，权限限定为
`contents: read` 和 `packages: write`。缓存仅包含 Cargo/pnpm/BuildKit 构建依赖，
不得缓存 `.env`、秘密目录、Cookie、storage state、数据库文件或对象存储数据。

## 唯一标准发布链路

```text
本地开发与验证
→ 推送 GitHub 私有仓库
→ GitHub Actions（`futures-vps` self-hosted runner）编译测试
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

## GitHub CLI 与 Windows 凭据

- GitHub CLI 的现有凭据有效，存储于 `HUASHAO\a6366` 的 Windows Credential
  Manager；不得输出、复制或重新生成现有 OAuth Token。
- Codex 沙箱命令进程使用 `HUASHAO\CodexSandboxOffline` 身份，无法读取
  `HUASHAO\a6366` 的 Windows Credential Manager。沙箱内 `gh auth status`
  失败不表示现有凭据失效，禁止因此执行 `gh auth login`。
- `gh auth status`、`gh run`、`gh workflow` 以及其他 GitHub API/Actions 操作
  必须以宿主身份 `HUASHAO\a6366` 执行并复用现有凭据。
- Git 仓库的读取、拉取和推送继续通过 `github-codex` SSH 别名完成；GitHub CLI
  凭据仅用于 GitHub API/Actions，不得借此把仓库远端改为 HTTPS。

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
- API、前端分别使用 `-api`、`-frontend` 后缀。
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
