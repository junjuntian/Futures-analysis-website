-- 把参考数据表从导入通道上解绑。
--
-- 背景
-- ----
-- exchanges / instruments / contracts / trading_calendar_versions / trading_calendar_days
-- 存的是**参考数据**:交易所、品种(含中文名与点值)、合约、交易日历。页面天天读
-- 它们。但每张表都带着指向导入通道的 `not null` 外键——想写一行,必须先在通道里
-- 造出血缘记录、导入批次、来源目录。
--
-- 运营者 2026-08-13 说明:导入中心当初是给「上传文件让 AI 分析」建的,那个功能
-- 早已取消,他从没手工导过数据。而这条绑定的实际后果是,**每天的自动采集都被迫
-- 走一遍为「人工上传需要预览和回滚」设计的七层流水线**,中间产物(暂存行、变更
-- 记录、血缘)长到和业务数据一样大。
--
-- 行情与席位已改走直灌(load-seats-direct.sql / run-official-seats.sh),剩下品种
-- 目录与日历还被这些外键拴着。解开,采集才能直接写参考表。
--
-- 这些列有没有人在用
-- ------------------
-- 页面主查询(spread_analytics.rs)一次都没读过 source_record_id;读它的 53 处全在
-- 导入通道自己的代码里(血缘展示、批次回滚)。那套即将整体摘掉。
--
-- 为什么保留列、只去掉约束
-- ------------------------
-- Rust 侧的 insert 还在写这些列,列一没就是运行时报错。分两步更稳:这次只解开
-- 约束与非空,让写入不再依赖导入通道;等 Rust 里的导入路径删干净、确认没人再写
-- 这些列,再单独一个迁移删列。

begin;

alter table exchanges
    drop constraint if exists exchanges_source_record_fk,
    drop constraint if exists exchanges_source_record_identity,
    alter column source_record_id drop not null;

alter table instruments
    drop constraint if exists instruments_source_record_fk,
    drop constraint if exists instruments_source_record_identity,
    alter column source_record_id drop not null;

alter table contracts
    drop constraint if exists contracts_source_record_fk,
    drop constraint if exists contracts_source_record_identity,
    alter column source_record_id drop not null;

-- 日历版本除了血缘,还绑着来源目录(data_sources,随通道一起摘)与创建人
-- (users——直灌是机器行为,没有「谁创建的」)。
alter table trading_calendar_versions
    drop constraint if exists trading_calendar_versions_source_record_fk,
    drop constraint if exists trading_calendar_versions_source_record_identity,
    drop constraint if exists trading_calendar_versions_source_fk,
    alter column source_record_id drop not null,
    alter column source_id drop not null,
    alter column created_by drop not null;

-- 日历天还绑着导入批次(import_batches)。业务唯一约束
-- (workspace_id, calendar_version_id, trade_date) 本来就有,直灌的 on conflict
-- 用它即可,不必新建。
alter table trading_calendar_days
    drop constraint if exists trading_calendar_days_source_record_fk,
    drop constraint if exists trading_calendar_days_source_record_identity,
    drop constraint if exists trading_calendar_days_batch_fk,
    alter column source_record_id drop not null,
    alter column source_import_batch_id drop not null,
    alter column source_row_number drop not null;

-- 日历版本的业务唯一键里带着 source_id(来源目录),而直灌把那一列留空——
-- 于是「同一交易所同一版本」在唯一性上不再唯一,upsert 也找不到可用的冲突目标
-- (2026-08-13 试跑实测报 no unique or exclusion constraint matching)。
--
-- 来源目录本就是导入通道的概念:同一份交易所日历,不该因为「从哪个连接器进来的」
-- 而在库里存成两份。改成 (workspace_id, exchange_id, version),这也是这张表
-- 本来该有的业务身份。
alter table trading_calendar_versions
    drop constraint if exists trading_calendar_versions_business_identity;
alter table trading_calendar_versions
    add constraint trading_calendar_versions_business_identity
    unique (workspace_id, exchange_id, version);

insert into schema_versions (version, description)
values ('202608130002', '参考数据表与导入通道解绑:去掉血缘/批次/来源目录的外键与非空,为直灌铺路')
on conflict (version) do nothing;

commit;
