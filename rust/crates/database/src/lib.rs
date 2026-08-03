pub mod compensations;
pub mod imports;
pub mod job_queue;
pub mod object_governance;
pub mod rollback_jobs;
pub mod worker_scheduler;

use sqlx::{PgPool, postgres::PgPoolOptions};
use std::time::Duration;

pub async fn connect(database_url: &str) -> Result<PgPool, sqlx::Error> {
    PgPoolOptions::new()
        .max_connections(5)
        .acquire_timeout(Duration::from_secs(5))
        .connect(database_url)
        .await
}

pub async fn check_ready(pool: &PgPool) -> bool {
    sqlx::query("select 1").execute(pool).await.is_ok()
}

#[cfg(test)]
mod phase_3d_schema_contract {
    const MIGRATION: &str = include_str!(
        "../../../migrations/202607260001_phase_3d_rollback_and_object_governance.sql"
    );
    const FAIRNESS_MIGRATION: &str =
        include_str!("../../../migrations/202607260002_phase_3d_worker_fairness.sql");

    #[test]
    fn legacy_batches_are_not_given_fabricated_change_rows() {
        assert!(MIGRATION.contains("default 'compensation_only'"));
        assert!(MIGRATION.contains("set rollback_capability = 'compensation_only'"));
        assert!(MIGRATION.contains("change_log_version = null"));
        assert!(
            !MIGRATION.contains("insert into import_row_changes\nselect"),
            "the migration must not infer history from current records"
        );
    }

    #[test]
    fn phase_3d_tables_are_workspace_scoped_and_force_rls() {
        for table in [
            "import_compensations",
            "import_row_changes",
            "import_rollback_requests",
            "import_rollback_conflicts",
            "import_data_invalidations",
            "object_consistency_runs",
            "object_consistency_findings",
            "object_quarantine_requests",
            "object_governance_jobs",
            "object_quarantines",
        ] {
            assert!(
                MIGRATION.contains(&format!("alter table {table} enable row level security;")),
                "{table} must enable RLS"
            );
            assert!(
                MIGRATION.contains(&format!("alter table {table} force row level security;")),
                "{table} must force RLS"
            );
            assert!(
                MIGRATION.contains(&format!(
                    "create policy {table}_workspace_isolation on {table}"
                )),
                "{table} must have a Workspace policy"
            );
        }
    }

    #[test]
    fn change_log_is_append_only_and_source_bound() {
        assert!(MIGRATION.contains("import_row_changes_immutable"));
        assert!(MIGRATION.contains("before update or delete on import_row_changes"));
        assert!(MIGRATION.contains("import_row_changes_source_file_batch_fk"));
        assert!(MIGRATION.contains("import_row_changes_contiguous_sequence"));
        assert!(MIGRATION.contains("import_batches_validate_direct_change_log"));
    }

    #[test]
    fn compensation_and_rollback_links_cannot_cross_workspaces() {
        assert!(MIGRATION.contains("import_compensations_original_batch_fk"));
        assert!(MIGRATION.contains("import_compensations_compensation_batch_fk"));
        assert!(MIGRATION.contains("import_compensations_immutable"));
        assert!(MIGRATION.contains("import_batches_compensation_lineage_identity"));
        assert!(MIGRATION.contains(
            "foreign key (\n            workspace_id,\n            compensation_import_batch_id,\n            original_import_batch_id\n        )"
        ));
        assert!(
            MIGRATION.contains("references import_batches(workspace_id, id, compensates_batch_id)")
        );
        assert!(MIGRATION.contains("import_batches_compensation_workspace_fk"));
        assert!(MIGRATION.contains("references import_batches(workspace_id, id)"));
        assert!(MIGRATION.contains("import_rollback_requests_job_batch_fk"));
        assert!(MIGRATION.contains("references job_queue(workspace_id, id, aggregate_id)"));
        assert!(MIGRATION.contains("compensation lineage cannot contain a cycle"));
    }

