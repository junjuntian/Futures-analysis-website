use domain::import::{ImportBatchStatus, ensure_status_transition};
use sqlx::{PgPool, Row};
use time::OffsetDateTime;
use uuid::Uuid;

#[derive(Debug, Clone)]
pub struct NewImportUpload {
    pub object_id: Uuid,
    pub import_id: Uuid,
    pub file_id: Uuid,
    pub workspace_id: Uuid,
    pub actor_user_id: Uuid,
    pub object_key: String,
    pub sha256: String,
    pub size_bytes: i64,
    pub original_filename: String,
    pub declared_mime_type: String,
    pub detected_format: String,
    pub request_id: Uuid,
}

#[derive(Debug, Clone)]
pub struct ImportUploadRecord {
    pub import_id: Uuid,
    pub status: ImportBatchStatus,
    pub file_id: Uuid,
    pub original_filename: String,
    pub declared_mime_type: String,
    pub detected_format: String,
    pub sha256: String,
    pub size_bytes: i64,
    pub created_at: OffsetDateTime,
    pub updated_at: OffsetDateTime,
}

#[derive(Debug, thiserror::Error)]
pub enum ImportRepositoryError {
    #[error("import is not visible")]
    NotFound,
    #[error("invalid import status transition")]
    InvalidTransition,
    #[error("invalid import status stored in database")]
    InvalidStoredStatus,
    #[error("database operation failed")]
    Database(#[from] sqlx::Error),
}

pub async fn register_upload(
    pool: &PgPool,
    upload: &NewImportUpload,
) -> Result<ImportUploadRecord, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, upload.workspace_id).await?;
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
    let batch_row = sqlx::query(
        "insert into import_batches (id, workspace_id, status, created_by)
         values ($1, $2, 'uploaded', $3)
         returning created_at, updated_at",
    )
    .bind(upload.import_id)
    .bind(upload.workspace_id)
    .bind(upload.actor_user_id)
    .fetch_one(&mut *tx)
    .await?;
    sqlx::query(
        "insert into import_files
            (id, workspace_id, import_batch_id, stored_object_id, original_filename,
             declared_mime_type, detected_format, sha256, size_bytes, created_by)
         values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
    )
    .bind(upload.file_id)
    .bind(upload.workspace_id)
    .bind(upload.import_id)
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
        "insert into audit_logs
            (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, 'import.upload', 'success', $4, jsonb_build_object('import_id', $5::text))",
    )
    .bind(Uuid::now_v7())
    .bind(upload.workspace_id)
    .bind(upload.actor_user_id)
    .bind(upload.request_id)
    .bind(upload.import_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;

    Ok(ImportUploadRecord {
        import_id: upload.import_id,
        status: ImportBatchStatus::Uploaded,
        file_id: upload.file_id,
        original_filename: upload.original_filename.clone(),
        declared_mime_type: upload.declared_mime_type.clone(),
        detected_format: upload.detected_format.clone(),
        sha256: upload.sha256.clone(),
        size_bytes: upload.size_bytes,
        created_at: batch_row.get("created_at"),
        updated_at: batch_row.get("updated_at"),
    })
}

pub async fn record_upload_denied(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    request_id: Uuid,
    error_code: &'static str,
) -> Result<(), ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    sqlx::query(
        "insert into audit_logs
            (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, 'import.upload', 'denied', $4,
                 jsonb_build_object('error_code', $5::text))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(request_id)
    .bind(error_code)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(())
}

pub async fn get_import(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<ImportUploadRecord, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let row = sqlx::query(
        "select b.id as import_id, b.status::text as status, b.created_at, b.updated_at,
                f.id as file_id, f.original_filename, f.declared_mime_type,
                f.detected_format, f.sha256, f.size_bytes
         from import_batches b
         join import_files f
           on f.workspace_id = b.workspace_id and f.import_batch_id = b.id
         where b.workspace_id = $1 and b.id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?;
    tx.commit().await?;
    let row = row.ok_or(ImportRepositoryError::NotFound)?;
    let status: String = row.get("status");
    Ok(ImportUploadRecord {
        import_id: row.get("import_id"),
        status: ImportBatchStatus::parse(&status)
            .ok_or(ImportRepositoryError::InvalidStoredStatus)?,
        file_id: row.get("file_id"),
        original_filename: row.get("original_filename"),
        declared_mime_type: row.get("declared_mime_type"),
        detected_format: row.get("detected_format"),
        sha256: row.get("sha256"),
        size_bytes: row.get("size_bytes"),
        created_at: row.get("created_at"),
        updated_at: row.get("updated_at"),
    })
}

pub async fn transition_status(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
    from: ImportBatchStatus,
    to: ImportBatchStatus,
) -> Result<(), ImportRepositoryError> {
    ensure_status_transition(from, to).map_err(|_| ImportRepositoryError::InvalidTransition)?;
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let affected = sqlx::query(
        "update import_batches
         set status = $1::import_batch_status, updated_at = now()
         where workspace_id = $2 and id = $3 and status = $4::import_batch_status",
    )
    .bind(to.as_str())
    .bind(workspace_id)
    .bind(import_id)
    .bind(from.as_str())
    .execute(&mut *tx)
    .await?
    .rows_affected();
    if affected != 1 {
        return Err(ImportRepositoryError::InvalidTransition);
    }
    tx.commit().await?;
    Ok(())
}

async fn set_workspace(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
) -> Result<(), sqlx::Error> {
    sqlx::query("select set_config('app.current_workspace_id', $1, true)")
        .bind(workspace_id.to_string())
        .execute(&mut **tx)
        .await?;
    Ok(())
}
