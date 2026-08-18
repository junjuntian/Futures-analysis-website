-- 套利监控:每个月份模板的手工产业备注(DEC-069,两份交易文档因子化第二批 ④)。
--
-- 统计层(回归率/MAE/红线)回答「历年数字怎么说」,回答不了「为什么」。运营者从
-- 直播与盖楼里学到的品种级知识——「夏天的煤不能做空(安监停产)」「纯碱低库存是
-- 逼空窗口(17 万吨→1500 涨到 3700)」「玻纯反套大仓正套小仓(纯碱能源成本高)」
-- 「12 月焦煤交割量大于 1 月」——此前只存在于聊天记录里。落成每模板一条手填备注,
-- 监控行上随统计一起显示:统计说「合格」,备注提醒「但现在是夏天,这是煤」。
--
-- 键是**月份模板**(品种1+月1+品种2+月2),不是具体合约对:产业规律跟月份走,
-- JD2609/JD2701 与 JD2709/JD2801 共享同一条「09-01」的知识。跨品种(FG-SA 同月)
-- 天然被四元键覆盖。纯手填,零算法;写路径走 API(校验长度),与压力位表同一模式。

begin;

create table if not exists spread_template_notes (
    workspace_id uuid not null references workspaces(id) on delete restrict,
    instrument_1 text not null,
    month_1 int not null,
    instrument_2 text not null,
    month_2 int not null,
    note text not null,
    updated_at timestamptz not null default now(),

    primary key (workspace_id, instrument_1, month_1, instrument_2, month_2),
    constraint spread_template_notes_shape
        check (instrument_1 ~ '^[A-Z]{1,2}$' and instrument_2 ~ '^[A-Z]{1,2}$'
           and month_1 between 1 and 12 and month_2 between 1 and 12
           -- 空备注不落行(删除即清空),长度上限挡住误贴整篇文章。
           and length(note) between 1 and 2000)
);

alter table spread_template_notes enable row level security;
alter table spread_template_notes force row level security;

drop policy if exists spread_template_notes_workspace on spread_template_notes;
create policy spread_template_notes_workspace on spread_template_notes
    using (workspace_id = app.current_workspace_id())
    with check (workspace_id = app.current_workspace_id());

grant select, insert, update, delete on spread_template_notes to futures_runtime;

insert into schema_versions (version, description)
values ('202608180001',
        'Manual per-template industry notes for the spread monitor: the operator''s variety knowledge shown beside the statistics')
on conflict (version) do nothing;

commit;
