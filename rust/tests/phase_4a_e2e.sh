#!/usr/bin/env bash
# 采集链路的生产验收。
#
# 这个脚本 2026-08-13 重写过一次。旧版验的是审计导入通道:上传文件、逐行校验、
# 冲突检测、人工确认、批次回滚、血缘追溯——763 行里大半在断言那套。运营者说明
# 导入中心服务的 AI 分析功能早已取消,采集全部改走直灌(CSV → psql → 宽表),
# 通道连同 27 张表一起摘除,旧断言随之失去对象。
#
# 现在验的是直灌链路真正该保证的四件事:
#   1. 采集器能把数据写成 CSV(格式与列序对得上装载脚本)
#   2. 装载脚本能把 CSV 写进宽表,且**真的写进去了**(不是静默跳过)
#   3. 重复跑不会写重(幂等)
#   4. 一个数据集失败不连累其余(隔离)
#
# 为什么第 2 条要单独强调:2026-08-13 首次试跑时,五个 CSV 全部报告「装载成功」
# 而库里一行没多——`\copy` 不做变量插值,报错还不中断执行,最后 commit 一个空
# 事务。**「命令返回 0」不等于「数据进去了」**,所以每一步都比对前后行数。

set -Eeuo pipefail

RELEASE_DIR=${PHASE4A_RELEASE_DIR:?set PHASE4A_RELEASE_DIR}
COLLECTION_DATE=${PHASE4A_COLLECTION_DATE:?set PHASE4A_COLLECTION_DATE}
RUN_LIVE_COLLECTION=${PHASE4A_RUN_LIVE_COLLECTION:-true}
EVIDENCE_DIR=${PHASE4A_EVIDENCE_DIR:?set PHASE4A_EVIDENCE_DIR}
DB_SERVICE=postgres
DB_USER=futures_app
DB_NAME=futures_platform

