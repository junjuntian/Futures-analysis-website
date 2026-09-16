-- 装载外盘原油主连 CSV(迁移 202609160001)。由 run-collector.sh 在采集之后调用。
--
-- 与现货基差/席位/行情的直灌同一套路数:stage 表按 CSV 列序 `\copy` 进来,
-- 再 upsert 进正式表。**列序必须与 fetch-overseas.py 的 FIELDS 一一对应**
-- —— `\copy` 是按位置匹配的,改一边必须同批改另一边(DEC-066 的教训)。
--
-- stage 列一律收成 text 再在 insert 里转型:CSV 里的空串代表「上游没有这个数」
-- (OIL 无持仓量、CL 无成交量与结算价),直接声明成 numeric 会让 `\copy` 把空串
-- 当成非法输入整批报错。`nullif(...,'')::numeric` 才能把空串落成 NULL。

\set ON_ERROR_STOP on

begin;

create temp table stage_overseas (
    symbol text,
    trade_date text,
    open_price text,
    high_price text,
    low_price text,
    close_price text,
    settlement_price text,
    volume text,
    open_interest text
) on commit drop;

-- **固定路径,不用 `:'csv_path'`**:`\copy` 是客户端元命令,**不做变量插值**
-- (load-seats-direct.sql 与 load-spot-basis.sql 的注释都写过这条)。
-- 路径与 run-collector.sh 里 docker cp 的目标一一对应,改一边必须改另一边。
\copy stage_overseas from '/tmp/overseas.csv' with (format csv, header true)

insert into overseas_price_daily (
    workspace_id, symbol, trade_date,
    open_price, high_price, low_price, close_price, settlement_price,
    volume, open_interest, source)
select w.id,
       s.symbol,
       s.trade_date::date,
       nullif(s.open_price, '')::numeric,
       nullif(s.high_price, '')::numeric,
       nullif(s.low_price, '')::numeric,
       nullif(s.close_price, '')::numeric,
       nullif(s.settlement_price, '')::numeric,
       nullif(s.volume, '')::numeric,
       nullif(s.open_interest, '')::numeric,
       'sina_foreign_v1'
  from stage_overseas s
  cross join (select id from workspaces order by created_at limit 1) w
 where nullif(s.close_price, '') is not null
on conflict (workspace_id, symbol, trade_date, source) do update set
    open_price = excluded.open_price,
    high_price = excluded.high_price,
    low_price = excluded.low_price,
    close_price = excluded.close_price,
    settlement_price = excluded.settlement_price,
    volume = excluded.volume,
    open_interest = excluded.open_interest,
    updated_at = now();

select 'OVERSEAS_LOADED' tag, symbol, count(*) rows,
       min(trade_date) first_day, max(trade_date) last_day
  from overseas_price_daily
 group by symbol
 order by symbol;

commit;
