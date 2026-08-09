begin;

-- What one point of price movement is worth on one lot.
--
-- This is not the contract size, and conflating the two is wrong for any
-- instrument whose quote unit differs from its trading unit. Eggs are exactly
-- that case: the contract is 5 tonnes but the price is quoted per 500kg, so a
-- one-yuan move is worth 10 yuan a lot, not 5. Seven of the eight varieties
-- under collection happen to have the two coincide, which is precisely what
-- makes the eighth dangerous — a profit computed from contract_multiplier
-- looks entirely normal and is out by a factor of two.
--
-- contract_multiplier keeps its own meaning (the trading unit, 5 tonnes for
-- eggs). Profit and loss must use this column and never that one.
alter table instruments
    add column price_multiplier numeric(20, 8);

alter table instruments
    add constraint instruments_price_multiplier_positive check (
        price_multiplier is null or price_multiplier > 0
    );

-- Seeded from the exchanges' published contract specifications, which the
-- operator supplied on 2026-08-09. Each value is cross-checked two ways: the
-- exchange's own "一手合约最小波动 ÷ 最小变动价位", and independently by
-- turnover / (volume x settlement) computed from published daily files, which
-- agrees to four decimal places on every product where both are available.
--
-- Matched on the instrument code within whichever workspace holds it, so a
-- fresh database that has not yet collected a catalog simply updates nothing.
update instruments set price_multiplier = spec.multiplier
  from (values
    ('JM', 60::numeric),   -- 60 吨/手, 元/吨,      0.5 元 -> 30 元/手
    ('JD', 10::numeric),   -- 5 吨/手,  元/500千克, 1 元   -> 10 元/手  (quote unit differs)
    ('LH', 16::numeric),   -- 16 吨/手, 元/吨,      5 元   -> 80 元/手
    ('FG', 20::numeric),   -- 20 吨/手, 元/吨,      1 元   -> 20 元/手
    ('SA', 20::numeric),   -- 20 吨/手, 元/吨,      1 元   -> 20 元/手
    ('AP', 10::numeric),   -- 10 吨/手, 元/吨,      1 元   -> 10 元/手
    ('AU', 1000::numeric), -- 1000 克/手, 元/克,    0.02 元 -> 20 元/手
    ('AG', 15::numeric)    -- 15 千克/手, 元/千克,  1 元   -> 15 元/手
  ) as spec(code, multiplier)
 where upper(instruments.code) = spec.code;

insert into schema_versions (version, description)
values ('202608100003', 'Instrument price multiplier distinct from contract size')
on conflict (version) do nothing;

commit;
