pub mod imports;
pub mod job_queue;

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
            "import_row_changes",
            "import_rollback_requests",
            "import_rollback_conflicts",
            "import_data_invalidations",
            "object_consistency_runs",
            "object_consistency_findings",
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
        assert!(MIGRATION.contains("import_batches_compensation_workspace_fk"));
        assert!(MIGRATION.contains("references import_batches(workspace_id, id)"));
        assert!(MIGRATION.contains("import_rollback_requests_job_batch_fk"));
        assert!(MIGRATION.contains("references job_queue(workspace_id, id, aggregate_id)"));
        assert!(MIGRATION.contains("compensation lineage cannot contain a cycle"));
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
        assert!(MIGRATION.contains("Phase 3D does not permit physical object deletion"));
        assert!(!MIGRATION.contains("delete from stored_objects"));
    }
}
