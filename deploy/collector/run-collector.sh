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

COLLECTION_DATE=${1:-$(TZ=Asia/Shanghai date +%F)}
[[ "$COLLECTION_DATE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

COMPOSE=(
  docker compose
  -f "$previous_release_dir/docker-compose.yml"
  -f "$previous_release_dir/docker-compose.production.yml"
  -f "$previous_release_dir/docker-compose.release.yml"
  --profile collector
)

"${COMPOSE[@]}" run --rm --no-deps collector --date "$COLLECTION_DATE"
