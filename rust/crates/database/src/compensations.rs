use domain::import::{
    ImportBatchStatus, ImportCompensationFile, ImportCompensationResponse, ImportLineageAudit,
    ImportLineageFile, ImportLineageJob, ImportLineageNode, ImportLineageResponse,
    ImportLineageRollback, ImportRollbackRequestStatus, RollbackCapability,
};
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Postgres, Row, Transaction};
use time::{OffsetDateTime, format_description::well_known::Rfc3339};
use uuid::Uuid;

const COMPENSATION_IDEMPOTENCY_LOCK_SQL: &str = "select pg_advisory_xact_lock(
    hashtextextended($1::text || ':compensation:' || $2::text, 0)
)";

#[derive(Debug, Clone)]
pub struct NewCompensationUpload {
    pub object_id: Uuid,
    pub compensation_import_id: Uuid,
    pub file_id: Uuid,
    pub workspace_id: Uuid,
    pub actor_user_id: Uuid,
    pub original_import_id: Uuid,
    pub reason: String,
    pub object_key: String,
    pub sha256: String,
    pub size_bytes: i64,
    pub original_filename: String,
    pub declared_mime_type: String,
    pub detected_format: String,
    pub idempotency_key_hash: String,
    pub request_hash: String,
    pub audit_request_id: Uuid,
}

#[derive(Debug, thiserror::Error)]
pub enum CompensationRepositoryError {
    #[error("import is not visible")]
    NotFound,
    #[error("compensation is not allowed for this batch")]
    NotAllowed,
    #[error("compensation lineage would contain a cycle")]
    Cycle,
    #[error("compensation idempotency key was reused")]
    IdempotencyKeyReused,
    #[error("invalid stored compensation state")]
    InvalidStoredState,
    #[error("database operation failed")]
    Database(#[from] sqlx::Error),
}

pub enum CompensationUploadPreparation<'a> {
    Ready(PreparedCompensationUpload<'a>),
    Replay(ImportCompensationResponse),
}

pub struct PreparedCompensationUpload<'a> {
    tx: Transaction<'a, Postgres>,
    dataset_type: String,
}

pub async fn prepare_compensation_upload<'a>(
    pool: &'a PgPool,
    upload: &NewCompensationUpload,
) -> Result<CompensationUploadPreparation<'a>, CompensationRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, upload.workspace_id).await?;
    sqlx::query(COMPENSATION_IDEMPOTENCY_LOCK_SQL)
        .bind(upload.workspace_id)
        .bind(&upload.idempotency_key_hash)
        .execute(&mut *tx)
        .await?;

    if let Some(existing) =
        load_idempotent_compensation(&mut tx, upload.workspace_id, &upload.idempotency_key_hash)
            .await?
    {
        if existing.original_import_id != upload.original_import_id
            || existing.request_hash != upload.request_hash
        {
            return Err(CompensationRepositoryError::IdempotencyKeyReused);
        }
        insert_compensation_audit(
            &mut tx,
            upload.workspace_id,
            upload.actor_user_id,
            upload.audit_request_id,
            existing.original_import_id,
            existing.response.compensation_import_id,
            "import.compensation_replayed",
            "success",
            &existing.response.reason,
        )
        .await?;
        tx.commit().await?;
        return Ok(CompensationUploadPreparation::Replay(
            ImportCompensationResponse {
                replayed: true,
                ..existing.response
            },
        ));
    }

    let original = sqlx::query(
        "select status::text as status, dataset_type
           from import_batches
          where workspace_id = $1 and id = $2
          for update",
    )
    .bind(upload.workspace_id)
    .bind(upload.original_import_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(CompensationRepositoryError::NotFound)?;
    let original_status = original.get::<String, _>("status");
    if !matches!(
        original_status.as_str(),
        "succeeded" | "rollback_conflict" | "rolled_back" | "rollback_failed"
    ) {
        return Err(CompensationRepositoryError::NotAllowed);
    }

    Ok(CompensationUploadPreparation::Ready(
        PreparedCompensationUpload {
            tx,
            dataset_type: original.get("dataset_type"),
        },
    ))
}