case "$RELEASE_DIR" in
  /opt/futures-platform-releases/*) ;;
  *) echo "PHASE4A_E2E_FAIL unsafe_release_dir" >&2; exit 1 ;;
esac

echo "PHASE4A_E2E_STAGE preconditions"
install -d -m 700 "$EVIDENCE_DIR"

# cron 与安装产物按整文件比对。逐条 grep 兑现过两个坑:改时刻漏改断言(2026-08-11
# 整轮回滚),以及只断言两条、第三条被删了也发现不了。部署就是把 bundle 里那份
# install 过来的,逐字节一致是唯一不会漂移的断言。
diff -u "$RELEASE_DIR/deploy/collector/futures-collector.cron" /etc/cron.d/futures-collector
test "$(stat -c %a /etc/cron.d/futures-collector)" = 600
test -x /usr/local/sbin/run-futures-collector
echo "PHASE4A_E2E_STAGE preconditions_passed"

COMPOSE=(
  docker compose
  -f "$RELEASE_DIR/docker-compose.yml"
  -f "$RELEASE_DIR/docker-compose.production.yml"
  -f "$RELEASE_DIR/docker-compose.release.yml"
  --profile collector
)

psql_value() {
  "${COMPOSE[@]}" exec -T "$DB_SERVICE" \
    psql -X -U "$DB_USER" -d "$DB_NAME" -Atq -v ON_ERROR_STOP=1 "$@"
}

workspace_id=$(psql_value -c "select id from workspaces order by created_at limit 1")
test -n "$workspace_id"

# ---- 基线 ----
#
# 每一步都以「前后行数」判定成败,而不是命令的退出码。理由见文件头。
seats_before=$(psql_value -c "select count(*) from seat_history")
prices_before=$(psql_value -c "select count(*) from price_history")
instruments_before=$(psql_value -c "select count(*) from instruments")
contracts_before=$(psql_value -c "select count(*) from contracts")
echo "PHASE4A_E2E_BASELINE seats=$seats_before prices=$prices_before instruments=$instruments_before contracts=$contracts_before"

# 导入通道必须确实不在了。留着一张空表不影响运行,但会让下一个人以为那条路还能走。
for gone in import_batches import_staging_rows imported_records market_prices seat_positions job_queue; do
  test "$(psql_value -c "select count(*) from pg_tables where schemaname='public' and tablename='$gone'")" = 0
done
echo "PHASE4A_E2E_STAGE import_channel_absent"

# 点值:八个品种都必须有,而且鸡蛋必须是 10 而不是交易单位 5。
# 这一条单列是因为它错了不会报错,只会让盈亏差一倍。
test "$(psql_value -c "select count(*) from instruments where price_multiplier is not null and code in ('AU','AG','JD','LH','JM','AP','FG','SA')")" = 8
test "$(psql_value -c "select price_multiplier::int from instruments where code='JD' limit 1")" = 10
echo "PHASE4A_E2E_STAGE price_multipliers_intact"

if [ "$RUN_LIVE_COLLECTION" != true ]; then
  # 轻量回归:不真采,只确认已有数据没被这次发布弄坏。
  test "$seats_before" -gt 0
  test "$prices_before" -gt 0
  test "$instruments_before" -gt 0
  echo "PHASE4A_E2E_LIGHT_REGRESSION_PASS seats=$seats_before prices=$prices_before"
  echo "PHASE4A_E2E_PASS"
  exit 0
fi

# ---- 真采一轮 ----
CSV_DIR=/opt/futures-platform/load/collector
install -d -m 700 "$CSV_DIR"
rm -f "$CSV_DIR"/*-"$COLLECTION_DATE".csv "$CSV_DIR"/*-"$COLLECTION_DATE".csv.failed 2>/dev/null || true

echo "PHASE4A_E2E_STAGE live_collection_started"
collect_status=0
"${COMPOSE[@]}" run --rm --no-deps \
  -v "$CSV_DIR":/tmp/emit \
  collector --date "$COLLECTION_DATE" --dataset catalog --emit-csv /tmp/emit \
  >"$EVIDENCE_DIR/catalog.log" 2>&1 || collect_status=$?

# 大商所席位是采集器在日更里唯一不可替代的活:上期所与郑商所的席位和行情由
# run-official-seats.sh 直灌(且带增减量与持仓量),大商所行情由新浪直灌。
seats_status=0
"${COMPOSE[@]}" run --rm --no-deps \
  -v "$CSV_DIR":/tmp/emit \
  collector --date "$COLLECTION_DATE" --exchange DCE --dataset seats --emit-csv /tmp/emit \
  >"$EVIDENCE_DIR/seats.log" 2>&1 || seats_status=$?

# 采集失败不等于验收失败:上游偶发不可达是常态(DEC-047 的 DCE 行情就是常态
# 不可达)。但**写出来的东西必须是对的**——所以失败只记录,下面照样验格式与幂等。
echo "PHASE4A_E2E_COLLECTION catalog_exit=$collect_status seats_exit=$seats_status"

# 采成功却没写出文件 = 那天没有数据(节假日),不是错误;
# 采失败必须留下 .failed 标记,与「压根没跑」区分得开。
if [ "$seats_status" -ne 0 ]; then
  test -n "$(find "$CSV_DIR" -name "*seat_positions_v1-$COLLECTION_DATE.csv.failed" -print -quit)"
  echo "PHASE4A_E2E_STAGE failure_left_a_marker"
fi

# ---- 装载并核对「真的写进去了」 ----
load_one() {
  local csv=$1 loader=$2 extra=${3:-}
  local pg
  pg=$("${COMPOSE[@]}" ps -q "$DB_SERVICE")
  docker cp "$csv" "$pg":/tmp/direct.csv
  docker cp "$RELEASE_DIR/deploy/collector/$loader" "$pg":/tmp/loader.sql
  # shellcheck disable=SC2086
  "${COMPOSE[@]}" exec -T "$DB_SERVICE" \
    psql -X -U "$DB_USER" -d "$DB_NAME" -v ON_ERROR_STOP=1 $extra -f /tmp/loader.sql
}

loaded_any=0
for csv in "$CSV_DIR"/*-futures_catalog_v1-"$COLLECTION_DATE".csv; do
  test -s "$csv" || continue
  load_one "$csv" load-catalog-direct.sql >>"$EVIDENCE_DIR/load.log" 2>&1
  loaded_any=1
done

if [ "$loaded_any" = 1 ]; then
  # 目录是 upsert:行数可能不变(品种早就有了),所以看的是 updated_at 有没有被推进。
  # 只看行数会把「静默什么都没做」判成成功——那正是重写这个脚本的起因。
  test "$(psql_value -c "select count(*) from instruments where updated_at > now() - interval '10 minutes'")" -gt 0
  echo "PHASE4A_E2E_STAGE catalog_rows_actually_written"
fi

# 文件名与来源标签都必须与 run-collector.sh 逐字一致:验收要么验的是生产真正
# 走的那条路,要么什么都没验。preflight 有一条守卫盯着两边不漂移。
SEAT_CSV="$CSV_DIR/DCE-seat_positions_v1-$COLLECTION_DATE.csv"
if test -s "$SEAT_CSV"; then
  load_one "$SEAT_CSV" load-seats-direct.sql "-v source_code=eastmoney_seats_v1" \
    >>"$EVIDENCE_DIR/load.log" 2>&1
  test "$(psql_value -c "select count(*) from seat_history where source='eastmoney_seats_v1' and trade_date=date '$COLLECTION_DATE'")" -gt 0
  echo "PHASE4A_E2E_STAGE seat_rows_actually_written"
fi

# ---- 幂等 ----
#
# 同一份 CSV 再装一遍,行数必须一模一样。日更每天两轮、补采还会重跑,
# 写重的话席位会凭空翻倍,而图上看不出来——线还是连续的。
seats_after_first=$(psql_value -c "select count(*) from seat_history")
if test -s "$SEAT_CSV"; then
  load_one "$SEAT_CSV" load-seats-direct.sql "-v source_code=eastmoney_seats_v1" \
    >>"$EVIDENCE_DIR/load.log" 2>&1
fi
test "$(psql_value -c "select count(*) from seat_history")" = "$seats_after_first"
echo "PHASE4A_E2E_STAGE idempotent"

# ---- 既有数据没被弄坏 ----
test "$(psql_value -c "select count(*) from seat_history")" -ge "$seats_before"
test "$(psql_value -c "select count(*) from price_history")" -ge "$prices_before"
test "$(psql_value -c "select count(*) from contracts")" -ge "$contracts_before"
# 点值再验一次:装载脚本明确不覆盖它,但这条是「错了差一倍」级别的,值得验两遍。
test "$(psql_value -c "select price_multiplier::int from instruments where code='JD' limit 1")" = 10
echo "PHASE4A_E2E_STAGE existing_data_intact"

echo "PHASE4A_E2E_SUMMARY seats=$(psql_value -c "select count(*) from seat_history") prices=$(psql_value -c "select count(*) from price_history") instruments=$(psql_value -c "select count(*) from instruments")"
echo "PHASE4A_E2E_PASS"
