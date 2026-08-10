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
PROJECTION="$previous_release_dir/deploy/collector/project-history.sql"
if [ -r "$PROJECTION" ]; then
  "${COMPOSE[@]}" exec -T postgres \
    psql -U futures_app -d futures_platform -v ON_ERROR_STOP=1 < "$PROJECTION"
else
  # 老版本发布目录里没有这个文件。不当致命错误：采集本身已经成功了，
  # 报一声让日志里留下痕迹就够了。
  echo "PROJECTION_SKIPPED missing $PROJECTION" >&2
fi
