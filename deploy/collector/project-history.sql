-- 把每日采集的结果投影进两张历史表。
--
-- 每日采集写的是 market_prices / seat_positions（审计导入通道，外键化、可追溯到
-- 导入批次）。套利页和席位页读的是 price_history / seat_history（回填装载的可读
-- 宽表）。两边并存是 202608100008 里写明的、知情的临时状态——但那时候没人把日更
-- 接过来，结果是回填完的数据停在装载那天不动。这个脚本就是那座桥。
--
-- 幂等：按业务身份 upsert，重复跑只会刷新同一批行。窗口取最近若干天而不是全量，
-- 因为交易所会修正近几日的数据，更早的不会再动。

\set ON_ERROR_STOP on
\set window_days 10

begin;

-- 价格。
--
-- 来源如实记连接器代码（akshare_v1 / eastmoney_dce_quote_v1 …），不冒充回填时
-- 那几个 *_official：那批是从交易所年度文件解析的，这批是每日接口取的，两者
-- 出处不同，混成一个名字以后就分不出哪行来自哪里了。
insert into price_history (
    id, workspace_id, exchange, instrument, contract, trade_date,
    open_price, high_price, low_price, close_price, settlement_price, prev_settlement_price,
    volume, volume_basis, turnover, open_interest, open_interest_change, source
)
select
    gen_random_uuid(),
    m.workspace_id,
    e.code,
    upper(i.code),
    upper(c.code),
    m.trade_date,
    m.open_price, m.high_price, m.low_price, m.close_price, m.settlement_price,
    null,  -- market_prices 没有前结算，不编
    m.volume,
    -- 单边还是双边：用「成交额 ÷（成交量 × 结算价）」比对登记点值实测。
    -- 比值接近点值是单边，接近点值一半是双边。四个输入缺一个就判不了，
    -- 判不了就留空——留空的含义是口径未知，不是单边。
    case
        when m.turnover is null or m.volume is null or m.volume = 0
          or m.settlement_price is null or m.settlement_price = 0
          or i.price_multiplier is null or i.price_multiplier = 0 then null
        when abs(m.turnover / (m.volume * m.settlement_price) - i.price_multiplier)
             <= abs(m.turnover / (m.volume * m.settlement_price) - i.price_multiplier / 2)
            then 'single'
        else 'double'
    end,
    m.turnover,
    null, null,  -- market_prices 没有持仓量与增减
    ds.connector_code
from market_prices m
join contracts c on c.id = m.contract_id and c.workspace_id = m.workspace_id
join instruments i on i.id = c.instrument_id and i.workspace_id = m.workspace_id
join exchanges e on e.id = i.exchange_id
join data_sources ds on ds.id = m.source_id and ds.workspace_id = m.workspace_id
where m.trade_date >= current_date - :window_days
  -- 历史表只收四位月份的合约代码；形状不符的行不是错的，只是不属于这张表。
  and upper(c.code) ~ '^[A-Z]{1,2}[0-9]{4}$'
  -- 只投影两个产品覆盖的那八个品种。market_prices 里有五家交易所六十来个品种，
  -- 全量投进来会让套利页的品种下拉冒出一堆只有三天历史的品种——点进去是空图，
  -- 而空图比没有这个选项更糟，它看起来像是数据坏了。
  and exists (
      select 1 from product_instrument_scope s
       where s.workspace_id = m.workspace_id
         and s.instrument = upper(i.code)
         and s.exchange = e.code
  )
  -- 至少要有一个价格，否则这一行没有在陈述任何事情（历史表的约束也这么写）。
  and (m.close_price is not null or m.settlement_price is not null)
on conflict (workspace_id, contract, trade_date, source) do update set
    open_price = excluded.open_price,
    high_price = excluded.high_price,
    low_price = excluded.low_price,
    close_price = excluded.close_price,
    settlement_price = excluded.settlement_price,
    volume = excluded.volume,
    volume_basis = excluded.volume_basis,
    turnover = excluded.turnover,
    loaded_at = now();

-- 席位。
--
-- seat_positions 一行一个榜（rank_type 决定 volume / long_position / short_position
-- 哪一列有值），正好对上 seat_history 的一行。
--
-- change 留空：seat_positions 没有这一列，而交易所公布的「增减」是相对该会员前一日
-- 的真实持仓算的，不是相对「昨天不在榜上就当 0」。自己拿前后两天相减凑出来的数在
-- 会员进出前二十那天必然与交易所公布的不一致，那就成了一个看起来像官方数字的
-- 自造数。建仓过程页用的持仓成本引擎本来就自己从净持仓变化推加减仓，不依赖这一列。
insert into seat_history (
    id, workspace_id, exchange, instrument, contract, is_variety_total,
    variety_total_is_computed, trade_date, rank_type, rank, member, quantity, change, source
)
select
    gen_random_uuid(),
    sp.workspace_id,
    e.code,
    upper(i.code),
    upper(c.code),
    false,
    false,
    sp.trade_date,
    sp.rank_type,
    sp.rank,
    se.canonical_name,
    coalesce(sp.volume, sp.long_position, sp.short_position),
    null,
    ds.connector_code
from seat_positions sp
join contracts c on c.id = sp.contract_id and c.workspace_id = sp.workspace_id
join instruments i on i.id = c.instrument_id and i.workspace_id = sp.workspace_id
join exchanges e on e.id = i.exchange_id
join seat_entities se on se.id = sp.seat_id and se.workspace_id = sp.workspace_id
join data_sources ds on ds.id = sp.source_id and ds.workspace_id = sp.workspace_id
where sp.trade_date >= current_date - :window_days
  and upper(c.code) ~ '^[A-Z]{1,2}[0-9]{4}$'
  and exists (
      select 1 from product_instrument_scope s
       where s.workspace_id = sp.workspace_id
         and s.instrument = upper(i.code)
         and s.exchange = e.code
  )
  and coalesce(sp.volume, sp.long_position, sp.short_position) is not null
  and length(trim(se.canonical_name)) > 0
on conflict (workspace_id, trade_date, exchange, instrument, contract,
             is_variety_total, rank_type, member, source) do update set
    rank = excluded.rank,
    quantity = excluded.quantity,
    loaded_at = now();

commit;

-- 跑完把两张表的水位报出来，好在日志里一眼看出有没有推进。
select 'price_history' as 表, max(trade_date) as 最新交易日, count(*) as 总行数
  from price_history
union all
select 'seat_history', max(trade_date), count(*) from seat_history;
