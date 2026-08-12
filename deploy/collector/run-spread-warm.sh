#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

STATE_FILE=/var/lib/futures-platform/deployments/stable.env
# 与日更、部署抢**同一把**锁。原来自持一把 spread-warm 专属锁，只防自身并发，
# 防不了与部署的迁移 DDL 撞车：2026-08-12 实测预热 15:00:01–15:16:42 与部署
# 14:58 起的窗口整段重叠，owner 迁移一来就是死锁+回滚反杀连接那一套。
LOCK_FILE=/run/lock/futures-collector.lock

test -r "$STATE_FILE"
# shellcheck disable=SC1090
. "$STATE_FILE"
test -n "${previous_release_dir:-}"
case "$previous_release_dir" in
  /opt/futures-platform-releases/*) ;;
  *) echo "SPREAD_WARM_FAIL unsafe_release_dir" >&2; exit 1 ;;
esac

exec 9>"$LOCK_FILE"
# 等而不是立刻放弃：锁多半被部署或日更短暂占用，等一刻钟能保住当天的预热；
# 等不到就跳过——预热丢一天只是首个访客慢一点，不算失败。
flock -w 900 9 || exit 0

# Same reason as the collector wrapper: docker-compose.production.yml declares
# every image as `${IMAGE_TAG:?...}` and compose interpolates each file before
# merging, so the `:?` aborts the command even though the release overlay pins
# digests one file later. The value is inert; it only has to be set.
test -n "${previous_git_sha:-}"
export IMAGE_TAG="sha-${previous_git_sha}"

COMPOSE=(
  docker compose
  -f "$previous_release_dir/docker-compose.yml"
  -f "$previous_release_dir/docker-compose.production.yml"
  -f "$previous_release_dir/docker-compose.release.yml"
)

"${COMPOSE[@]}" run --rm --no-deps api /app/api --warm-spread-cache
