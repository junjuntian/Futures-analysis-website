#!/usr/bin/env bash
# 定时任务自检:今天(或指定日)的五条 cron 是不是真的自己跑了,而且真的写了东西。
#
# 用法:cron-selfcheck.sh [YYYY-MM-DD]   默认今天(北京时区)
#
# 为什么需要它:cron "跑了"和"干成了"是两回事。装好 cron 之后光看
# journalctl 有没有触发记录不够——脚本可能起来了、上游拒了、库里一行没多,
# 日志里还全是 INFO。这里一律以**库里的水位**和**产物的时间戳**判定。
set -Eeuo pipefail

DAY=${1:-$(TZ=Asia/Shanghai date +%F)}
PG=$(docker ps -qf name=postgres | head -1)
test -n "$PG"

q() { docker exec "$PG" psql -X -U futures_app -d futures_platform -Atqc "$1"; }
ok() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
no() { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
FAIL=0

printf '\n\033[1m定时任务自检 %s(北京)\033[0m\n' "$DAY"
printf '现在 %s 北京 / %s UTC\n\n' "$(TZ=Asia/Shanghai date '+%H:%M')" "$(date -u '+%H:%M')"

# ---- 1. cron 本身触发过吗 ----
printf '\033[1m一、cron 触发记录\033[0m\n'
for job in run-futures-collector run-official-seats run-smart-money; do
  n=$(journalctl -u cron --since "$DAY 00:00" --no-pager 2>/dev/null | grep -c "$job" || true)
  if [ "$n" -gt 0 ]; then ok "$job 被 cron 触发 $n 次"; else no "$job 今天没有触发记录"; fi
done

# ---- 2. 数据真的进库了吗(唯一可信判据) ----
printf '\n\033[1m二、库里的水位(判定成败看这里,不看退出码)\033[0m\n'
seat_today=$(q "select count(*) from seat_history where trade_date = date '$DAY'")
price_today=$(q "select count(*) from price_history where trade_date = date '$DAY'")
if [ "$seat_today" -gt 0 ]; then ok "席位 $DAY 已入库 $seat_today 行"; else no "席位 $DAY 零行"; fi
if [ "$price_today" -gt 0 ]; then ok "行情 $DAY 已入库 $price_today 行"; else no "行情 $DAY 零行"; fi

printf '\n  分来源:\n'
q "select '    '||source||' '||count(*)||' 行' from seat_history
   where trade_date = date '$DAY' group by source order by 1"

# 官方席位带增减量,机构资金引擎靠它;没有 change 信号整体归零
chg=$(q "select count(*) from seat_history where trade_date = date '$DAY'
         and source in ('shfe_official','czce_official') and change is not null")
if [ "$chg" -gt 0 ]; then ok "官方席位带增减量 $chg 行(引擎 ΔNet 可算)"; else no "官方席位无增减量,引擎信号会归零"; fi

# ---- 3. 引擎产物 ----
printf '\n\033[1m三、机构资金引擎\033[0m\n'
SIG=/opt/futures-platform/smart-money/web/signals.json
if [ -r "$SIG" ]; then
  dd=$(python3 -c "import json;print(json.load(open('$SIG'))['data_date'])")
  gen=$(python3 -c "import json;print(json.load(open('$SIG'))['generated_at'][:16])")
  grp=$(python3 -c "import json;print(len(json.load(open('$SIG'))['rules']['group']))")
  if [ "$dd" = "$DAY" ]; then ok "signals.json 已算到 $dd(生成于 $gen,信号组 $grp 家)"
  else no "signals.json 只到 $dd,不是 $DAY(生成于 $gen)"; fi
else
  no "signals.json 不存在"
fi

# ---- 4. 异地备份(23:40 才跑,早于此时不算失败) ----
printf '\n\033[1m四、异地备份\033[0m\n'
now_hm=$(TZ=Asia/Shanghai date +%H%M)
if [ "$now_hm" -lt 2345 ]; then
  printf '  \033[33m·\033[0m 未到 23:40,跳过\n'
else
  line=$(grep "OFFSITE_BACKUP_OK" /var/log/futures-offsite-backup.log 2>/dev/null | tail -1 || true)
  if [ -n "$line" ]; then ok "备份成功:$line"; else no "备份日志里没有 OFFSITE_BACKUP_OK"; fi
fi

printf '\n'
if [ "$FAIL" -eq 0 ]; then
  printf '\033[32m全部通过。\033[0m\n\n'
else
  printf '\033[31m%d 项未通过。\033[0m 排查:\n' "$FAIL"
  printf '  tail -40 /var/log/futures-collector.log\n'
  printf '  tail -20 /var/log/futures-official-seats.log\n'
  printf '  tail -20 /var/log/futures-smart-money.log\n\n'
  exit 1
fi
