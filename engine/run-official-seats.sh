#!/usr/bin/env bash
# 每日官方席位增量采集(SHFE + CZCE)。
#
# 为什么存在:collector(akshare_v1)的席位数据集不含「增减量」字段,而官方
# 逐日文件带完整 change。机构资金引擎的 ΔNet 依赖 change——没有它信号全为 0。
# 官方回填(2026-08-10)只是一次性,这里把同一套已实战的采集/解析/灌库链
# 变成每日增量。官方行入库后,同日的 akshare_v1 席位行即为冗余,一并清除,
# 免得任何不做来源去重的下游把 ΔNet 聚合成 0(2026-08-11 实测踩过)。
#
# 幂等:fetch 对已有文件跳过;灌库是 upsert;删除按 (日期, 来源) 定点。
set -euo pipefail

cd /opt/futures-platform
PG=futures-analysis-platform-postgres-1
SINCE=$(date -d "7 days ago" +%F)   # 7 天回看窗,补节假日与偶发漏采
TODAY=$(TZ=Asia/Shanghai date +%F)

echo "[official-seats] $(date '+%F %T') 采集 $SINCE ~ $TODAY"
# 回看窗内的文件全部强制重取。fetch 对已存在文件跳过,而白天抓到的是盘中快照
# (SHFE kx.dat 收盘价与结算价为空),不删就永远是那一版。
#
# 原来只删「今天」,而且删错了:文件名是 2026-08-12.dat,变量却按 %Y%m%d 生成
# 20260812,glob `*20260812*` 从来没匹配上任何文件——**强制重取一直是空操作**。
# 2026-08-11 的 SHFE 文件正是北京 13:57 盘中抓的,收盘价全空,被下面的
# 「盘中快照不入库」过滤挡掉,当天行情于是只剩没有持仓量的 akshare 行;
# 引擎 main_contract() 首行就 dropna(open_interest),整个 08-11 被丢弃,
# 机构资金页的数据停在 08-10。运营者 2026-08-12 问「为什么显示 0」时一路查到这里。
#
# 只删当天治不了根:任何一天若晚间那趟没跑成,它的盘中版就冻结在归档里。
# 整窗重取一次 16 个小文件,代价可以忽略,换掉的是一整类问题。
for offset in $(seq 0 7); do
  stamp=$(date -d "$offset days ago" +%F)
  rm -f exchange-raw/czce/market/*"$stamp"* exchange-raw/czce/seats/*"$stamp"* \
        exchange-raw/shfe/market/*"$stamp"* exchange-raw/shfe/seats/*"$stamp"*
done
python3 fetch_exchange.py czce "$SINCE" "$TODAY"
python3 fetch_exchange.py shfe "$SINCE" "$TODAY"

echo "[official-seats] 解析增量"
python3 to_csv.py --what czce --since "$SINCE"
python3 to_csv.py --what shfe --since "$SINCE"

echo "[official-seats] 灌库"
for f in price_czce price_shfe seat_czce seat_shfe; do
  docker cp "load/$f.csv" "$PG:/tmp/$f.csv"
done
docker exec -i "$PG" psql -U futures_app -d futures_platform -v ON_ERROR_STOP=1 <<'EOF'
create temp table p_stage (like price_history);
alter table p_stage drop column id, drop column workspace_id, drop column loaded_at;
\copy p_stage from '/tmp/price_czce.csv' with (format csv, header true, null '')
\copy p_stage from '/tmp/price_shfe.csv' with (format csv, header true, null '')
insert into price_history (id, workspace_id, exchange, instrument, contract, trade_date,
  open_price, high_price, low_price, close_price, settlement_price, prev_settlement_price,
  volume, volume_basis, turnover, open_interest, open_interest_change, source)
select gen_random_uuid(), (select workspace_id from market_prices group by 1 order by count(*) desc limit 1), s.*
  from p_stage s
 -- 盘中快照(SHFE kx.dat 白天就能取到,收盘/结算为空)不入库;
 -- 收盘后的完整文件会在晚间 cron 覆盖原始文件并正常入库。
 where s.close_price is not null or s.settlement_price is not null
on conflict (workspace_id, contract, trade_date, source) do update set
  open_price=excluded.open_price, high_price=excluded.high_price, low_price=excluded.low_price,
  close_price=excluded.close_price, settlement_price=excluded.settlement_price,
  prev_settlement_price=excluded.prev_settlement_price, volume=excluded.volume,
  turnover=excluded.turnover, open_interest=excluded.open_interest,
  open_interest_change=excluded.open_interest_change, loaded_at=now();

create temp table s_stage (like seat_history);
alter table s_stage drop column id, drop column workspace_id, drop column loaded_at;
\copy s_stage from '/tmp/seat_czce.csv' with (format csv, header true, null '')
\copy s_stage from '/tmp/seat_shfe.csv' with (format csv, header true, null '')
insert into seat_history (id, workspace_id, exchange, instrument, contract, is_variety_total,
  variety_total_is_computed, trade_date, rank_type, rank, member, quantity, change, source)
select gen_random_uuid(), (select workspace_id from market_prices group by 1 order by count(*) desc limit 1), s.*
  from (select distinct on (trade_date, exchange, instrument, contract, is_variety_total,
                            rank_type, member, source) *
          from s_stage) s
on conflict (workspace_id, trade_date, exchange, instrument, contract, is_variety_total,
             rank_type, member, source) do update set
  rank=excluded.rank, quantity=excluded.quantity, change=excluded.change, loaded_at=now();

-- 官方已到的日子,akshare 的席位行是冗余且 change 为空,清除。
-- 只清「官方同日已有数据」的行:官方哪天缺了,akshare 行保留兜底。
delete from seat_history a
 where a.source = 'akshare_v1'
   and exists (select 1 from seat_history o
                where o.source in ('shfe_official','czce_official')
                  and o.trade_date = a.trade_date
                  and o.instrument = a.instrument
                  and o.workspace_id = a.workspace_id);

select 'seat 今日官方行' as k, count(*) from seat_history
 where source like '%official' and trade_date >= current_date - 3
union all
select 'seat 残留 akshare 行', count(*) from seat_history where source='akshare_v1';
EOF
echo "[official-seats] 完成"
