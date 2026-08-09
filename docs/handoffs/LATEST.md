# 最新交接状态

- 最新完整交接：`docs/handoffs/HANDOFF_20260809_1111.md`（务必先读其第 2、6 节：分支拓扑与部署参数）。
- Phase 5A 自由价差页已上生产可用，但当前生产 `e2f5c11` 仅含近一年历史；含完整 13 年历史 + 三处根因修复 + 发布流程提速的新候选就绪，正在部署。
- 分支：代码源头 `phase/05-spread-analytics`（HEAD `49f3dcd`）；部署候选 `deploy/phase-5a-candidate`（HEAD `decdef0`）。两支只差两个白名单 e2e 脚本，部署"仅白名单差异"门会过。`ci.yml` 不在白名单，只放 phase/05，两支必须一致。
- 三处根因（全在 phase/05）：provider 层 + domain 层日期严格递增误杀换月交界重复日期（改段内严格）；窗口引擎历史合约查不到致图只剩一年（`derive_contract_window` 从合约代码自解析交割月，jm 09-01 保留点 130→2989）。Rust 171 测试绿。
- 发布提速四条（运营者确定）：cargo-chef 依赖分层（`a001762`，版本 pin 0.1.77）+ 删 CI 冗余镜像构建 + deploy 加 `run_live_collection` 开关（默认 true；false 时 4A 只做只读轻量回归、跳过三次真采省~1h）；快通道由该开关覆盖不另建。采集无关部署 ~2h→~15-20min。**均为静态验证，首次部署验证；工装改动在部署前门，失败不影响生产。**
- 生产：`PUBLIC_ORIGIN` 已改为 IP（IP 访问登录所需）；临时登录用 collector 服务账号（凭据待轮换）；回填暂停（运营者指令，标记文件在 VPS）；DCE 走新浪 fallback（东财席位接口待 DEC-041 修订纳入）。
- 下一步：本次部署完成 → Phase 5A 独立 Evaluator（全新会话，重点见交接第 7 节）→ 4B-2 → Phase 6 前确认 OPEN-PORT-002。
- Phase 5 尚未合 main（main 停在 `phase-4a-pass-20260805`），待 5A Evaluator PASS 后合。

接手须以 Git、Actions、VPS 实态复核，不盲信摘要；不得输出密钥、恢复回填、清理数据或未经授权合 main/打标签。
