# 项目环境

## 本地与 GitHub

- 本地项目路径：`C:\Users\a6366\ssp\Futures analysis website`
- GitHub 用户：`junjuntian`
- GitHub 私有仓库：`https://github.com/junjuntian/Futures-analysis-website`
- Git origin：`git@github-codex:junjuntian/Futures-analysis-website.git`
- 本地 Git 仓库是唯一源码源头。

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
