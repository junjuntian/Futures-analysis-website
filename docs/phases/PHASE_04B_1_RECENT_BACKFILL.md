# Phase 4B-1：近 5 年历史回填

## 1. 范围

本阶段只使用 futures VPS 已验收的 collector 镜像
`82cec44184ffb6ae4bf700afd0210193a081ad0a` 和既有导入 API 回填近 5 年数据。
不构建镜像、不部署应用、不修改 Rust/collector 业务代码，不处理超过 5 年边界的
老年份适配。老年份适配和 Phase 4B 收口属于 4B-2。

唯一新增运行代码为 `deploy/collector/run-backfill.sh`。脚本固定读取生产
`stable.env`，拒绝不是上述 SHA 的运行候选，并核验 Compose 中 collector 的 digest
引用及 512 MiB 内存限额。

## 2. 日期与数据语义

- 主模式使用 `--from`、`--to` 指定闭区间，按日期倒序处理。
- 若 `trading_calendar_days` 已覆盖目标日期，以受控日历的 `is_trading_day` 为准。
- 日历未覆盖时只尝试周一至周五；周末记为已处理但不调用 collector。
- 每个日期按 DCE、SHFE、CZCE、GFEX、CFFEX 分来源调用既有 collector，默认执行
  catalog、calendar、market、seats 四数据集。DCE 的 official-first/Sina fallback
  行为完全由既有 82cec44 collector 执行，驱动脚本不改变来源身份。
- 若数据库中目标日期已经存在五所×四数据集的 succeeded 批次，记为
  `already_succeeded` 并跳过；业务唯一键仍是最终幂等保障。

## 3. 状态、失败与互斥

默认状态目录为 `/var/lib/futures-platform/backfill`，权限为 root-only：

- `state.env`：区间、水位、已处理日期数、未解决失败对数、磁盘水位和更新时间。
- `processed_dates.tsv`：每个已扫描日期及 `succeeded`、`partial`、
  `already_succeeded`、calendar skip 结果。
- `failed_dates.tsv`：未解决的日期/交易所、最后尝试时间、退出码和原因；成功重试后
  原子移除对应条目。
- `source_state.tsv`：当前上海自然日内各来源连续失败数及暂停标记。
- `daily_state.tsv`：当前上海自然日已经消耗的交易日额度。
- `events.log`、`runs/`：结构化事件和逐日期/来源日志，不记录凭据。

`--retry-failures` 只处理 `failed_dates.tsv` 中仍未解决的日期/来源。主遍历与失败
重试都受单实例 driver lock 控制；每次实际 collector 调用复用
`/run/lock/futures-collector.lock`，与 17:30、21:30 每日 cron 互斥。

## 4. 速率、时间与资源护栏

- 每个实际尝试日期结束后固定 sleep，`--sleep-seconds` 小于 60 会拒绝启动。
- 每个上海自然日最多 80 个尝试日期；计数持久化，重启进程不能绕过。
- 任一来源连续 5 个日期失败后，该来源在当日暂停；其余来源继续，暂停项进入失败
  清单，不做高频重试。次日来源状态重新开放。
- 17:00–22:30 禁止回填。生产默认进一步从 16:30 停止启动新来源，并给每个来源
  30 分钟 timeout，确保最迟 17:00 退出；22:30 后恢复。
- 每次启动和每个日期前检查 `/var/lib/docker`，磁盘使用率达到 80% 时立即停止并
  写入告警事件。
- collector 继续使用既有 Compose 的 512 MiB 限额、只读根文件系统和 `/work`
  tmpfs；驱动脚本不改变容器资源配置。

## 5. 运行方式

安装为 root-only 宿主脚本：

```bash
install -o root -g root -m 0700 deploy/collector/run-backfill.sh \
  /usr/local/sbin/run-futures-backfill
```

10 个交易日试跑示例：

```bash
nohup /usr/local/sbin/run-futures-backfill \
  --from 2026-07-16 --to 2026-07-29 \
  --daily-limit 80 --run-limit 10 --sleep-seconds 60 --continuous \
  >>/var/log/futures-backfill.log 2>&1 &
```

近 5 年连续回填示例：

```bash
nohup /usr/local/sbin/run-futures-backfill \
  --from 2021-08-04 --to 2026-08-03 \
  --daily-limit 80 --sleep-seconds 60 --continuous \
  >>/var/log/futures-backfill.log 2>&1 &
```

状态和失败重试：

```bash
/usr/local/sbin/run-futures-backfill --status
nohup /usr/local/sbin/run-futures-backfill --retry-failures \
  --daily-limit 80 --sleep-seconds 60 --continuous \
  >>/var/log/futures-backfill.log 2>&1 &
```

监控只读观察点：`state.env`、`failed_dates.tsv`、`events.log`、
`/var/log/futures-backfill.log`，以及 market/seat 行数、业务唯一键重复计数和磁盘
水位。出现某来源连续 412、429 或空数据失败达到 5 日时，以暂停状态和失败清单为
准，不手工提频。

## 6. 验收与边界

试跑必须确认：目标日期五所批次 succeeded 或失败如实入清单、正式表行数增长、
market/seat 业务唯一键重复均为 0、水位与失败状态可续跑、既有手动批次及用户数据
不变。连续任务启动后按自然日轻量观察行数、磁盘和失败增速。

到达近 5 年边界即停止。DCE 历史允许经既有 Sina fallback；单个历史日期失败保留
在失败清单中，不阻塞其他来源和日期，也不视为 Phase 4B-1 的系统性失败。任何
akshare 版本调整、老年份解析适配、新镜像、应用部署、main 合并均不在本阶段。
