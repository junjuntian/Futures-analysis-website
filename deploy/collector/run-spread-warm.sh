#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

STATE_FILE=/var/lib/futures-platform/deployments/stable.env
LOCK_FILE=/run/lock/futures-spread-warm.lock

test -r "$STATE_FILE"
# shellcheck disable=SC1090
. "$STATE_FILE"
test -n "${previous_release_dir:-}"
case "$previous_release_dir" in
  /opt/futures-platform-releases/*) ;;
  *) echo "SPREAD_WARM_FAIL unsafe_release_dir" >&2; exit 1 ;;
esac

exec 9>"$LOCK_FILE"
# A run can outlast the gap to the next one; overlapping runs would double the
# request rate at the one upstream this is meant to be gentle with.
flock -n 9 || exit 0

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
