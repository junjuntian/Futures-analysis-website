#!/usr/bin/env bash
# 机构资金信号引擎:每日盘后运行(采集入库之后)。
# 两段式:宿主机用 psql 导出 CSV → collector 镜像(自带 pandas)跑引擎 → 输出 signals.json
# 幂等:引擎全量重放,重复运行结果一致;失败不覆盖旧 signals.json。
set -euo pipefail

ROOT=/opt/futures-platform/smart-money
TMP="$ROOT/tmp"
WEB="$ROOT/web"
PG_CONTAINER=futures-analysis-platform-postgres-1
PG_USER=futures_app
PG_DB=futures_platform
# awk 读完整个流再输出首个匹配:head -1 会提前关管道,镜像多时上游收到
# SIGPIPE(退出码 141),在 pipefail 下判死整个脚本——2026-08-11 实测踩坑。
IMAGE=$(docker images --format '{{.ID}} {{.Repository}}' | awk '/collector/{if(!f){print $1;f=1}}')

if [ -z "$IMAGE" ]; then
  echo "[smart-money] 找不到 collector 镜像,中止" >&2
  exit 1
fi

mkdir -p "$TMP" "$WEB"

echo "[smart-money] $(date '+%F %T') 导出数据…"
# LH 与 AU/AG 一起导:生猪引擎读同样的两张表,只是品种不同。
for INST in AU AG LH; do
  low=$(echo "$INST" | tr 'A-Z' 'a-z')
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -q -c \
    "\copy (select exchange,instrument,contract,trade_date,open_price,high_price,low_price,close_price,settlement_price,volume,open_interest,source from price_history where instrument='$INST') to '/tmp/${low}_price.csv' with (format csv, header true)"
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -q -c \
    "\copy (select instrument,contract,is_variety_total,trade_date,rank_type,member,quantity,change,source from seat_history where instrument='$INST') to '/tmp/${low}_seat.csv' with (format csv, header true)"
  docker cp "$PG_CONTAINER:/tmp/${low}_price.csv" "$TMP/${low}_price.csv"
  docker cp "$PG_CONTAINER:/tmp/${low}_seat.csv" "$TMP/${low}_seat.csv"
  docker exec "$PG_CONTAINER" rm -f "/tmp/${low}_price.csv" "/tmp/${low}_seat.csv"
done

echo "[smart-money] 计算信号…"
docker run --rm \
  -v "$ROOT:/work" \
  -e ENGINE_SOURCE=csv \
  -e CSV_DIR=/work/tmp \
  -e ENGINE_DATA=/work/data \
  -e ENGINE_OUT=/work/tmp/signals.json \
  -e PYTHONIOENCODING=utf-8 \
  --entrypoint python "$IMAGE" /work/smart_money.py

# 仅在引擎成功产出后才替换线上文件
if [ -s "$TMP/signals.json" ]; then
  cp "$TMP/signals.json" "$WEB/signals.json.new"
  mv "$WEB/signals.json.new" "$WEB/signals.json"
  echo "[smart-money] 已更新 $WEB/signals.json"
else
  echo "[smart-money] 引擎无输出,保留上一版 signals.json" >&2
  exit 1
fi

# ---- 生猪:独立引擎、独立产物 ----
# 与金银**刻意分开跑**:信号形态不同(合计流向 vs 逐家共振),两条链失败也各自
# 隔离。生猪挂了不该让金银信号跟着不更新——所以这一段不带 set -e 的传染性,
# 失败只告警并保留上一版 hog_signals.json。
if [ -f "$ROOT/hog_money.py" ]; then
  echo "[hog] 计算生猪信号…"
  if docker run --rm       -v "$ROOT:/work"       -e ENGINE_SOURCE=csv       -e CSV_DIR=/work/tmp       -e HOG_OUT=/work/tmp/hog_signals.json       -e PYTHONIOENCODING=utf-8       --entrypoint python "$IMAGE" /work/hog_money.py; then
    if [ -s "$TMP/hog_signals.json" ]; then
      cp "$TMP/hog_signals.json" "$WEB/hog_signals.json.new"
      mv "$WEB/hog_signals.json.new" "$WEB/hog_signals.json"
      echo "[hog] 已更新 $WEB/hog_signals.json"
    else
      echo "[hog] 引擎无输出,保留上一版 hog_signals.json" >&2
    fi
  else
    echo "[hog] 引擎失败,保留上一版 hog_signals.json(不影响金银)" >&2
  fi
else
  echo "[hog] 未安装 hog_money.py,跳过生猪" >&2
fi

rm -f "$TMP"/*.csv
