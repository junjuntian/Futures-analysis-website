-- 套利监控:加「前一交易日位置」与「历史回归率」。
--
-- 两件事,一条共同的约束:**不许把阈值焊进表里**(202608120001 立下的原则)。
--
-- 一、段首日标记
--
-- 「今天刚进极值」和「已经在极值里待了一百天」是完全不同的两件事。焦煤 2026 年
-- 有 64% 的交易日都在 3% 触发——价差持续创新低,滚动区间天天被刷新,阈值在单边
-- 突破的行情里失去了筛选作用;而连续触发段的中位长度只有 3 日(鸡蛋 2 日),说明
-- 绝大多数段是短的,长段拖着不放才是噪音的来源。
--
-- 但触发与否要到读的时候才按阈值算,所以这里存的**不是**「是不是段首日」,而是
-- 前一交易日的两条轨位置:读时对前一日套同一个阈值,前一日不触发而今天触发,就是
-- 段首日。任何阈值都能在任何一天重判,历史跟着一起变,不会出现新旧口径混在一张
-- 表里的局面。
--
-- 二、历史回归率
--
-- 同样是 3% 触发,鸡蛋和焦煤是两回事:2026-08-17 的全样本扫描里,鸡蛋 445 段极值
-- 有 45% 朝回归方向走,焦煤 68 段只有 29%——后者意味着贴到极值之后继续极端化的
-- 概率是回归的两倍多。页面现在把这两种情况画成同一个红条,读的人无从分辨。
--
-- 统计口径(页面必须标明,别让人以为是「这一组合自己的胜率」):
--
--   · 主体是**月份组合模板**(同品种 + 同月份对 + 同年差,例如鸡蛋 09-01),不是
--     具体合约对。JD2609/JD2701 一辈子只有一个生命周期,极值段可能就两三段,按它
--     自己算出来的比率没有意义;跨年拼起来才有样本。
--
--   · 极值段按**当年轨**划分(该合约对自身截至当日的滚动区间位置),不用页面报警
--     用的合成轨。合成轨要先有历年百分位,而历年轨是逐 (组合,日期) 跑一次
--     percentile_cont 算出来的,推到全历史会从「百来次」变成几万次。这是工程折中,
--     不是口径上更优——因此页面标注写「当年轨口径」,与报警口径可能有出入。
--
--   · 段首日起 **20 个交易日**,价差朝「该回归的方向」走(低位段价差走高、高位段
--     价差走低)即算命中。不足 20 日的段(最近发生的)自然不计入,统计因此永远
--     落后 20 个交易日,这是对的:那些段的结果还没发生。
--
--   · 三档 3% / 5% / 10% 各算一份。只存一档会出现「页面按 3% 报警、底下挂着 10%
--     的回归率」这种对不上的展示,读的人不会注意到脚注。
--
-- 存的是命中数与样本数两个整数,比率留到读时相除。整数没有精度问题;而且界面上
-- 「17 段里 5 段回归」比一个孤零零的 29% 更能让人看出样本有多薄。
--
-- 这些统计是**模板级**的,同一模板下每个具体组合、每一天都存同样的值。冗余是有意
-- 的:这张表每天全量重算,冗余不会漂;拆成单独一张表则要在读路径上多一次 join,
-- 而监控页当初落表就是为了躲开读路径上的现算。
--
-- 全部列可空、无默认值,加列是瞬时的(不重写表)。
--
-- **为什么全篇 if not exists / drop if exists**:2026-08-17 开发期做「事务里跑一遍
-- 再 rollback」的只读验证时,拼接脚本的 grep 转义没生效,compute 脚本自带的
-- `commit;` 没被剥掉,这套 DDL 当场落进了生产库。后果本身可控——列可空无默认、
-- 当时的生产代码不读新列、日更也不会失败——但它让这份迁移在正式部署时必然撞上
-- 「列已存在」。同类教训仓库里已经有一次(concurrently 预建索引的迁移),规则是
-- 同一条:**迁移必须能在已经处于目标状态的库上再跑一遍而不报错**。
-- 约束没有 add constraint if not exists,用 drop if exists + add 达到同样效果。

