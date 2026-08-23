# 开发规则

## 文档维护规则（2026-08-18 运营者拍板,每次改动收口时强制执行）

三个文件职责不交叉,收口时各归各位:

- **状态变化 → `docs/handoffs/LATEST.md`,覆盖不追加。** 它只描述"现在是什么
  样子"(生产 SHA、现行规则速查、最近约三批变更、待办),永远保持一屏左右。
  历史不复制到任何地方——git 历史即归档。
- **决策与勘误 → `docs/DECISIONS.md`,追加不改写。** 旧条目是历史记录;口径被
  修订时新立条目并在文首导航里更新最短路径。
- **教训 → `docs/PITFALLS.md`,追加进对应分类。** 症状→根因→正确做法,三句话
  一条。教训不进 LATEST、不散落在提交信息里了事。
  **例外一处**:`research/` 下一次性分析脚本的坑写进 `research/PITFALLS.md`
  ——那类脚本不进 CI、没有测试兜底,错了只会安静地给一个看着合理的数,
  与工程教训不是一回事。**动 `research/` 下任何脚本前先读它。**

新会话接手固定三步:LATEST → PITFALLS → 按需查 DECISIONS。发现文档表述与
生产实态不符时,当场修正并注明核对日期,不许带病传给下一个会话。

## 写中文文档的硬规则（2026-08-21 立,当天被咬四次）

**禁止用 `python -c "..."` 或 shell heredoc 写含反引号的中文文档。**
一律：Write 工具落一个 `.py` 到 scratchpad，再 `python 那个文件`。

为什么是硬规则而不是「注意一下」：这条已经在 `docs/PITFALLS.md`
五·批量编辑里记了很久，2026-08-21 当天我仍然踩了四次 ——
第四次把整段 `git diff` 的输出塞进了 `docs/DECISIONS.md`。
**记在教训清单里挡不住它**，因为出事的时候人正忙着想别的。

三种表现，都不会当场报错：

- 反引号被当成命令替换 → 那段文字**凭空消失**，替换成命令的输出；
- `\n` 被 heredoc 吃掉 → 变成真实换行，Python 源码语法错；
- 反斜杠成对被吞 → 正则 `\[` 变 `[`，行为静默改变。

**第二条硬规则(2026-08-23 立,DECISIONS.md 整个丢过一次)**:改文档的脚本里,
**先把要写回的完整内容算成一个变量,最后一步才 `open(path, "w").write(var)`**。
`open(p,"w").write(d + body.format(...))` 这种写法,`format` 一抛错文件就是空的,
而且 commit/push 不会报错。写完对比行数或条目数,少了就是截断,立刻 `git show 上一提交:路径` 恢复。

**写完必检两样**：`grep -c 'diff --git'` 为 0；用 Python 读回文件数一遍
`\ufffd` 替换字符为 0。终端里的中文乱码是 Git Bash 的显示问题，不是文件坏了 ——
**别靠肉眼看终端判断**。

## 部署前强制清单（不得跳过）

**每次部署前必须先跑 `ops/preflight-deploy.sh`，全绿再读 `docs/DEPLOY_PREFLIGHT.md`
的第二节（脚本查不了的那几条）。**两者都过了才允许触发 `deploy-futures.yml`。

脚本会打印出可直接执行的 dispatch 命令，digest 也由它从构建产物里取，不要手抄。

三条最容易漏、且线上不会报错的：

- **建镜像到部署完成之间不推任何提交**（包括只改文档）——推了就得整套镜像重建。
- **新迁移**要写 `schema_versions`、带 `begin;`、列进 `deploy-futures.yml` 的显式清单。
- **`deploy/` 下的新文件**要装进发布包，否则线上找不到它且多半不报错。

这份清单是 2026-08-10 连续三次部署失败之后立的，每一条都对应一次真实失败。

- 项目现状以 `docs/handoffs/LATEST.md` 与 `docs/DECISIONS.md` 为准（`PLANS.md` 是 Phase 5 前的历史计划存档）。
- 产品范围以 `docs/PRODUCT_REQUIREMENTS.md` 为准；未确认事项直接立 `DEC-xxx` 草案（原 `docs/OPEN_QUESTIONS.md` 已于 2026-08-16 删除）。
- 文档使用中文；代码标识符、API 字段和数据库字段使用英文与 `snake_case`。
- 领域计算必须确定、可复核、可追溯；AI 默认只读，不参与确定性数值计算。
- 原始文件、导入批次、计算公式和分类规则必须版本化；批量写入必须可审计、可回滚。
- 本地 Git 仓库是唯一源码源头；VPS 上不得手工编辑业务源码。
- 唯一标准发布链路为：本地开发 → GitHub 仓库（2026-08-13 起为公开仓库）→ GitHub 托管 runner 跑 CI 与 `linux/amd64` GHCR 镜像构建 → 生产机 `qh` 拉取镜像、执行迁移和最终 E2E。老机 `futures` 与自建 runner 均已于 2026-08-15 退役。生产镜像必须使用 SHA 标签或 digest，禁止仅依赖 `latest`；VPS 不得手工编译或编辑业务源码。
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
- CI 与镜像构建跑在 GitHub 托管 runner（仓库已公开，免费且互不排队）；Deploy 负责拉取已验证镜像、数据库备份与迁移、真实数据库/RLS 和最终 E2E。Actions 门禁不得替代生产实态验收。
- 子 Agent 连续两次卡住时，Generator 可改用新的顶层 Codex 会话接管；Evaluator 可改用新的顶层 Codex 会话或 Codex Cloud 独立审查；主 Agent 不得冒充独立 Evaluator。

## 文档索引(2026-08-18 核对,死链已清)

- **接手必读**：`docs/handoffs/LATEST.md`（当前状态）、`docs/PITFALLS.md`（全部实抓教训）
- 决策与口径：`docs/DECISIONS.md`（append-only，文首有现行口径导航）
- 发布与部署：`docs/RELEASE_PROCESS.md`（操作手册，含手动重算）、`docs/DEPLOY_PREFLIGHT.md`、`docs/DEPLOYMENT.md`
- 总览与边界：`README.md`、`docs/PRODUCT_REQUIREMENTS.md`
- 架构与设计（Phase 1-3 时期蓝本，现行以代码与 DECISIONS 为准）：`docs/ARCHITECTURE.md`、`docs/MODULE_DESIGN.md`、`docs/DATABASE_DESIGN.md`、`docs/API_DESIGN.md`、`docs/SECURITY_DESIGN.md`、`docs/ACCEPTANCE_CRITERIA.md`
- 专项：`docs/SMART_MONEY_DESIGN.md`（机构资金）、`docs/SEAT_AND_SPREAD_REQUIREMENTS.md`、`docs/phases/PHASE_05_SPREAD_ANALYTICS.md`（Phase 5 契约存档）
- 历史计划存档：`PLANS.md`
- 环境与 GitHub 接入：`docs/ENVIRONMENT.md`
- 交接机制：`docs/handoffs/README.md`
