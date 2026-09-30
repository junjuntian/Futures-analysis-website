#!/usr/bin/env bash
# 两条采集链完成当日席位/行情入库后调用 --dispatch；齐备即独立启动引擎。
# 固定时刻的 run-smart-money cron 保留，负责补采和盘后修订。
set -euo pipefail

mode=${1:-}
trade_date=${2:-}
if [[ "$mode" != --dispatch && "$mode" != --run ]] ||
   [[ ! "$trade_date" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "用法: $0 --dispatch|--run YYYY-MM-DD" >&2
  exit 2
fi

# 只对上海时区的当天触发。补历史日和休市日交给原有补跑机制。
if [[ "$trade_date" != "$(TZ=Asia/Shanghai date +%F)" ]]; then
  echo "[smart-money-ready] $trade_date 不是今天，跳过"
  exit 0
fi

state_dir=${SMART_MONEY_READY_STATE_DIR:-/var/lib/futures-platform/smart-money/ready}
runner=${SMART_MONEY_READY_RUNNER:-/usr/local/sbin/run-smart-money}
ready_script=${SMART_MONEY_READY_SCRIPT:-/usr/local/sbin/run-smart-money-on-ready}
marker="$state_dir/$trade_date.done"
log_file=${SMART_MONEY_READY_LOG:-/var/log/futures-smart-money.log}
lock_file=${SMART_MONEY_READY_LOCK:-/run/lock/futures-smart-money-ready.lock}

is_ready() {
  local result
  # 与首页 freshness 判据一致：四所真实逐合约席位、五所行情；按 workspace
  # 分组，不能把不同空间的数据拼成一次「齐备」。INE 按设计不公布席位。
  if ! result=$(docker exec futures-analysis-platform-postgres-1 \
    psql -U futures_app -d futures_platform -tA -v ON_ERROR_STOP=1 -c "
      with seats as (
        select workspace_id, count(distinct exchange) as n
          from seat_history
         where trade_date = '$trade_date' and not is_variety_total
           and source <> 'reboard_inferred'
           and exchange in ('CFFEX','CZCE','DCE','SHFE')
         group by workspace_id
      ), prices as (
        select workspace_id, count(distinct exchange) as n
          from price_history
         where trade_date = '$trade_date'
           and exchange in ('CFFEX','CZCE','DCE','INE','SHFE')
         group by workspace_id
      ), ready as (
        select seats.workspace_id from seats join prices using (workspace_id)
         where seats.n = 4 and prices.n = 5
      ), codes(instrument) as (
        values ('AU'), ('AG'), ('LH'), ('FG'), ('SA'), ('JD'),
               ('JM'), ('IH'), ('I'), ('FU'), ('AP')
      ), flags as (
        select ready.workspace_id, codes.instrument,
               exists (
                 select 1 from price_history p
                  where p.workspace_id = ready.workspace_id
                    and p.trade_date = '$trade_date'
                    and p.instrument = codes.instrument
                    and p.settlement_price > 0
                    and p.open_interest is not null
               ) as price_ok,
               exists (
                 select 1 from seat_history s
                  where s.workspace_id = ready.workspace_id
                    and s.trade_date = '$trade_date'
                    and s.instrument = codes.instrument
                    and not s.is_variety_total
                    and s.source <> 'reboard_inferred'
                    and s.rank_type in ('long', 'short')
               ) as seat_ok
          from ready cross join codes
      )
      select workspace_id::text || ':' ||
             string_agg(instrument || case when price_ok then '1' else '0' end ||
                        case when seat_ok then '1' else '0' end,
                        ',' order by instrument)
        from flags group by workspace_id order by workspace_id limit 1"); then
    echo "[smart-money-ready] $trade_date 齐备查询失败" >&2
    return 2
  fi
  input_fingerprint=$result
  [[ -n "$input_fingerprint" ]]
}

if [[ "$mode" == --dispatch ]]; then
  ready_status=0
  is_ready || ready_status=$?
  case "$ready_status" in
    0) ;;
    1) echo "[smart-money-ready] $trade_date 尚未五所齐，等待后续采集"; exit 0 ;;
    *) exit 1 ;;
  esac
  [[ ! -f "$marker" || $(<"$marker") != "$input_fingerprint" ]] || exit 0
  # 由 systemd 管理独立进程，不继承采集脚本的维护锁；采集可继续跑基差/监控。
  # 唯一 unit 名允许两条采集链近乎同时报齐；--run 内的锁和标记负责去重。
  unit="futures-smart-money-ready-${trade_date//-/}-$(date +%s)-$$"
  systemd-run --collect --unit="$unit" "$ready_script" --run "$trade_date"
  echo "[smart-money-ready] $trade_date 已立即启动 $unit"
  exit 0
fi

exec >>"$log_file" 2>&1
mkdir -p "$state_dir"
exec 6>"$lock_file"
flock -w 900 6
ready_status=0
is_ready || ready_status=$?
case "$ready_status" in
  0) ;;
  1) echo "[smart-money-ready] $trade_date 启动时数据不再齐备，跳过"; exit 0 ;;
  *) exit 1 ;;
esac
[[ ! -f "$marker" || $(<"$marker") != "$input_fingerprint" ]] || {
  echo "[smart-money-ready] $trade_date 输入未变化，跳过"
  exit 0
}
echo "[smart-money-ready] $trade_date 五所齐且输入有更新，立即计算"
"$runner"
printf '%s\n' "$input_fingerprint" >"$marker.tmp.$$"
mv "$marker.tmp.$$" "$marker"
echo "[smart-money-ready] $trade_date 完成"
