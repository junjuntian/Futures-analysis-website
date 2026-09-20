-- 席位页的「这个会员做过哪些品种」下拉,从 11.8 秒降到 29 毫秒。
--
-- 事故(2026-09-20 23:00 北京,运营者「网站刷不出来」):席位页卡死,
-- nginx 记下一条 **499**(浏览器等不及自己断开)。api 日志里同一分钟:
--
--   select distinct instrument … member = any($2) …   **11.78 秒**(连着 5 条)
--   select distinct trade_date  … member = any($2) …   **15.18 秒**
--
-- 两天前(09-18)同一批查询还是 1.6~2.4 秒。**慢了近十倍。**
--
-- 根因不是锁、不是连接池(DEC-246)、也不是 /dev/shm(DEC-249),是**磁盘 I/O**:
--
--   Parallel Index Scan using seat_history_by_member
--     Buffers: shared hit=1330 **read=60633**      ← 60633 × 8KB ≈ **474 MB**
--
-- `seat_history_by_member` 是 (workspace_id, member, instrument, trade_date),
-- **不含** `is_variety_total` / `rank_type` / `source` 这三个过滤列,
-- 于是每一条候选都要回表取一次堆页 —— 为了 12 行结果读了 474 MB。
--
-- 为什么**现在**才炸:这张表 3.29 GB / 878 万行,而机器总共 1.9 GB、
-- postgres 容器限额 768 MB、`shared_buffers` 只有 128 MB。
-- **甲醇 2026-09-15 接入,一次加进 126 万行(约 483 MB,占全表 14%)**,
-- 把工作集顶过了缓存能装下的量 —— `seat_history` 的缓存命中率实测只有 **63%**
-- (健康值应 >99%)。热的时候 1.7 秒还能忍,冷的时候就是 11.8 秒。
-- **加品种不是只加数据,它会把已有查询推下缓存悬崖。**
--
-- 这条部分索引把那三个过滤条件写进索引条件,查询变成 Index Only Scan:
--
--   实测:read=60633 → **read=0**,耗时 11.8 秒 / 1.7 秒 → **29 毫秒**
--   索引只有 **28 MB**(部分索引,只收 395 万行里真正会被查的那部分)
--
-- 与 `seat_meta_cache`(DEC-245)不冲突:那个缓存挡的是重复请求,
-- 这条治的是**缓存未命中那一次**——而每天数据一进来版本就变、缓存就空,
-- 运营者晚上第一次开页面撞的正是这一次。
create index if not exists seat_history_member_instrument_live
    on seat_history (workspace_id, member, instrument)
 where not is_variety_total
   and rank_type in ('long', 'short')
   and source <> 'reboard_inferred';

-- 顺带治统计信息陈旧:实测 `last_autoanalyze` 停在 2026-09-15,**六天没更新**。
--
-- 不是 autovacuum 坏了,是默认阈值对这张表根本不适用:
-- `autovacuum_analyze_scale_factor` 默认 0.1,878 万行要**变动 88 万行**才触发一次,
-- 而日更每天只进几千行 —— 按这个速度要三个月才分析一次。
-- 表一直在长(接一个品种就是上百万行),而规划器看的还是几个月前的分布。
--
-- 改成「变动 2 万行 + 0.5%」≈ 每 6 万多行分析一次,日更约每两周一次,
-- 接新品种当天必然触发。scale_factor 不设 0:表继续长大时阈值要跟着长。
alter table seat_history set (
    autovacuum_analyze_scale_factor = 0.005,
    autovacuum_analyze_threshold = 20000
);

insert into schema_versions (version, description)
values ('202609210001',
        'Partial index for per-member instrument lookup; tighter autoanalyze on seat_history')
on conflict (version) do nothing;
