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

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

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
"${COMPOSE[@]}" run --rm --no-deps collector --date "$COLLECTION_DATE" || COLLECTION_STATUS=$?
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

PROJECTION="$previous_release_dir/deploy/collector/project-history.sql"
if [ -r "$PROJECTION" ]; then
  "${COMPOSE[@]}" exec -T postgres \
    psql -U futures_app -d futures_platform -v ON_ERROR_STOP=1 < "$PROJECTION"
else
  # 老版本发布目录里没有这个文件。不当致命错误：采集本身已经成功了，
  # 报一声让日志里留下痕迹就够了。
  echo "PROJECTION_SKIPPED missing $PROJECTION" >&2
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