    #[test]
    fn compensation_lineage_is_bound_on_insert_and_immutable_afterward() {
        let invariant = MIGRATION
            .split("create or replace function app.enforce_import_batch_phase_3d_invariants()")
            .nth(1)
            .expect("batch invariant trigger")
            .split("create or replace function app.enforce_rollback_request_job()")
            .next()
            .expect("batch invariant trigger end");
        assert!(
            invariant.contains(
                "if tg_op = 'UPDATE'\n       and new.compensates_batch_id is distinct from old.compensates_batch_id then"
            ),
            "NULL-to-parent, parent-to-NULL, and parent-to-other updates must all be rejected"
        );
        assert!(invariant.contains("compensation lineage is immutable once established"));

        let repository = include_str!("compensations.rs");
        let create = repository
            .split("pub async fn create_compensation_upload")
            .nth(1)
            .expect("compensation creation")
            .split("pub async fn recover_compensation")
            .next()
            .expect("compensation creation end");
        assert!(create.contains(
            "insert into import_batches\n            (id, workspace_id, status, dataset_type, created_by, compensates_batch_id)"
        ));
    }

    #[test]
    fn synchronous_prechecks_do_not_require_or_create_jobs() {
        assert!(MIGRATION.contains("idempotency_key_hash char(64),"));
        assert!(MIGRATION.contains("request_hash char(64),"));
        assert!(MIGRATION.contains("status text not null default 'prechecked'"));
        assert!(MIGRATION.contains("status = 'precheck_conflict'"));
        assert!(MIGRATION.contains("job_id is null"));
        assert!(MIGRATION.contains("('prechecked', 'queued')"));
        assert!(
            MIGRATION
                .contains("rollback request must persist a synchronous precheck before queueing")
        );
    }

    #[test]
    fn asynchronous_rollback_states_are_strictly_job_bound() {
        assert!(MIGRATION.contains("status in ('queued', 'running')"));
        assert!(MIGRATION.contains("status = 'succeeded'"));
        assert!(MIGRATION.contains("status = 'worker_conflict'"));
        assert!(MIGRATION.contains("status = 'failed'"));
        assert!(MIGRATION.contains("idempotency_key_hash is not null"));
        assert!(MIGRATION.contains("request_hash is not null"));
        assert!(MIGRATION.contains("job_id is not null"));
        assert!(MIGRATION.contains("import_rollback_requests_validate_job"));
    }

    #[test]
    fn persisted_conflict_pages_match_their_request_count() {
        assert!(MIGRATION.contains("import_rollback_requests_validate_conflict_count"));
        assert!(MIGRATION.contains("import_rollback_conflicts_validate_parent_count"));
        assert!(MIGRATION.contains("rollback conflict count must match"));
        assert!(MIGRATION.contains("rollback conflict snapshot cannot diverge"));
    }

    #[test]
    fn object_governance_has_quarantine_but_no_runtime_delete() {
        assert!(MIGRATION.contains("'quarantined'"));
        assert!(MIGRATION.contains("object_quarantines_immutable"));
        assert!(MIGRATION.contains("revoke delete on stored_objects from futures_runtime"));
        assert!(!MIGRATION.contains("delete from stored_objects"));
        assert!(!MIGRATION.contains("'deleted'"));
        assert!(!MIGRATION.contains("'deleting'"));
        assert!(MIGRATION.contains("stored_objects_validate_quarantine_metadata"));
        assert!(MIGRATION.contains("object_quarantines_validate_stored_object"));
        assert!(MIGRATION.contains("new.object_key ~ ("));
        assert!(MIGRATION.contains("object_governance_jobs_scan_fk"));
        assert!(MIGRATION.contains("object_governance_jobs_quarantine_fk"));
        assert!(!MIGRATION.contains("drop constraint job_queue_import_batch_fk"));
        assert!(MIGRATION.contains("import_files_validate_no_quarantine_reference"));
        assert!(MIGRATION.contains("stored_objects_prevent_quarantine_registration"));
        assert!(MIGRATION.contains("':object-key:'"));
    }

    #[test]
    fn runtime_delete_is_granted_only_for_controlled_import_rollback() {
        assert!(MIGRATION.contains("grant delete on imported_records to futures_runtime;"));
        assert!(MIGRATION.contains("revoke delete on stored_objects from futures_runtime;"));
        assert!(!MIGRATION.contains("grant delete on stored_objects"));
    }

