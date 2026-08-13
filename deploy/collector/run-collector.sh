#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

STATE_FILE=/var/lib/futures-platform/deployments/stable.env
LOCK_FILE=/run/lock/futures-collector.lock

test -r "$STATE_FILE"
# shellcheck disable=SC1090
. "$STATE_FILE"
test -n "${previous_release_dir:-}"
case "$previous_release_dir" in
  /opt/futures-platform-releases/*) ;;
  *) echo "COLLECTOR_FAIL unsafe_release_dir" >&2; exit 1 ;;
esac

# 同一把锁部署那边也要拿（见 deploy-futures.yml 里 COLLECTOR_LOCK 的说明）：
# 迁移要 AccessExclusiveLock，和正在写 seat_history 的这一轮撞上就是死锁。
#
# 抢不到就跳过这一轮，但**要留下一行日志**。原来是 `flock -n 9 || exit 0`，
# 静默退出——部署恰好压在 09:30 或 13:30 上时，那一轮采集无声无息地没了，
# 日志里连一个字都没有。一天有两轮，漏一轮通常还能补回来，前提是看得见它漏了。
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "COLLECTION_SKIPPED 另一个作业占着 $LOCK_FILE（多半是部署），这一轮不跑" >&2
  exit 0
fi

# docker-compose.production.yml declares every image as
# `...:${IMAGE_TAG:?set IMAGE_TAG to an immutable sha-* tag}`. Compose
# interpolates each file before merging them, so that `:?` aborts the whole
# command even though docker-compose.release.yml immediately overrides all four
# images with pinned digests. The deploy job exports IMAGE_TAG and never
# noticed; cron does not, so every scheduled collection died at interpolation
# with no data written and nothing but a one-line error in the log. The value is
# inert here — the digests win — but it has to be set, so derive it from the
# release the state file points at.
test -n "${previous_git_sha:-}"
export IMAGE_TAG="sha-${previous_git_sha}"

COMPOSE=(
  docker compose
  -f "$previous_release_dir/docker-compose.yml"
  -f "$previous_release_dir/docker-compose.production.yml"
  -f "$previous_release_dir/docker-compose.release.yml"
  --profile collector
)

if [ "$#" -gt 0 ]; then
  COLLECTION_DATE=$1
else
  AS_OF_DATE=$(TZ=Asia/Shanghai date +%F)
  COLLECTION_DATE=$("${COMPOSE[@]}" run --rm --no-deps collector --resolve-date "$AS_OF_DATE")
fi
[[ "$COLLECTION_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]

# 采集失败也要往下走，但失败状态留到最后再退出。
#
# 大商所的行情按 DEC-047 是已知采不到的（官网 412、akshare 同源、东财端点拒绝），
# 采集器因此每天都以非零退出。而这个脚本是 set -e：它当场就死了，后面的投影、
# 新浪日更、汇总一步都跑不到——于是数据停在 market_prices，永远进不了 price_history。
# **日更投影其实从来没有自动成功过**，2026-08-11 运营者问「为什么没有 8.11 的数据」
# 时才查出来（那天先查出 cron 时区不生效，修完仍然没数据，才发现还有这一层）。
#
# 一部分交易所失败不该让另外四家的数据也进不了库。状态记下来，最后如实退出。
COLLECTION_STATUS=0

# 席位:写 CSV 直灌宽表,不经导入通道。
#
# 2026-08-13 查生产后重新划的分工。此前每天的采集都走「上传→暂存→逐行校验→
# 冲突检测→确认→血缘→canonical→投影→宽表」七层,而那套是为「人工上传文件
# 需要预览和回滚」建的——运营者说明那个入口服务的 AI 分析功能早已取消,他从没
# 手工导过数据。代价是中间产物(暂存行、变更记录、血缘)和最终业务数据一样大,
# 光席位一项就占了 88%。
#
# 而三家交易所里,**只有大商所席位还真正需要采集器**:
#   上期所 / 郑商所 席位 —— run-official-seats.sh 早已直灌,且带增减量
#                          (走通道的 akshare 那份最新日一行都没进来,纯冗余)
#   大商所 行情         —— 新浪直灌(load-dce-daily.sql)
#   上期所 / 郑商所 行情 —— run-official-seats.sh 直灌,带持仓量
#                          (通道那份持仓量为 0,套利监控靠它筛流动性)
#
# 所以这里只采大商所席位,并直接落 CSV。品种目录与交易日历暂时仍走通道
# (它们的表有 source_record_id 外键绑着 imported_records,直灌要先改 schema),
# 那部分只占中间产物的 8%,单独一批处理。
CSV_DIR=/opt/futures-platform/load/collector
install -d -m 700 "$CSV_DIR"
SEATS_LOAD="$previous_release_dir/deploy/collector/load-seats-direct.sql"
if [ -r "$SEATS_LOAD" ]; then
  if "${COMPOSE[@]}" run --rm --no-deps \
       -v "$CSV_DIR":/tmp/emit \
       collector --date "$COLLECTION_DATE" --exchange DCE --dataset seats --emit-csv /tmp/emit; then
    seat_csv="$CSV_DIR/DCE-seat_positions_v1-$COLLECTION_DATE.csv"
    if [ -s "$seat_csv" ]; then
      postgres_id=$("${COMPOSE[@]}" ps -q postgres)
      docker cp "$seat_csv" "$postgres_id":/tmp/seats_direct.csv
      "${COMPOSE[@]}" exec -T postgres \
        psql -U futures_app -d futures_platform -v ON_ERROR_STOP=1 \
        -v csv_path=/tmp/seats_direct.csv -v source_code=eastmoney_seats_v1 < "$SEATS_LOAD"
    else
      # 采集器成功但没写出文件 = 那天没有数据(节假日),不是错误。
      echo "DCE_SEATS_EMPTY $COLLECTION_DATE 没有席位数据" >&2
    fi
  else
    COLLECTION_STATUS=1
    echo "DCE_SEATS_FAILED 大商所席位今天没采到，日更其余步骤继续" >&2
  fi
else
  echo "SEATS_DIRECT_SKIPPED missing $SEATS_LOAD" >&2
fi

# 品种目录与交易日历也走直灌。
#
# 这两样此前是导入通道最后的输入。它们的表(exchanges/instruments/contracts/
# trading_calendar_*)原本带着 not null 外键指向 imported_records、import_batches、
# data_sources——想写一行就得先在通道里造一整套血缘,迁移 202608130002 解开了。
#
# 目录里装的是页面天天读的东西:品种中文名、合约与交割月、**点值**。点值算错
# 鸡蛋盈亏就差一倍,所以装载 SQL 里明确不覆盖 price_multiplier(采集器给的
# contract_multiplier 是交易单位 5,点值是 10,两回事)。
# 日历给自由价差的散户可交易窗口用,缺了会退回一个粗略的近似算法。
#
# 至此日更完全不经导入通道。
#
# 采集失败也要往下走:一部分数据集失败不该让其余步骤进不了库。状态记下来,
# 最后如实退出。
for pair in "catalog:futures_catalog_v1:load-catalog-direct.sql" \
            "calendar:trading_calendar_v1:load-calendar-direct.sql"; do
  dataset=${pair%%:*}; rest=${pair#*:}
  dataset_type=${rest%%:*}; loader=${rest#*:}
  loader_path="$previous_release_dir/deploy/collector/$loader"
  if [ ! -r "$loader_path" ]; then
    echo "${dataset^^}_DIRECT_SKIPPED missing $loader_path" >&2
    continue
  fi
  if ! "${COMPOSE[@]}" run --rm --no-deps \
         -v "$CSV_DIR":/tmp/emit \
         collector --date "$COLLECTION_DATE" --dataset "$dataset" --emit-csv /tmp/emit; then
    COLLECTION_STATUS=1
    echo "${dataset^^}_FAILED 今天没采到，日更其余步骤继续" >&2
    continue
  fi
  # 一个交易所一个文件。逐个装载:一家的数据坏掉不该让另外两家也进不了库。
  for csv in "$CSV_DIR"/*-"$dataset_type"-"$COLLECTION_DATE".csv; do
    [ -s "$csv" ] || continue
    postgres_id=$("${COMPOSE[@]}" ps -q postgres)
    docker cp "$csv" "$postgres_id":/tmp/direct.csv
    "${COMPOSE[@]}" exec -T postgres \
      psql -U futures_app -d futures_platform -v ON_ERROR_STOP=1 \
      -v csv_path=/tmp/direct.csv < "$loader_path" ||
      { COLLECTION_STATUS=1; echo "${dataset^^}_LOAD_FAILED $(basename "$csv")" >&2; }
  done
done
if [ "$COLLECTION_STATUS" -ne 0 ]; then
  echo "COLLECTION_PARTIAL exit=$COLLECTION_STATUS 继续做投影，最后仍以此状态退出" >&2
fi

# 采到的东西还要投影进两张历史表，套利页和席位页读的是那两张。放在采集之后同一个
# 脚本里而不是另开一条 cron：顺序是硬要求，投影必须看得到刚落库的那一天，两条独立
# 的定时任务迟早会在某个慢日子里跑反。
# 大商所的行情不走上面那条采集：交易所官网对所有客户端 412，akshare 的大商所接口打的
# 是同一个站，东财的行情与 K 线端点 2026-08-10 起第一个请求就被断开。新浪是当天唯一
# 还应答的源，而且 price_history 里大商所 2025-01 之后的行情本来就是它的。见 DEC-047。
DCE_SCRIPT="$previous_release_dir/deploy/collector/sina-dce-daily.py"
DCE_LOAD="$previous_release_dir/deploy/collector/load-dce-daily.sql"
if [ -r "$DCE_SCRIPT" ] && [ -r "$DCE_LOAD" ]; then
  # 失败不拖垮整轮：其余四家已经采完了，把它们丢掉没有道理。
  if "${COMPOSE[@]}" run --rm --no-deps \
       -v "$DCE_SCRIPT":/tmp/sina-dce-daily.py:ro \
       -v /opt/futures-platform/load:/tmp/load \
       --entrypoint python collector /tmp/sina-dce-daily.py --out /tmp/load/price_dce_daily.csv; then
    postgres_id=$("${COMPOSE[@]}" ps -q postgres)
    docker cp /opt/futures-platform/load/price_dce_daily.csv "$postgres_id":/tmp/price_dce_daily.csv
    "${COMPOSE[@]}" exec -T postgres \
      psql -U futures_app -d futures_platform -v ON_ERROR_STOP=1 < "$DCE_LOAD"
  else
    echo "DCE_DAILY_FAILED 新浪那一步没成功，大商所行情今天不前进" >&2
  fi
else
  echo "DCE_DAILY_SKIPPED missing $DCE_SCRIPT" >&2
fi

# 这里原本有一步「投影」：把 market_prices / seat_positions（导入通道的中转表）
# 搬进 price_history / seat_history（页面读的宽表）。上面几步现在直接写宽表，
# 中转表也已随 DEC-049 删除，这座桥两岸都没了。

# 修掉三禾填出来的「持仓 0」。**必须排在品种汇总之前**：它会改写和删除席位行，
# 跑在汇总之后的话，汇总里还留着按假 0 算出来的合计，要等到第二天才对得上。
#
# 每天都得跑，不是一次性清理：三禾的采集每天都在按同样的手法写新的假 0
# （掉榜日填一行持仓 0、增减记 −前日持仓）。首次清理修了 2,484 行、删了 15,269 行，
# 之后每天只处理新增的那几行，全表扫一遍也只要几秒。
SANHE_ZEROS="$previous_release_dir/deploy/collector/fix-sanhe-fabricated-zeros.sql"
if [ -r "$SANHE_ZEROS" ]; then
  "${COMPOSE[@]}" exec -T postgres \
    psql -U futures_app -d futures_platform -v ON_ERROR_STOP=1 < "$SANHE_ZEROS"
else
  echo "SANHE_ZEROS_SKIPPED missing $SANHE_ZEROS" >&2
fi

# 修掉三禾回写持仓后没跟着改的「增减」。紧跟在零持仓那一步之后：它要读「昨日持仓」，
# 而零持仓脚本刚刚改写和删除过那些行。也必须在品种汇总之前——汇总会把增减求和。
#
# 上一步管的是持仓仍是 0 的行；这一步管它已经回写过持仓、却把清零差分留在增减里的行
# （财达 JD2505 04-17 的 `215 (−822)`，真实增减是 −607）。趋势跟随读的就是增减。
SANHE_CHANGES="$previous_release_dir/deploy/collector/fix-sanhe-fabricated-changes.sql"
if [ -r "$SANHE_CHANGES" ]; then
  "${COMPOSE[@]}" exec -T postgres \
    psql -U futures_app -d futures_platform -v ON_ERROR_STOP=1 < "$SANHE_CHANGES"
else
  echo "SANHE_CHANGES_SKIPPED missing $SANHE_CHANGES" >&2
fi

# 品种汇总。大商所、上期所官方不发品种合计，郑商所的合计口径也要统一，这些
# 汇总行是 compute-seat-totals.sql 从席位行自算出来的。它一直只在回填时手工
# 跑过——发布包里装了这个文件，却没有任何定时任务执行它，于是汇总永远停在
# 上一次有人手工跑的那天（2026-08-12 部署后验证时发现停在 08-10）。跟投影
# 同理：必须跟在采集后面、在同一个脚本里按顺序跑。
# window_days=10 只重算最近十天：日更只需要覆盖新落库的一两天，兜一点补采余量。
SEAT_TOTALS="$previous_release_dir/deploy/collector/compute-seat-totals.sql"
if [ -r "$SEAT_TOTALS" ]; then
  "${COMPOSE[@]}" exec -T postgres \
    psql -U futures_app -d futures_platform -v ON_ERROR_STOP=1 -v window_days=10 \
    < "$SEAT_TOTALS"
else
  echo "SEAT_TOTALS_SKIPPED missing $SEAT_TOTALS" >&2
fi

# 掉榜前一日的持仓反推。排在投影之后（它读 seat_history）。
#
# 放在品种汇总**之后**是有意的：反推行被排除在汇总之外，见 compute-seat-totals.sql
# 里的说明——一份「有时含反推、有时不含」的品种汇总会凭空造出 ΔNet 跳变，
# 对趋势跟随比少算更糟。所以两者先后其实无关，摆这里只是让日志顺序好读。
#
# window_days=7 只重算最近一周：反推只依赖「今天回榜、昨天不在榜」这一对相邻日，
# 新数据到了才需要重算，往回一周足够兜住补采。
INFER_OFFBOARD="$previous_release_dir/deploy/collector/infer-offboard-seats.sql"
if [ -r "$INFER_OFFBOARD" ]; then
  "${COMPOSE[@]}" exec -T postgres \
    psql -U futures_app -d futures_platform -v ON_ERROR_STOP=1 -v window_days=7 \
    < "$INFER_OFFBOARD"
else
  echo "INFER_OFFBOARD_SKIPPED missing $INFER_OFFBOARD" >&2
fi

# 套利监控快照。必须排在投影之后：它读的是 price_history，而那张表由投影填。
# 生产实测约 77 秒（瓶颈是历年百分位那一步，见 SQL 里的注释）。
# window_days=3 只重算最近三天：日更只需覆盖新落库的一两天，兜一点补采余量。
SPREAD_MONITOR="$previous_release_dir/deploy/collector/compute-spread-monitor.sql"
if [ -r "$SPREAD_MONITOR" ]; then
  "${COMPOSE[@]}" exec -T postgres \
    psql -U futures_app -d futures_platform -v ON_ERROR_STOP=1 -v window_days=3 \
    < "$SPREAD_MONITOR"
else
  echo "SPREAD_MONITOR_SKIPPED missing $SPREAD_MONITOR" >&2
fi

# 投影做完了，现在才把采集的失败如实抛出去——cron 的邮件与退出码仍然看得到它，
# 只是不再因为它而丢掉当天其余四家交易所的数据。
exit "$COLLECTION_STATUS"
