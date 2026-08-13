-- 删掉导入通道的全部表。
--
-- 为什么删
-- --------
-- 导入中心当初是给「上传文件让 AI 分析」建的。运营者 2026-08-13 说明那个功能
-- 早已取消,他从没手工导过数据——而每天的自动采集一直被迫走这条为「人工上传
-- 需要预览和回滚」设计的七层流水线(上传→暂存→逐行校验→冲突检测→确认→血缘→
-- canonical→投影→宽表)。
--
-- 代价是实打实的:生产上暂存行 1448 MB、变更记录 832 MB、校验错误 66 MB、
-- 血缘 292 MB,中间产物和最终业务数据一样大,而且每天都在长。九张表从建库至今
-- 一行数据都没有过。
--
-- 采集已全部改走直灌(load-*-direct.sql / run-official-seats.sh / load-dce-daily.sql),
-- 前端导入中心与后端 13,000 余行实现也已删除。这些表现在没有任何写入方。
--
-- 保留了什么
-- ----------
-- data_sources 不删:spread_provider_series(自由价差的原始序列)引用着它。
-- 它现在的角色变成「来源目录」这一件事,与导入通道无关。
--
-- market_prices / seat_positions 删:它们是 canonical 中转层,页面读的是
-- price_history / seat_history 两张宽表,数据已经在那里了(新机器迁移时
-- 逐表核对过:604 万席位 / 24 万行情,一行不差)。
--
-- 不可逆
-- ------
-- 这一步删的是数据不是结构别名。执行前新机器上做过完整备份,老机器仍在运行
-- 且保有全部历史——那是真正的退路。

begin;

-- 顺序无关:cascade 会处理彼此之间的外键。列在一起是为了一眼看清删了什么。
drop table if exists
    import_row_changes,
    import_staging_rows,
    import_errors,
    import_job_events,
    import_confirmations,
    import_conflict_candidates,
    import_rollback_conflicts,
    import_rollback_requests,
    import_compensations,
    import_data_invalidations,
    import_mappings,
    import_template_versions,
    import_templates,
    import_files,
    import_batches,
    imported_records,
    object_consistency_findings,
    object_consistency_runs,
    object_governance_jobs,
    object_quarantine_requests,
    object_quarantines,
    stored_objects,
    extraction_jobs,
    job_queue,
    market_prices,
    seat_positions,
    seat_entities
cascade;

insert into schema_versions (version, description)
values ('202608130003', '删除导入通道的 27 张表:采集已全部改走直灌,前端与后端实现已移除')
on conflict (version) do nothing;

commit;
