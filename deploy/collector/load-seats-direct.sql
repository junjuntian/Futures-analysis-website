-- 把采集器写出的席位 CSV 直接装进 seat_history。
--
-- 为什么不走导入通道
-- ------------------
-- 通道(上传→暂存→逐行校验→冲突检测→确认→血缘→canonical→投影→宽表)是为
-- 「人工上传文件、需要预览和回滚」建的。运营者 2026-08-13 说明那个入口服务的
-- AI 分析功能早已取消,他从没手工导过数据——而每天的自动采集一直被迫走这条
-- 七层流水线。生产实测:席位一项就占了 665,940 行导入中间产物(全部的 88%),
-- 这些暂存行、变更记录、血缘加起来和最终业务数据一样大,且每天都在长。
--
-- 采集器的数据不需要那套:格式是它自己规范化的、来源是白名单里的公开接口、
-- 错了明天重采一遍就有。它需要的只是「把这批行写进宽表」。
--
-- 这条路不是新发明:上期所与郑商所的席位早就这么灌了(engine/run-official-seats.sh),
-- 大商所行情也是(load-dce-daily.sql)。这里补上的是最后一块——大商所席位。
--
-- 幂等:按 seat_history 的业务身份 upsert,重复跑只刷新同一批行。

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

-- 列顺序必须与 collector 的 SEAT_FIELDS 逐一对应(normalize.py)。
-- 错位不会报错,只会把持仓量灌进名次列——而错位的数据看起来完全正常。
create temp table seat_stage (
    exchange_code text,
    contract_code text,
    trade_date date,
    seat_name text,
    rank_type text,
    rank numeric,
    volume numeric,
    long_position numeric,
    short_position numeric,
    -- 增减量:可负可零。东财源自 2026-08-17 起给这一列(契约 seat_positions_v1
    -- 同批扩列);不给的源留空。\copy 按列序对位,这里的顺序必须与
    -- normalize.py 的 SEAT_FIELDS 一致。
    change numeric,
    source_record_ref text
);

\copy seat_stage from '/tmp/direct.csv' with (format csv, header true, null '')

-- 装进来的必须就是要装的那一天。
--
-- 2026-08-13:采集器采回 08-13 的 5,940 行,这里却装了 /tmp/direct.csv 上遗留的
-- 08-12 那份(调用方把文件 cp 到了另一个名字),把昨天的数据又 upsert 一遍。
-- 全程零报错,库里一行 08-13 都没有。行数、退出码都正常——唯一能看出不对的,
-- 是「装进去的是哪一天」,而当时没有任何一步在看它。
--
-- psql 的变量插值**不进入美元引用块**:写在 $$ ... $$ 里的 :'expect_date' 会被
-- 当成字面量,报 `syntax error at or near ":"`。2026-08-13 我加这道守卫时就是这么
-- 写的,部署当场失败并整轮回滚——加了守卫却没跑过它,和它要防的问题同一类。
--
-- 所以先用一条普通 SQL 把值放进会话变量(这里插值是生效的),块里再读出来。
select set_config('futures.expect_date', :'expect_date', false);

do $$
declare
    staged date;
    expected date := current_setting('futures.expect_date')::date;
begin
    select max(trade_date) into staged from seat_stage;
    if staged is null then
        raise exception '席位 CSV 是空的:一行数据都没有';
    end if;
    if staged <> expected then
        raise exception '装错文件了:CSV 里最新交易日是 %,要装的是 %', staged, expected;
    end if;
end $$;

insert into seat_history (
    id, workspace_id, exchange, instrument, contract, is_variety_total,
    variety_total_is_computed, trade_date, rank_type, rank, member, quantity, change, source,
    updated_at
)
select
    gen_random_uuid(),
    w.id,
    s.exchange_code,
    -- 品种从合约代码取:字母前缀就是品种,与宽表其余来路的写法一致。
    upper(regexp_replace(s.contract_code, '[0-9]+$', '')),
    upper(s.contract_code),
    false,
    false,
    s.trade_date,
    s.rank_type,
    -- 名次可以没有(有些源不给),但不能编。
    nullif(s.rank, 0)::int,
    trim(s.seat_name),
    -- 一行只有一个榜有值,rank_type 决定是哪一列。
    coalesce(s.volume, s.long_position, s.short_position),
    -- 增减照源给的装,不自造。交易所公布的「增减」相对该会员前一日**真实全量仓**
    -- 算;源没给就留空——拿前后两天自己相减凑数,在会员进出前二十那天必然与
    -- 交易所口径对不上。东财源 2026-08-17 起带真增减(掉榜反推靠它续供)。
    s.change,
    :'source_code',
    now()
  from seat_stage s
  cross join (select id from workspaces order by created_at limit 1) w
 where s.contract_code is not null
   and s.trade_date is not null
   and length(trim(coalesce(s.seat_name, ''))) > 0
   and coalesce(s.volume, s.long_position, s.short_position) is not null
   -- 只装八品种。采集器可能带回整个交易所的榜,全灌进来会让席位页的品种
   -- 下拉冒出一堆只有几天历史的品种,点进去是空图——空图比没有这个选项更糟,
   -- 它看起来像是数据坏了。
   and upper(regexp_replace(s.contract_code, '[0-9]+$', ''))
       in ('AU','AG','JD','LH','JM','AP','FG','SA')
   -- 合约代码必须是四位月份的规范形状,否则不属于这张表。
   and upper(s.contract_code) ~ '^[A-Z]{1,2}[0-9]{4}$'
on conflict (workspace_id, trade_date, exchange, instrument, contract,
             is_variety_total, rank_type, member, source) do update set
    rank = excluded.rank,
    quantity = excluded.quantity,
    -- 晚间第二轮采集会把早间没有的增减补上;同一 (身份键, source) 内自我更新,
    -- 不跨源覆盖(source 在唯一键里)。
    change = excluded.change,
    -- 两个时间戳各司其职(2026-08-16,迁移 202608160001):loaded_at=首次
    -- 入库时刻,upsert 不许碰——min(loaded_at) 按(交易日,交易所)聚合就是
    -- 数据源到达时刻画像;updated_at=最近装载触碰时刻,E2E 拿它验"本轮
    -- 真的写过"。首版误让画像挤掉验收语义,当晚被 4A E2E 拦下回滚过一次。
    -- 全部装载路径同一口径:本文件 / load-dce-daily / load_history.py /
    -- run-official-seats.sh。
    updated_at = now();

commit;

select 'seat_history 直灌' as 来路, :'source_code' as 源,
       max(trade_date) as 最新交易日, count(*) as 该源总行数
  from seat_history where source = :'source_code';
