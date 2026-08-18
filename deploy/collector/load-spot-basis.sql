-- 装载现货价与基差 CSV(DEC-074)。由 run-collector.sh 在采集之后调用。
--
-- 与席位/行情的直灌同一套路数:stage 表按 CSV 列序 \copy 进来,再 upsert 进
-- 宽表。**列序必须与 fetch-spot-basis.py 的 FIELDS 一一对应**——\copy 是按位置
-- 匹配的,改一边必须同批改另一边(席位契约扩列时踩过,DEC-066)。

\set ON_ERROR_STOP on

begin;

create temp table stage_spot_basis (
    trade_date date,
    instrument text,
    spot_price numeric,
    near_contract text,
    near_price numeric,
    near_basis numeric,
    near_basis_rate numeric,
    dominant_contract text,
    dominant_price numeric,
    dominant_basis numeric,
    dominant_basis_rate numeric
) on commit drop;

\copy stage_spot_basis from :'csv_path' with (format csv, header true)

insert into spot_basis_history (
    workspace_id, trade_date, instrument, spot_price,
    near_contract, near_price, near_basis, near_basis_rate,
    dominant_contract, dominant_price, dominant_basis, dominant_basis_rate,
    source)
select w.id, s.trade_date, s.instrument, s.spot_price,
       nullif(s.near_contract, ''), s.near_price, s.near_basis, s.near_basis_rate,
       nullif(s.dominant_contract, ''), s.dominant_price, s.dominant_basis,
       s.dominant_basis_rate,
       'shengyishe_v1'
  from stage_spot_basis s
  cross join (select id from workspaces order by created_at limit 1) w
 where s.spot_price > 0
on conflict (workspace_id, trade_date, instrument, source) do update set
    spot_price = excluded.spot_price,
    near_contract = excluded.near_contract,
    near_price = excluded.near_price,
    near_basis = excluded.near_basis,
    near_basis_rate = excluded.near_basis_rate,
    dominant_contract = excluded.dominant_contract,
    dominant_price = excluded.dominant_price,
    dominant_basis = excluded.dominant_basis,
    dominant_basis_rate = excluded.dominant_basis_rate,
    loaded_at = now();

select 'SPOT_BASIS_LOADED' tag, count(*) rows, min(trade_date), max(trade_date)
  from spot_basis_history;

commit;
