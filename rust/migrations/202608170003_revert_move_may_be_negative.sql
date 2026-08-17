-- 修 202608170002 里一条写错的约束:`move` 不是非负的。
--
-- 我当时把 move 想成「最有利那一刻相对起点走了多远」,默认它至少是 0。**不对**:
--   · 低位段 move = 后续最高价差 − 起点。如果历年那一年后续最高价差**仍低于**起点,
--     move 就是负的。
--   · 高位段同理,move = 起点 − 后续最低价差,后续最低仍高于起点时为负。
-- 负的 move 是**有效信息**,意思是「中位年份下压根没回到起点」,和低 hit 率互相印证,
-- 不是脏数据。
--
-- 2026-08-17 部署后跑全量重算(window_days=45)时被 check 当场拦下:
--   LH2609/LH2701 2026-07-31 低位 hit=2/5、move=−45 —— 五年里只有两年曾涨过起点,
--   中位那年的「最有利」比起点还低 45 点。**约束拦得对,错的是约束本身。**
--
-- 为什么上线前没抓到:只读验证脚本把 insert 之后的部分整个截掉了(为了绝不写生产),
-- 于是只检验了计算、没检验落库,值域约束一条都没走到。**教训:只读验证要另配一遍
-- 值域检查,别以为「算得出来」就等于「存得进去」。**
--
-- 顺带给 202608170002 那条 add constraint 补上 drop if exists —— 它当时漏了,重放
-- 迁移会撞「约束已存在」。这里的 drop + add 也顺手把那个状态收拾干净。

begin;

alter table spread_monitor_daily
    drop constraint if exists spread_monitor_daily_revert_pairs_sane,
    add constraint spread_monitor_daily_revert_pairs_sane
        check ((revert_high_hit is null) = (revert_high_n is null)
           and (revert_high_n is null
                or (revert_high_n > 0 and revert_high_hit between 0 and revert_high_n))
           and (revert_low_hit is null) = (revert_low_n is null)
           and (revert_low_n is null
                or (revert_low_n > 0 and revert_low_hit between 0 and revert_low_n))
           -- move 与 drift 都可正可负,只有天数必须为正。
           and (revert_high_days is null or revert_high_days > 0)
           and (revert_low_days is null or revert_low_days > 0));

insert into schema_versions (version, description)
values ('202608170003',
        'Revert stats: move may be negative when the best point across history never reached the anchor')
on conflict (version) do nothing;

commit;