pub async fn create_compensation_upload(
    prepared: PreparedCompensationUpload<'_>,
    upload: &NewCompensationUpload,
) -> Result<ImportCompensationResponse, CompensationRepositoryError> {
    let PreparedCompensationUpload {
        mut tx,
        dataset_type,
    } = prepared;

    sqlx::query(
        "insert into stored_objects
            (id, workspace_id, object_key, sha256, size_bytes, mime_type, backend, state,
             retention_until, created_by)
         values ($1, $2, $3, $4, $5, $6, 'local', 'available', null, $7)",
    )
    .bind(upload.object_id)
    .bind(upload.workspace_id)
    .bind(&upload.object_key)
    .bind(&upload.sha256)
    .bind(upload.size_bytes)
    .bind(&upload.declared_mime_type)
    .bind(upload.actor_user_id)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "insert into import_batches
            (id, workspace_id, status, dataset_type, created_by, compensates_batch_id)
         values ($1, $2, 'uploaded', $3, $4, $5)",
    )
    .bind(upload.compensation_import_id)
    .bind(upload.workspace_id)
    .bind(&dataset_type)
    .bind(upload.actor_user_id)
    .bind(upload.original_import_id)
    .execute(&mut *tx)
    .await
    .map_err(map_compensation_constraint)?;
    sqlx::query(
        "insert into import_files
            (id, workspace_id, import_batch_id, stored_object_id, original_filename,
             declared_mime_type, detected_format, sha256, size_bytes, created_by)
         values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
    )
    .bind(upload.file_id)
    .bind(upload.workspace_id)
    .bind(upload.compensation_import_id)
    .bind(upload.object_id)
    .bind(&upload.original_filename)
    .bind(&upload.declared_mime_type)
    .bind(&upload.detected_format)
    .bind(&upload.sha256)
    .bind(upload.size_bytes)
    .bind(upload.actor_user_id)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "insert into import_compensations
            (id, workspace_id, original_import_batch_id, compensation_import_batch_id,
             reason, requested_by, idempotency_key_hash, request_hash)
         values ($1, $2, $3, $4, $5, $6, $7, $8)",
    )
    .bind(Uuid::now_v7())
    .bind(upload.workspace_id)
    .bind(upload.original_import_id)
    .bind(upload.compensation_import_id)
    .bind(&upload.reason)
    .bind(upload.actor_user_id)
    .bind(&upload.idempotency_key_hash)
    .bind(&upload.request_hash)
    .execute(&mut *tx)
    .await
    .map_err(map_compensation_constraint)?;
    insert_compensation_audit(
        &mut tx,
        upload.workspace_id,
        upload.actor_user_id,
        upload.audit_request_id,
        upload.original_import_id,
        upload.compensation_import_id,
        "import.compensation_created",
        "success",
        &upload.reason,
    )
    .await?;
    tx.commit().await?;
    Ok(ImportCompensationResponse {
        original_import_id: upload.original_import_id,
        compensation_import_id: upload.compensation_import_id,
        status: ImportBatchStatus::Uploaded,
        reason: upload.reason.clone(),
        requested_by: upload.actor_user_id,
        file: ImportCompensationFile {
            file_id: upload.file_id,
            original_filename: upload.original_filename.clone(),
            detected_format: upload.detected_format.clone(),
            sha256: upload.sha256.clone(),
            size_bytes: upload.size_bytes as u64,
        },
        replayed: false,
    })
}

pub async fn recover_compensation(
    pool: &PgPool,
    workspace_id: Uuid,
    original_import_id: Uuid,
    idempotency_key_hash: &str,
    request_hash: &str,
) -> Result<Option<ImportCompensationResponse>, CompensationRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let existing =
        load_idempotent_compensation(&mut tx, workspace_id, idempotency_key_hash).await?;
    tx.commit().await?;
    match existing {
        Some(existing)
            if existing.original_import_id == original_import_id
                && existing.request_hash == request_hash =>
        {
            Ok(Some(existing.response))
        }
        Some(_) => Err(CompensationRepositoryError::IdempotencyKeyReused),
        None => Ok(None),
    }
}

struct ExistingCompensation {
    original_import_id: Uuid,
    request_hash: String,
    response: ImportCompensationResponse,
}

