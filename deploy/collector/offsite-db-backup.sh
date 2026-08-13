#!/usr/bin/env bash
# 每日整库备份推到 ssp(172.104.107.155),远端只留最新一份。
#
# 为什么有这个:老机 2026-08-13 退役后,生产的全部备份都在本机
# /opt/futures-platform-backups——磁盘一坏,605 万行席位加 13 年历史一起没。
# 运营者拍板:异地放 ssp,只留最新一份,别占空间。
#
# 顺序是硬要求:**先验可读,再上传,最后才覆盖远端唯一副本**。
# 传一个坏文件把远端仅有的一份顶掉,比不备份更糟。所以:
#   1. 容器内 pg_dump 到本机
#   2. pg_restore --list 验证归档完整可读(custom 格式要可寻址文件,不能走管道)
#   3. scp 到远端 .tmp 名
#   4. 远端 mv 原子覆盖 latest.dump
# 任何一步失败都不会碰到远端现有副本。
#
# 恢复(在任何有 PG17+ 客户端的机器):
#   pg_restore -U futures_app -d futures_platform --clean --if-exists latest.dump
set -Eeuo pipefail

REMOTE=root@172.104.107.155
REMOTE_DIR=/root/futures-db-backup
KEY=/root/.ssh/futures_offsite_backup
STAMP=$(date -u +%Y%m%dT%H%M%SZ)

# /tmp 是 982MB 的 tmpfs,放不下也不该放(挤内存);用磁盘上的 /var/tmp。
TMP=$(mktemp -d /var/tmp/futures-offsite.XXXXXX)
PG=$(docker ps -qf name=postgres | head -1)
trap 'rm -rf "$TMP"; docker exec "$PG" rm -f /tmp/offsite.dump 2>/dev/null || true' EXIT
test -n "$PG"

docker exec "$PG" pg_dump -U futures_app -d futures_platform -Fc -Z 6 -f /tmp/offsite.dump
docker exec "$PG" pg_restore --list /tmp/offsite.dump >/dev/null
docker cp "$PG":/tmp/offsite.dump "$TMP/db.dump"

scp -q -i "$KEY" -o StrictHostKeyChecking=yes -o BatchMode=yes \
  "$TMP/db.dump" "$REMOTE:$REMOTE_DIR/db-$STAMP.dump.tmp"
ssh -i "$KEY" -o StrictHostKeyChecking=yes -o BatchMode=yes "$REMOTE" \
  "mv '$REMOTE_DIR/db-$STAMP.dump.tmp' '$REMOTE_DIR/latest.dump' && find '$REMOTE_DIR' -name 'db-*.dump.tmp' -delete"

echo "OFFSITE_BACKUP_OK $STAMP $(stat -c%s "$TMP/db.dump") bytes"
