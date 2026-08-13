-- 把采集器写出的交易日历 CSV 直接装进 trading_calendar_versions / _days。
--
-- 谁在读:自由价差的「散户可交易窗口」引擎。它要判断某个合约在某天能不能交易,
-- 靠的就是这份日历;日历缺了会退回「交割月前月最后一个非周末日」的近似算法
-- (fallback_retail_deadline),那是个粗略兜底,不是正确答案。
--
-- 与导入通道的区别:通道要先造血缘记录、导入批次、来源目录才写得进这两张表
-- (迁移 202608130002 已解绑)。直灌只写业务字段。
--
-- 幂等:版本按 (workspace, 交易所, 版本号) upsert,日期按 (workspace, 版本, 交易日)
-- upsert。同一天重复跑结果一致。

-- **路径写死,不用 psql 变量。**
--
-- `\copy` 是客户端元命令,不做变量插值:写 `\copy t from :'csv_path'` 时它把
-- `:'csv_path'` 当成字面文件名,报 `:: No such file or directory`——**而且这个错
-- 不会中断执行**,psql 继续往下跑,最后 commit 一个什么都没装的空事务。
-- 2026-08-13 首次试跑正是如此:五个 CSV 全部「装载成功」,库里一行没多,
-- 前后计数完全一样才发现。普通 SQL 里 `:'var'` 能用、元命令里不能,
-- 这个差别不看输出根本不知道。
--
-- 所以路径固定,由调用方把 CSV 拷到这里(run-collector.sh 里的 docker cp)。

\set ON_ERROR_STOP on

begin;

-- 列顺序必须与 collector 的 CALENDAR_FIELDS 逐一对应(normalize.py)。
create temp table calendar_stage (
    exchange_code text,
    calendar_version text,
    effective_from date,
    trade_date date,
    is_trading_day boolean,
    day_session_json text,
    night_session_json text,
    source_record_ref text
);

\copy calendar_stage from '/tmp/direct.csv' with (format csv, header true, null '')

create temp table ws as select id from workspaces order by created_at limit 1;

-- 日历版本。source_id / created_by / source_record_id 三列留空:直灌是机器行为,
-- 没有「哪个来源目录」也没有「谁创建的」,编一个反而是假信息。
insert into trading_calendar_versions (
    id, workspace_id, exchange_id, version, source_id, effective_from, created_by, source_record_id)
select gen_random_uuid(), w.id, e.id, s.calendar_version, null, s.effective_from, null, null
  from (select distinct exchange_code, calendar_version, min(effective_from) as effective_from
          from calendar_stage
         where calendar_version is not null and effective_from is not null
         group by exchange_code, calendar_version) s
  cross join ws w
  join exchanges e on e.workspace_id = w.id and e.code = s.exchange_code
on conflict (workspace_id, exchange_id, version) do nothing;

-- 日历天。
insert into trading_calendar_days (
    workspace_id, calendar_version_id, trade_date, is_trading_day,
    day_session_json, night_session_json,
    source_import_batch_id, source_row_number, source_record_id)
select w.id, v.id, s.trade_date, s.is_trading_day,
       -- 盘口时段是 jsonb 列。采集器给的是字符串,空值要落成 '{}' 而不是 null:
       -- 表上有 jsonb_typeof(...) = 'object' 的 check,null 进不去。
       coalesce(nullif(s.day_session_json, '')::jsonb, '{}'::jsonb),
       coalesce(nullif(s.night_session_json, '')::jsonb, '{}'::jsonb),
       null, null, null
  from (select distinct on (exchange_code, calendar_version, trade_date)
               exchange_code, calendar_version, trade_date, is_trading_day,
               day_session_json, night_session_json
          from calendar_stage
         where trade_date is not null and is_trading_day is not null
         order by exchange_code, calendar_version, trade_date) s
  cross join ws w
  join exchanges e on e.workspace_id = w.id and e.code = s.exchange_code
  join trading_calendar_versions v
    on v.workspace_id = w.id and v.exchange_id = e.id and v.version = s.calendar_version
on conflict (workspace_id, calendar_version_id, trade_date) do update set
    is_trading_day = excluded.is_trading_day,
    day_session_json = excluded.day_session_json,
    night_session_json = excluded.night_session_json;

commit;

select '交易日历直灌' as 来路,
       (select count(*) from trading_calendar_versions) as 版本数,
       (select count(*) from trading_calendar_days) as 日期数,
       (select max(trade_date) from trading_calendar_days) as 最新交易日;