    #[test]
    fn fair_dispatch_state_is_persistent_without_a_tenant_bypass_table() {
        assert!(FAIRNESS_MIGRATION.contains("alter table workspaces"));
        assert!(FAIRNESS_MIGRATION.contains("import_job_last_served_ticket"));
        assert!(FAIRNESS_MIGRATION.contains("object_job_last_served_ticket"));
        assert!(FAIRNESS_MIGRATION.contains("worker_dispatch_ticket_seq"));
        assert!(!FAIRNESS_MIGRATION.contains("create table"));
        assert!(!FAIRNESS_MIGRATION.contains("disable row level security"));
    }
}

#[cfg(test)]
mod phase_4a_schema_contract {
    const MIGRATION: &str =
        include_str!("../../../migrations/202608020001_phase_4a_collection_schema.sql");
    const EVALUATOR_FIXES: &str =
        include_str!("../../../migrations/202608030001_phase_4a_evaluator_fixes.sql");
    const RLS_BACKFILL: &str =
        include_str!("../../../migrations/202608030002_phase_4a_rls_backfill.sql");

    #[test]
    fn all_phase_4a_business_tables_force_workspace_rls() {
        for table in [
            "data_sources",
            "data_source_allowed_domains",
            "exchanges",
            "instruments",
            "contracts",
            "trading_calendar_versions",
            "trading_calendar_days",
            "market_prices",
            "seat_entities",
            "seat_positions",
            "extraction_jobs",
        ] {
            assert!(MIGRATION.contains(&format!("alter table {table} enable row level security;")));
            assert!(MIGRATION.contains(&format!("alter table {table} force row level security;")));
            assert!(MIGRATION.contains(&format!(
                "create policy {table}_workspace_isolation on {table}"
            )));
        }
    }

    #[test]
    fn formal_facts_have_business_keys_and_import_provenance() {
        for table in ["trading_calendar_days", "market_prices", "seat_positions"] {
            let section = MIGRATION
                .split(&format!("create table {table}"))
                .nth(1)
                .expect("table")
                .split("create table")
                .next()
                .expect("table end");
            assert!(section.contains("business_identity unique"));
            assert!(section.contains("source_import_batch_id"));
            assert!(section.contains("source_row_number"));
            assert!(section.contains("source_record_id"));
        }
    }

    #[test]
    fn automatic_metadata_is_fixed_to_four_versioned_datasets() {
        assert!(MIGRATION.contains("fixed_template_code = dataset_type || '@1'"));
        for dataset in [
            "futures_catalog_v1",
            "trading_calendar_v1",
            "daily_market_prices_v1",
            "seat_positions_v1",
        ] {
            assert!(MIGRATION.contains(dataset));
        }
    }

    #[test]
    fn evaluator_fixes_version_every_formal_projection_for_atomic_rollback() {
        for table in [
            "trading_calendar_versions",
            "trading_calendar_days",
            "market_prices",
            "seat_positions",
        ] {
            assert!(
                EVALUATOR_FIXES
                    .contains(&format!("alter table {table}\n    add column row_version"))
            );
        }
        for target in [
            "'exchange'",
            "'instrument'",
            "'contract'",
            "'trading_calendar_version'",
            "'trading_calendar_day'",
            "'market_price'",
            "'seat_entity'",
            "'seat_position'",
        ] {
            assert!(EVALUATOR_FIXES.contains(target));
        }
        assert!(EVALUATOR_FIXES.contains("ingestion_mode = 'automatic'"));
        assert!(EVALUATOR_FIXES.contains("rollback_capability = 'compensation_only'"));
    }

    #[test]
    fn source_identity_backfill_is_tenant_scoped_and_preserves_v2_batches() {
        assert!(RLS_BACKFILL.contains("select id from workspaces order by id"));
        assert!(RLS_BACKFILL.contains("set_config("));
        assert!(RLS_BACKFILL.contains("'app.current_workspace_id'"));
        assert!(RLS_BACKFILL.contains("workspace_id = target_workspace_id"));
        assert!(RLS_BACKFILL.contains("change_log_version = 1"));
        assert!(!RLS_BACKFILL.contains("disable row level security"));
        assert!(!RLS_BACKFILL.contains("bypassrls"));
    }
}
