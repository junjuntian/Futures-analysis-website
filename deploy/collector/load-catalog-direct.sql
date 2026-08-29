-- 把采集器写出的品种目录 CSV 直接装进参考表(exchanges / instruments / contracts)。
--
-- 这三张表存的是页面天天读的东西:交易所、品种中文名与**点值**、合约与交割月。
-- 点值算错鸡蛋盈亏就差一倍(合约 5 吨但报价按 500 千克),所以这条路上任何一步
-- 静默失败都比报错糟。
--
-- 与导入通道的区别:通道要先造血缘记录、导入批次、来源目录才能写这三张表
-- (迁移 202608130002 已解绑)。直灌只写业务字段,血缘列留空。
--
-- 幂等:按各表的业务身份 upsert。重复跑只刷新同一批行,不会造出重复品种。
--
-- 顺序不能乱:交易所 → 品种 → 合约,后者的外键指着前者。

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

-- 列顺序必须与 collector 的 CATALOG_FIELDS 逐一对应(normalize.py)。
-- 错位不报错,只会把品种名灌进货币代码列——而错位的数据看起来完全正常。
create temp table catalog_stage (
    exchange_code text,
    exchange_name text,
    timezone text,
    instrument_code text,
    instrument_name text,
    currency_code text,
    contract_multiplier numeric,
    price_tick numeric,
    contract_code text,
    delivery_month text,
    listed_at date,
    expires_at date,
    source_record_ref text
);

\copy catalog_stage from '/tmp/direct.csv' with (format csv, header true, null '')

-- 只装这个 workspace。单人自用,库里只有一个;写死会在将来多一个空间时静默
-- 灌错地方,所以按「最早创建的那个」取——它就是运营者自己那个。
create temp table ws as select id from workspaces order by created_at limit 1;

-- 交易所。
insert into exchanges (id, workspace_id, code, name, timezone, source_record_id)
select gen_random_uuid(), w.id, s.exchange_code, s.exchange_name, s.timezone, null
  from (select distinct exchange_code, exchange_name, timezone from catalog_stage
         where exchange_code is not null and exchange_name is not null) s
  cross join ws w
on conflict (workspace_id, code) do update set
    name = excluded.name, timezone = excluded.timezone, updated_at = now();

-- 品种。**不覆盖 price_multiplier**:那一列由迁移按交易所公布的合约规格 seed,
-- 采集器给的 contract_multiplier 是交易单位(鸡蛋 5),与点值(10)不是一回事,
-- 抄过来会让鸡蛋盈亏差一倍。这里只更新名称与规格,点值原样保留。
insert into instruments (
    id, workspace_id, exchange_id, code, name, currency_code,
    contract_multiplier, price_tick, source_record_id)
select gen_random_uuid(), w.id, e.id, s.instrument_code, s.instrument_name,
       coalesce(nullif(s.currency_code, ''), 'CNY'),
       s.contract_multiplier, s.price_tick, null
  from (select distinct on (exchange_code, instrument_code)
               exchange_code, instrument_code, instrument_name, currency_code,
               contract_multiplier, price_tick
          from catalog_stage
         where instrument_code is not null and instrument_name is not null
         order by exchange_code, instrument_code) s
  cross join ws w
  join exchanges e on e.workspace_id = w.id and e.code = s.exchange_code
on conflict (workspace_id, exchange_id, code) do update set
    name = excluded.name,
    currency_code = excluded.currency_code,
    contract_multiplier = excluded.contract_multiplier,
    price_tick = excluded.price_tick,
    updated_at = now();

-- 新品种的点值兜底(2026-08-28)。instruments 行由上面这段按采集到的目录创建,
-- 而 price_multiplier 由迁移 seed —— **新加的品种(铁矿石 I)在迁移跑完之后才
-- 第一次出现**,于是点值永远是 null,净持仓页会因此不算盈亏(设计上宁可少一条
-- 曲线也不乘错倍数)。这里按同一份合约规格补,只补空值、不覆盖已有值。
-- 规格来源与 rust/migrations/202608100003 / 202608280001 同,改一处要改三处。
update instruments set price_multiplier = spec.m, updated_at = now()
  from (values ('I', 100::numeric),      -- 铁矿石 100 吨/手
               ('IH', 300::numeric),     -- 上证50 300 元/点
               ('SC', 1000::numeric)     -- 原油 1000 桶/手
       ) as spec(code, m)
 where upper(instruments.code) = spec.code and instruments.price_multiplier is null;

-- 合约。
insert into contracts (
    id, workspace_id, instrument_id, code, delivery_month, listed_at, expires_at, source_record_id)
select gen_random_uuid(), w.id, i.id, upper(s.contract_code),
       nullif(s.delivery_month, ''), s.listed_at, s.expires_at, null
  from (select distinct on (exchange_code, instrument_code, contract_code)
               exchange_code, instrument_code, contract_code, delivery_month, listed_at, expires_at
          from catalog_stage
         where contract_code is not null
         order by exchange_code, instrument_code, contract_code) s
  cross join ws w
  join exchanges e on e.workspace_id = w.id and e.code = s.exchange_code
  join instruments i on i.workspace_id = w.id and i.exchange_id = e.id and i.code = s.instrument_code
 -- 交割月的格式受表上的 check 约束管着('YYYY-MM'),形状不对的行不是错的,
 -- 只是不属于这张表——放进来会让整个事务回滚,那才是真的错。
 where s.delivery_month is null or s.delivery_month = '' or s.delivery_month ~ '^[0-9]{4}-[0-9]{2}$'
on conflict (workspace_id, instrument_id, code) do update set
    delivery_month = excluded.delivery_month,
    listed_at = excluded.listed_at,
    expires_at = excluded.expires_at,
    updated_at = now();

commit;

select '品种目录直灌' as 来路,
       (select count(*) from exchanges) as 交易所,
       (select count(*) from instruments) as 品种,
       (select count(*) from contracts) as 合约,
       (select count(*) from instruments where price_multiplier is not null) as 有点值的品种;
