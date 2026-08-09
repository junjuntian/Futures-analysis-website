# 最新交接状态

- 最新完整交接：`docs/handoffs/HANDOFF_20260809_1111.md`。**接手前必读其第 0 节「部署铁律」**（四条，每条都是踩出来的），再看第 2 节分支拓扑。
- 增量补充：`docs/handoffs/HANDOFF_20260809_1740.md`（自由价差前端三项缺陷修复，提交 `9fa886e`）。**其中 2.1 是通用坑**：echarts 用按需入口，未 `use()` 注册的组件其 option 被静默忽略、无任何报错——时间滑块「写了但没出现」就是漏注册 `DataZoomComponent`。
- 生产运行 `babb15d`：Phase 5A 自由价差页含完整 13 年历史、腿序规则修正、时间滑块、数据视图、季节叠年图连续化。最后一轮部署已切换成功但验收被主动取消（未按提速路径跑，误入 1 小时真采），**需补一次干净的部署验收**。
- 分支：`phase/05-spread-analytics` 与 `deploy/phase-5a-candidate` 已合一（同一提交），e2e 脚本全部并入主线。这是为避免两份脚本互相覆盖——提速短路代码就是这样丢失的。
- 部署四条铁律：①`run_live_collection` 与采集无关时传 `false`；②**传了还要 `grep -c PHASE4A_RUN_LIVE_COLLECTION rust/tests/phase_4a_e2e.sh` 确认 ≥1**，否则开关空转白跑一小时；③镜像必须本链新构建；④验收 Origin 与 `PUBLIC_ORIGIN` 一致（workflow 已自动传，勿删）。正常耗时 ≈15 分钟，超 20 分钟未见验收标记即应检查第②条。
- 业务口径要点：三禾序列会混入反向组合（查 09-01 时混进 `jm2609-jm2601`，那属于 01-09）。规则=前腿必须先到期，不满足整段排除。实测 jm 09-01 保留点 2987→2069，段边界 33→16，每段起于晚上市腿的上市日。
- 待办：补部署验收 → Phase 5A 独立 Evaluator（全新会话）→ 4B-2 → Phase 6 前确认 OPEN-PORT-002。前端三项只经 CI 验证，未经人工肉眼验收。
- Phase 5 尚未合 main（main 停在 `phase-4a-pass-20260805`），待 Evaluator PASS 后合。

接手须以 Git、Actions、VPS 实态复核，不盲信摘要；不得输出密钥、恢复回填、清理数据或未经授权合 main/打标签。
