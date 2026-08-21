#!/usr/bin/env bash
# 机构资金信号引擎:每日盘后运行(采集入库之后)。
# 两段式:宿主机用 psql 导出 CSV → collector 镜像(自带 pandas)跑引擎 → 输出 signals.json
# 幂等:引擎全量重放,重复运行结果一致;失败不覆盖旧 signals.json。
set -euo pipefail

ROOT=/opt/futures-platform/smart-money
TMP="$ROOT/tmp"
WEB="$ROOT/web"

# **自己跟自己不能并发**(2026-08-20 核验补)。这个脚本此前**不加任何锁**,
# 而它有两个互不知情的调用方:
#   · cron       08:40 / 10:10 / 14:10 UTC
#   · 部署       DEC-099 的 ENGINE_REFRESH,引擎文件一变就立刻跑一遍
# 一轮要跑六到八分钟,部署赶在 10:08 触发就会和 10:10 那轮叠在一起。两轮共用
# 同一个 $TMP:后来者的 psql 导出会盖掉前者正在读的 CSV,而先跑完的那一轮
# 结尾 `rm -f "$TMP"/*.csv` 会把另一轮的中间文件直接删掉。
# 结果是**信号文件算错或没产出**,而 ENGINE_REFRESH 失败是有意不阻断部署的
# (`|| true`),没人会当场发现。这是整条链上唯一没保护的作业,偏偏产出的是
# 运营者据以下单的信号。
#
# 用**自己的锁**,不用 futures-collector.lock:部署在跑 ENGINE_REFRESH 时**仍然
# 握着**那把锁(fd 8 从迁移一直开到脚本结束),共用会让部署自己的重算被自己挡住。
#
# 用 `-w` 等而不是 `-n` 跳过:两个调用方产出的是同一份东西,等前一轮跑完再跑
# 一遍是幂等的;而跳过会让「引擎换了要立刻重算」这条(DEC-099)悄悄落空。
# 900 秒 = 一轮的上限量级;真等不到就退出并留一行日志,让下一轮 cron 兜底。
exec 7>/run/lock/futures-smart-money.lock
if ! flock -w 900 7; then
  echo "[smart-money] 另一轮还在跑,等了 900 秒仍拿不到锁,本轮退出" >&2
  exit 0
fi
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
# AU/AG 给金银引擎;LH/FG/SA/JD/JM 给合计流向引擎。都读同样的两张表,只是品种不同。
# 加品种时**这里和 FLOW_CODES 要一起改**——只改一边不报错,只是那个品种没数据。
for INST in AU AG LH FG SA JD JM; do
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
  # **权限写死 644,不许靠继承 umask**(2026-08-20 事故):这个脚本既被 cron 调
  # (umask 022 → 644,nginx 读得到),也被部署脚本调(umask 077 → 600,nginx 403)。
  # DEC-099 让部署自动重算之后,第一次由部署产出的信号就把整个机构资金页面打成了
  # 403 —— 修 A 问题引入 B 问题。`install -m 644` 把模式钉死,与谁调用无关。
  install -m 644 "$TMP/signals.json" "$WEB/signals.json.new"
  mv "$WEB/signals.json.new" "$WEB/signals.json"
  echo "[smart-money] 已更新 $WEB/signals.json"
else
  echo "[smart-money] 引擎无输出,保留上一版 signals.json" >&2
  exit 1
fi

# ---- 合计流向品种(生猪/玻璃/纯碱):独立引擎、每品种独立产物 ----
# 与金银**刻意分开跑**:信号形态不同(合计流向 vs 逐家共振),两条链失败也各自
# 隔离——它们挂了不该让金银信号跟着不更新,所以这一段不带 set -e 的传染性,
# 失败只告警并保留上一版 JSON。引擎内部也按品种各跑各的:一个品种挂了不影响其余。
if [ -f "$ROOT/hog_money.py" ]; then
  echo "[flow] 计算生猪/玻璃/纯碱/鸡蛋/焦煤信号…"
  if docker run --rm       -v "$ROOT:/work"       -e ENGINE_SOURCE=csv       -e CSV_DIR=/work/tmp       -e FLOW_OUT_DIR=/work/tmp       -e FLOW_CODES=LH,FG,SA,JD,JM       -e PYTHONIOENCODING=utf-8       --entrypoint python "$IMAGE" /work/hog_money.py; then
    for f in hog_signals.json fg_signals.json sa_signals.json jd_signals.json jm_signals.json pair_fgsa.json; do
      if [ -s "$TMP/$f" ]; then
        # 同上:模式写死,不靠继承的 umask。
        install -m 644 "$TMP/$f" "$WEB/$f.new"
        mv "$WEB/$f.new" "$WEB/$f"
        echo "[flow] 已更新 $WEB/$f"
      else
        echo "[flow] $f 无输出,保留上一版" >&2
      fi
    done
  else
    echo "[flow] 引擎失败,保留上一版信号(不影响金银)" >&2
  fi
else
  echo "[flow] 未安装 hog_money.py,跳过" >&2
fi

# 收尾自检:产出必须是**别人读得到**的。写完不验,下一次 umask 变了又是一次
# 静默的 403 —— 页面报「请确认信号引擎已运行」,而引擎明明跑得好好的。
for f in "$WEB"/*.json; do
  test -f "$f" || continue
  perm=$(stat -c '%a' "$f")
  case "$perm" in
    644|664|666) ;;
    *) echo "[smart-money] $f 权限 $perm,nginx 读不到,已改成 644" >&2
       chmod 644 "$f" ;;
  esac
done

rm -f "$TMP"/*.csv
