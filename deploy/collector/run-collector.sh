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

"${COMPOSE[@]}" run --rm --no-deps collector --date "$COLLECTION_DATE"

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