begin;

alter table spread_monitor_daily
    -- 前一交易日的两轨位置。为空有两种情况:该组合的第一天,或前一日无快照。
    -- 两种都按「无法判定段首日」处理,读时退化成不打新触发标记。
    add column if not exists prev_pair_position numeric,
    add column if not exists prev_years_position numeric,

    -- 低位段:价差贴在区间下端,回归方向是走高。
    add column if not exists revert_low_hit_3 integer,
    add column if not exists revert_low_n_3 integer,
    add column if not exists revert_low_hit_5 integer,
    add column if not exists revert_low_n_5 integer,
    add column if not exists revert_low_hit_10 integer,
    add column if not exists revert_low_n_10 integer,

    -- 高位段:价差贴在区间上端,回归方向是走低。
    add column if not exists revert_high_hit_3 integer,
    add column if not exists revert_high_n_3 integer,
    add column if not exists revert_high_hit_5 integer,
    add column if not exists revert_high_n_5 integer,
    add column if not exists revert_high_hit_10 integer,
    add column if not exists revert_high_n_10 integer;

-- 位置的护栏与现有两列同一套:允许越界(历年轨是百分位区间),但留一个数量级的
-- 余量,算错了(比如除数取反)当场报出来,而不是在页面上画一条看着正常的线。
alter table spread_monitor_daily
    drop constraint if exists spread_monitor_daily_prev_position_sane,
    add constraint spread_monitor_daily_prev_position_sane
        check ((prev_pair_position is null or prev_pair_position between -10 and 11)
           and (prev_years_position is null or prev_years_position between -10 and 11));

-- 命中数与样本数必须成对出现,且 0 <= 命中 <= 样本、样本 > 0。
-- 样本为 0 要存 null 而不是 0:0/0 在界面上会变成「0% 回归率」,那是最坏的一种
-- 错误——看起来像个结论,其实是没有数据。
alter table spread_monitor_daily
    drop constraint if exists spread_monitor_daily_revert_low_sane,
    drop constraint if exists spread_monitor_daily_revert_high_sane,
    add constraint spread_monitor_daily_revert_low_sane
        check ((revert_low_hit_3 is null) = (revert_low_n_3 is null)
           and (revert_low_n_3 is null
                or (revert_low_n_3 > 0 and revert_low_hit_3 between 0 and revert_low_n_3))
           and (revert_low_hit_5 is null) = (revert_low_n_5 is null)
           and (revert_low_n_5 is null
                or (revert_low_n_5 > 0 and revert_low_hit_5 between 0 and revert_low_n_5))
           and (revert_low_hit_10 is null) = (revert_low_n_10 is null)
           and (revert_low_n_10 is null
                or (revert_low_n_10 > 0 and revert_low_hit_10 between 0 and revert_low_n_10))),
    add constraint spread_monitor_daily_revert_high_sane
        check ((revert_high_hit_3 is null) = (revert_high_n_3 is null)
           and (revert_high_n_3 is null
                or (revert_high_n_3 > 0 and revert_high_hit_3 between 0 and revert_high_n_3))
           and (revert_high_hit_5 is null) = (revert_high_n_5 is null)
           and (revert_high_n_5 is null
                or (revert_high_n_5 > 0 and revert_high_hit_5 between 0 and revert_high_n_5))
           and (revert_high_hit_10 is null) = (revert_high_n_10 is null)
           and (revert_high_n_10 is null
                or (revert_high_n_10 > 0 and revert_high_hit_10 between 0 and revert_high_n_10)));

insert into schema_versions (version, description)
values ('202608170001',
        'Spread monitor: previous-day positions for segment-start detection, and template-level mean-reversion rates at 3/5/10 percent')
on conflict (version) do nothing;

commit;