async fn load_idempotent_compensation(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    idempotency_key_hash: &str,
) -> Result<Option<ExistingCompensation>, CompensationRepositoryError> {
    let row = sqlx::query(
        "select compensation.original_import_batch_id,
                compensation.compensation_import_batch_id, compensation.reason,
                compensation.requested_by, compensation.request_hash,
                batch.status::text as status,
                file.id as file_id, file.original_filename, file.detected_format,
                file.sha256, file.size_bytes
           from import_compensations compensation
           join import_batches batch
             on batch.workspace_id = compensation.workspace_id
            and batch.id = compensation.compensation_import_batch_id
           join import_files file
             on file.workspace_id = batch.workspace_id
            and file.import_batch_id = batch.id
          where compensation.workspace_id = $1
            and compensation.idempotency_key_hash = $2",
    )
    .bind(workspace_id)
    .bind(idempotency_key_hash)
    .fetch_optional(&mut **tx)
    .await?;
    row.map(|row| {
        let status = ImportBatchStatus::parse(row.get::<String, _>("status").as_str())
            .ok_or(CompensationRepositoryError::InvalidStoredState)?;
        let original_import_id = row.get("original_import_batch_id");
        Ok(ExistingCompensation {
            original_import_id,
            request_hash: row.get("request_hash"),
            response: ImportCompensationResponse {
                original_import_id,
                compensation_import_id: row.get("compensation_import_batch_id"),
                status,
                reason: row.get("reason"),
                requested_by: row.get("requested_by"),
                file: ImportCompensationFile {
                    file_id: row.get("file_id"),
                    original_filename: row.get("original_filename"),
                    detected_format: row.get("detected_format"),
                    sha256: row.get("sha256"),
                    size_bytes: row.get::<i64, _>("size_bytes") as u64,
                },
                replayed: true,
            },
        })
    })
    .transpose()
}

pub async fn get_lineage(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<ImportLineageResponse, CompensationRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let root_import_id = sqlx::query_scalar::<_, Uuid>(
        "with recursive ancestors(id, compensates_batch_id, depth) as (
            select id, compensates_batch_id, 0
              from import_batches
             where workspace_id = $1 and id = $2
            union all
            select parent.id, parent.compensates_batch_id, child.depth + 1
              from import_batches parent
              join ancestors child on child.compensates_batch_id = parent.id
             where parent.workspace_id = $1
         )
         select id from ancestors order by depth desc limit 1",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(CompensationRepositoryError::NotFound)?;
    let rows = sqlx::query(
        "with recursive lineage(id, depth) as (
            select $2::uuid, 0
            union all
            select child.id, parent.depth + 1
              from import_batches child
              join lineage parent on child.compensates_batch_id = parent.id
             where child.workspace_id = $1
         )
         select batch.id, batch.status::text as status, batch.compensates_batch_id,
                batch.created_by, batch.confirmed_by, batch.rollback_capability,
                batch.created_at, batch.confirmed_at, batch.rolled_back_at,
                mapping.id as mapping_id, compensation.reason,
                file.id as file_id, file.stored_object_id, file.original_filename,
                file.detected_format, file.sha256, file.size_bytes, object.state as object_state
           from lineage
           join import_batches batch
             on batch.workspace_id = $1 and batch.id = lineage.id
           join import_files file
             on file.workspace_id = batch.workspace_id and file.import_batch_id = batch.id
           join stored_objects object
             on object.workspace_id = file.workspace_id and object.id = file.stored_object_id
           left join import_mappings mapping
             on mapping.workspace_id = batch.workspace_id
            and mapping.import_batch_id = batch.id
           left join import_compensations compensation
             on compensation.workspace_id = batch.workspace_id
            and compensation.compensation_import_batch_id = batch.id
          order by lineage.depth, batch.created_at, batch.id",
    )
    .bind(workspace_id)
    .bind(root_import_id)
    .fetch_all(&mut *tx)
    .await?;
    let mut nodes = Vec::with_capacity(rows.len());
    for row in rows {
        let batch_id = row.get::<Uuid, _>("id");
        let jobs = load_lineage_jobs(&mut tx, workspace_id, batch_id).await?;
        let rollbacks = load_lineage_rollbacks(&mut tx, workspace_id, batch_id).await?;
        nodes.push(ImportLineageNode {
            import_id: batch_id,
            status: ImportBatchStatus::parse(row.get::<String, _>("status").as_str())
                .ok_or(CompensationRepositoryError::InvalidStoredState)?,
            compensates_import_id: row.get("compensates_batch_id"),
            compensation_reason: row.get("reason"),
            created_by: row.get("created_by"),
            confirmed_by: row.get("confirmed_by"),
            rollback_capability: RollbackCapability::parse(
                row.get::<String, _>("rollback_capability").as_str(),
            )
            .ok_or(CompensationRepositoryError::InvalidStoredState)?,
            mapping_id: row.get("mapping_id"),
            created_at: format_time(row.get("created_at"))?,
            confirmed_at: format_optional_time(row.get("confirmed_at"))?,
            rolled_back_at: format_optional_time(row.get("rolled_back_at"))?,
            file: ImportLineageFile {
                file_id: row.get("file_id"),
                object_id: row.get("stored_object_id"),
                original_filename: row.get("original_filename"),
                detected_format: row.get("detected_format"),
                sha256: row.get("sha256"),
                size_bytes: row.get::<i64, _>("size_bytes") as u64,
                object_state: row.get("object_state"),
            },
            jobs,
            rollbacks,
        });
    }
    let node_ids = nodes.iter().map(|node| node.import_id).collect::<Vec<_>>();
    let audits = load_lineage_audits(&mut tx, workspace_id, &node_ids).await?;
    tx.commit().await?;
    Ok(ImportLineageResponse {
        requested_import_id: import_id,
        root_import_id,
        nodes,
        audits,
    })
}

