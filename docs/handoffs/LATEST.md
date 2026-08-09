# 最新交接状态

- **最新权威交接：`docs/handoffs/HANDOFF_20260809_2130.md`。先读它第 0 节三条。** 同日的 `HANDOFF_20260809_1111.md`（部署铁律、分支拓扑、腿序规则）与 `HANDOFF_20260809_1740.md`（自由价差前端修复）仍然有效，其中已过时或口径打架的地方都在原文就地标注并指回 2130，历史结论未删改。
- **接手前必读 `1111` 第 0 节「部署铁律」**（四条，每条都是踩出来的），再看第 2 节分支拓扑，再补 2130 第 5 节的第五条。
- 生产运行 **`f7374e4`**（2026-08-09 部署成功，见 2130 第 10 节）。`retained_points` 2069→2071 是日历修复的直接量化证据。**其后仍有未部署提交**（workflow 打包修复等），需再跑一轮镜像构建+部署；含 Rust/采集器改动的批次**四个镜像都要重编，不是 15 分钟路径**。
- **两个当天查出的生产故障已修并已部署**（`IMAGE_TAG` 修复的真实效果待周一 cron 验证）：①定时采集自安装以来一次都没成功过（`run-collector.sh` 缺 `export IMAGE_TAG`，compose 逐文件插值时 `:?` 先炸），导致行情与交易日历停在 2026-08-05、08-04 整天为空；②交易日历只采半个月时，散户截止日被算成「已采到的最后一天」，把自由价差图的最后两天误剔。两者串联，详见 2130 第 2 节。
- 部署铁律：①`run_live_collection` 与采集无关时传 `false`（本批相关，见 2130 第 6 节）；②传了还要 `grep -c PHASE4A_RUN_LIVE_COLLECTION rust/tests/phase_4a_e2e.sh` 确认 ≥1，否则开关空转白跑一小时；③镜像必须本链新构建；④验收 Origin 与 `PUBLIC_ORIGIN` 一致（workflow 已自动传，勿删）；⑤**镜像构建完成后、部署跑完前不要再往 `deploy/phase-5a-candidate` 推任何提交（含文档）**，否则 `acceptance_sha` 与 ref tip 对不上，第一道护栏就拒。
- **数据获取有两条独立链路，谈「采集渠道」时必须说清是哪一条**（详见 2130 第 3.0 节）：
  - **链路 A 采集器**（Phase 4A，cron 定时，落事实表）：五家交易所官方 akshare 接口；DCE 官方全线 412，实际走新浪 fallback（`DEC-041`）；**东方财富已按 `DEC-043` 接入为席位专用兜底源**（`datacenter-web.eastmoney.com` 报表 `RPT_FUTU_DAILYPOSITION`，不经 akshare，排在全部官方源之后，五家通用，不承担行情与目录，INE 排除）。`1111` 里记的 `qhhqzl.eastmoney.com/...` 地址是错的。
  - **链路 B 服务端只读代理**（Phase 5A，`DEC-042`，用户查一次打一次）：**三禾 `www.sanheshuju.com`**，连接器 `sanhe_spread_v1`，三个端点 `all_varieties`/`variety_contracts`/`arbitrage_varieties`，只允许服务端调用，前端不得直连。**其数据一条都不进事实表**，只落 `spread_provider_*` 与 `spread_window_segments`。同端点+同参数按 `Asia/Shanghai` 业务日只取一次并持久缓存，请求间隔 2000ms。
- 业务口径：三禾序列会混入反向组合（查 09-01 时混进 `jm2609-jm2601`，那属于 01-09），规则=前腿必须先到期，不满足整段排除。**换段边界有三个不同的数**（服务端发 16 / 图上画 15 / 真实换月 13），别混用，见 2130 第 4 节；**服务端 `segment_boundaries` 语义仍错，只在前端绕过了**。
- 待办：部署本批 → 服务端段边界语义 → Phase 5A 独立 Evaluator → 4B-2 → 前端肉眼验收 → collector 凭据轮换。Phase 5 尚未合 main（main 停在 `b5db24e`），待 Evaluator PASS 后合。

- **两处「文档说的和机器上的不一样」，见 2130 第 6.5 节**：①`run-backfill.sh` 从来不在发布包里，生产那份停在 2026-08-05 手工安装的快照（已修，并加了断言：仓库拥有的每个运维脚本必须同时出现在打包与安装两处）；②`futures-backfill-phase4b1.service` **这个 systemd 单元根本不存在**——`systemctl is-active` 对不存在的单元也答 `inactive`（退出码 4），据此误判过。**判断东西在不在就 `ls` 它，别问一个对缺失和停用给同样答案的命令。**

接手须以 Git、Actions、VPS 实态复核，不盲信摘要；不得输出密钥、恢复回填、清理数据或未经授权合 main/打标签。