async fn load_lineage_jobs(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<Vec<ImportLineageJob>, CompensationRepositoryError> {
    let rows = sqlx::query(
        "select id, job_type, status, attempt_count, last_error_code
           from job_queue
          where workspace_id = $1 and aggregate_id = $2
          order by created_at, id",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_all(&mut **tx)
    .await?;
    Ok(rows
        .into_iter()
        .map(|row| ImportLineageJob {
            job_id: row.get("id"),
            job_type: row.get("job_type"),
            status: row.get("status"),
            attempt_count: row.get::<i32, _>("attempt_count") as u32,
            error_code: row.get("last_error_code"),
        })
        .collect())
}

async fn load_lineage_rollbacks(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<Vec<ImportLineageRollback>, CompensationRepositoryError> {
    let rows = sqlx::query(
        "select id, status, conflict_count, requested_by
           from import_rollback_requests
          where workspace_id = $1 and import_batch_id = $2
          order by created_at, id",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_all(&mut **tx)
    .await?;
    rows.into_iter()
        .map(|row| {
            Ok(ImportLineageRollback {
                rollback_request_id: row.get("id"),
                status: ImportRollbackRequestStatus::parse(row.get::<String, _>("status").as_str())
                    .ok_or(CompensationRepositoryError::InvalidStoredState)?,
                conflict_count: row.get::<i32, _>("conflict_count") as u32,
                requested_by: row.get("requested_by"),
            })
        })
        .collect()
}

async fn load_lineage_audits(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    import_ids: &[Uuid],
) -> Result<Vec<ImportLineageAudit>, CompensationRepositoryError> {
    let import_id_strings = import_ids.iter().map(Uuid::to_string).collect::<Vec<_>>();
    let rows = sqlx::query(
        "select id, actor_user_id, event_type, outcome, created_at,
                metadata ->> 'import_id' as import_id
           from audit_logs
          where workspace_id = $1
            and metadata ->> 'import_id' = any($2::text[])
          order by created_at, id",
    )
    .bind(workspace_id)
    .bind(&import_id_strings)
    .fetch_all(&mut **tx)
    .await?;
    rows.into_iter()
        .map(|row| {
            Ok(ImportLineageAudit {
                audit_id: row.get("id"),
                import_id: Uuid::parse_str(row.get::<String, _>("import_id").as_str())
                    .map_err(|_| CompensationRepositoryError::InvalidStoredState)?,
                event_type: row.get("event_type"),
                outcome: row.get("outcome"),
                actor_user_id: row.get("actor_user_id"),
                created_at: format_time(row.get("created_at"))?,
            })
        })
        .collect()
}

#[allow(clippy::too_many_arguments)]
async fn insert_compensation_audit(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    request_id: Uuid,
    original_import_id: Uuid,
    compensation_import_id: Uuid,
    event_type: &str,
    outcome: &str,
    reason: &str,
) -> Result<(), CompensationRepositoryError> {
    let reason_sha256 = format!("{:x}", Sha256::digest(reason.as_bytes()));
    sqlx::query(
        "insert into audit_logs
           (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, $4, $5, $6,
                 jsonb_build_object(
                    'import_id', $7::text,
                    'original_import_id', $8::text,
                    'reason_sha256', $9::text
                 ))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(event_type)
    .bind(outcome)
    .bind(request_id)
    .bind(compensation_import_id)
    .bind(original_import_id)
    .bind(reason_sha256)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

fn format_time(value: OffsetDateTime) -> Result<String, CompensationRepositoryError> {
    value
        .format(&Rfc3339)
        .map_err(|_| CompensationRepositoryError::InvalidStoredState)
}

fn format_optional_time(
    value: Option<OffsetDateTime>,
) -> Result<Option<String>, CompensationRepositoryError> {
    value.map(format_time).transpose()
}

fn map_compensation_constraint(error: sqlx::Error) -> CompensationRepositoryError {
    let message = error
        .as_database_error()
        .map(|error| error.message().to_string())
        .unwrap_or_default();
    if message.contains("cycle") {
        CompensationRepositoryError::Cycle
    } else if message.contains("idempotency") {
        CompensationRepositoryError::IdempotencyKeyReused
    } else if message.contains("compensation must reference") {
        CompensationRepositoryError::NotAllowed
    } else {
        CompensationRepositoryError::Database(error)
    }
}

async fn set_workspace(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
) -> Result<(), sqlx::Error> {
    sqlx::query("select set_config('app.current_workspace_id', $1, true)")
        .bind(workspace_id.to_string())
        .execute(&mut **tx)
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    const SOURCE: &str = include_str!("compensations.rs");
    const MIGRATION: &str = include_str!(
        "../../../migrations/202607260001_phase_3d_rollback_and_object_governance.sql"
    );

    #[test]
    fn compensation_is_uploaded_batch_not_an_empty_draft() {
        let create = SOURCE
            .split("pub async fn create_compensation_upload")
            .nth(1)
            .unwrap()
            .split("pub async fn recover_compensation")
            .next()
            .unwrap();
        assert!(create.contains("insert into stored_objects"));
        assert!(create.contains("insert into import_batches"));
        assert!(create.contains("insert into import_files"));
        assert!(create.contains("compensates_batch_id"));
        assert_eq!(create.matches("tx.commit().await?").count(), 1);
    }

    #[test]
    fn compensation_idempotency_is_workspace_scoped_and_serialized() {
        assert!(COMPENSATION_IDEMPOTENCY_LOCK_SQL.contains("pg_advisory_xact_lock"));
        assert!(MIGRATION.contains("unique (workspace_id, idempotency_key_hash)"));
        assert!(SOURCE.contains("existing.request_hash != upload.request_hash"));
    }

    #[test]
    fn compensation_and_rollback_serialize_on_the_original_batch() {
        let prepare = SOURCE
            .split("pub async fn prepare_compensation_upload")
            .nth(1)
            .unwrap()
            .split("pub async fn create_compensation_upload")
            .next()
            .unwrap();
        let original_lock = prepare.find("from import_batches").unwrap();
        let for_update = prepare[original_lock..]
            .find("for update")
            .map(|offset| original_lock + offset)
            .unwrap();
        let create = SOURCE
            .split("pub async fn create_compensation_upload")
            .nth(1)
            .unwrap()
            .split("pub async fn recover_compensation")
            .next()
            .unwrap();
        let compensation_insert = create.find("insert into import_compensations").unwrap();
        assert!(for_update > original_lock);
        assert!(compensation_insert > 0);
        assert!(prepare.contains("\"rolled_back\""));

        let rollback_source = include_str!("imports.rs");
        let evaluation = rollback_source
            .split("async fn evaluate_rollback")
            .nth(1)
            .unwrap()
            .split("fn finalize_rollback_evaluation")
            .next()
            .unwrap();
        assert!(evaluation.contains("from import_compensations compensation"));
        assert!(evaluation.contains("\"compensation_batch_exists\""));
    }

    #[test]
    fn lineage_is_same_workspace_recursive_and_summary_only() {
        let lineage = SOURCE
            .split("pub async fn get_lineage")
            .nth(1)
            .unwrap()
            .split("async fn load_lineage_jobs")
            .next()
            .unwrap();
        assert!(lineage.contains("with recursive ancestors"));
        assert!(lineage.contains("with recursive lineage"));
        assert!(lineage.contains("workspace_id = $1"));
        for forbidden in ["object_key", "mapping_json", "payload", "metadata"] {
            assert!(!lineage.contains(forbidden));
        }
    }

    #[test]
    fn reason_is_preserved_but_only_hashed_into_audit_metadata() {
        assert!(MIGRATION.contains("reason text not null"));
        let audit = SOURCE
            .split("async fn insert_compensation_audit")
            .nth(1)
            .unwrap()
            .split("fn format_time")
            .next()
            .unwrap();
        assert!(audit.contains("reason_sha256"));
        assert!(!audit.contains("'reason',"));
    }
}
