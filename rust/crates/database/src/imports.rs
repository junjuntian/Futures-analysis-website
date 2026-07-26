use application::import_jobs::{StagingRowInput, ValidationOutcome};
use domain::import::{
    ImportBatchStatus, ImportConflictPolicy, ImportErrorPreview, ImportErrorSeverity,
    ImportMappingDefinitionError, ImportMappingField, ImportMappingResponse, ImportPreviewRow,
    ImportTemplateSummary, ImportTemplateVersionResponse, ensure_status_transition,
    validate_mapping_fields,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Row};
use time::OffsetDateTime;
use uuid::Uuid;

const CONFIRM_BATCH_SQL: &str = "update import_batches
 set status = 'confirmed', conflict_policy = $1,
     confirmation_fingerprint = $2, confirmed_by = $3,
     confirmed_at = now(), updated_at = now()
 where workspace_id = $4 and id = $5 and status = 'preview_ready'";
const CONFIRM_IDEMPOTENCY_LOCK_SQL: &str = "select pg_advisory_xact_lock(
    hashtextextended($1::text || ':' || $2::text, 0)
)";

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
    pub object_key: String,
    pub original_filename: String,
    pub declared_mime_type: String,
    pub detected_format: String,
    pub sha256: String,
    pub size_bytes: i64,
    pub created_at: OffsetDateTime,
    pub updated_at: OffsetDateTime,
    pub validation_version: Option<i32>,
    pub staging_version: i64,
    pub blocking_error_count: i32,
    pub warning_count: i32,
    pub duplicate_count: i32,
    pub conflict_count: i32,
    pub conflict_policy: Option<String>,
    pub job_id: Option<Uuid>,
    pub job_status: Option<String>,
    pub job_attempt_count: Option<i32>,
    pub job_max_attempts: Option<i32>,
    pub job_error_code: Option<String>,
    pub total_rows: i64,
    pub processed_count: i32,
    pub imported_count: i32,
    pub skipped_count: i32,
    pub overwritten_count: i32,
    pub conflict_result_count: i32,
}

#[derive(Debug, Clone)]
pub struct InspectionUpdate<'a> {
    pub workspace_id: Uuid,
    pub actor_user_id: Uuid,
    pub import_id: Uuid,
    pub detected_encoding: Option<&'a str>,
    pub detected_delimiter: Option<&'a str>,
    pub selected_sheet: Option<&'a str>,
    pub header_row: i32,
}

#[derive(Debug, Clone, Copy)]
pub struct InspectionSaveResult {
    pub status: ImportBatchStatus,
    pub preview_invalidated: bool,
}

#[derive(Debug, Clone)]
pub struct PreviewInputSnapshot<'a> {
    pub detected_encoding: Option<&'a str>,
    pub detected_delimiter: Option<&'a str>,
    pub selected_sheet: Option<&'a str>,
    pub header_row: i32,
    pub fields: &'a [ImportMappingField],
}

#[derive(Debug, Clone)]
pub struct PreviewSave<'a> {
    pub workspace_id: Uuid,
    pub actor_user_id: Uuid,
    pub import_id: Uuid,
    pub snapshot: PreviewInputSnapshot<'a>,
    pub rows: &'a [ImportPreviewRow],
    pub errors: &'a [ImportErrorPreview],
    pub warnings: &'a [ImportErrorPreview],
}

#[derive(Debug, Clone)]
struct ExistingMapping {
    dataset_type: String,
    template_version_id: Option<Uuid>,
    fields: Vec<ImportMappingField>,
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
    #[error("invalid template configuration stored in database")]
    InvalidTemplateConfiguration,
    #[error("mapping does not match a supported dataset definition")]
    InvalidMappingDefinition(ImportMappingDefinitionError),
    #[error("template version is not visible")]
    TemplateVersionNotFound,
    #[error("mapping does not match the selected template version")]
    TemplateVersionMismatch,
    #[error("inspection or mapping changed before preview could be saved")]
    PreviewInputsChanged,
    #[error("validation is required")]
    ValidationRequired,
    #[error("validation result is stale")]
    ValidationStale,
    #[error("blocking validation errors are present")]
    BlockingErrorsPresent,
    #[error("conflict policy is not allowed")]
    ConflictPolicyNotAllowed,
    #[error("idempotency key was reused with different parameters")]
    IdempotencyKeyReused,
    #[error("batch was already confirmed with different parameters")]
    ConfirmationConflict,
    #[error("event cursor is invalid")]
    EventIdInvalid,
}

#[derive(Debug, Clone)]
pub struct ValidationContext {
    pub dataset_type: String,
    pub mapping_id: Uuid,
    pub mapping_hash: String,
    pub staging_version: i64,
    pub rows: Vec<StagingRowInput>,
    pub detected_encoding: Option<String>,
    pub detected_delimiter: Option<String>,
    pub selected_sheet: Option<String>,
    pub header_row: i32,
    pub mapping_fields: Vec<ImportMappingField>,
}

#[derive(Debug, Clone)]
pub struct SavedValidation {
    pub validation_version: i32,
    pub blocking_error_count: u32,
    pub warning_count: u32,
    pub duplicate_count: u32,
    pub conflict_count: u32,
}

#[derive(Debug, Clone)]
pub struct ErrorPage {
    pub items: Vec<ImportErrorPreview>,
    pub next_cursor: Option<String>,
}

#[derive(Debug, Clone)]
pub struct ConfirmedImport {
    pub job_id: Uuid,
    pub status: String,
    pub replayed: bool,
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
        "insert into import_batches (id, workspace_id, status, dataset_type, created_by)
         values ($1, $2, 'uploaded', 'generic', $3)
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
        object_key: upload.object_key.clone(),
        original_filename: upload.original_filename.clone(),
        declared_mime_type: upload.declared_mime_type.clone(),
        detected_format: upload.detected_format.clone(),
        sha256: upload.sha256.clone(),
        size_bytes: upload.size_bytes,
        created_at: batch_row.get("created_at"),
        updated_at: batch_row.get("updated_at"),
        validation_version: None,
        staging_version: 0,
        blocking_error_count: 0,
        warning_count: 0,
        duplicate_count: 0,
        conflict_count: 0,
        conflict_policy: None,
        job_id: None,
        job_status: None,
        job_attempt_count: None,
        job_max_attempts: None,
        job_error_code: None,
        total_rows: 0,
        processed_count: 0,
        imported_count: 0,
        skipped_count: 0,
        overwritten_count: 0,
        conflict_result_count: 0,
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

#[allow(clippy::too_many_arguments)]
pub async fn record_import_audit(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    request_id: Uuid,
    import_id: Option<Uuid>,
    event_type: &str,
    outcome: &str,
    reason_code: &str,
) -> Result<(), ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    sqlx::query(
        "insert into audit_logs
            (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, $4, $5, $6,
                 jsonb_strip_nulls(jsonb_build_object(
                    'import_id', $7::text, 'reason_code', $8::text
                 )))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(event_type)
    .bind(outcome)
    .bind(request_id)
    .bind(import_id)
    .bind(reason_code)
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
                b.validation_version, b.staging_version, b.blocking_error_count,
                b.warning_count, b.duplicate_count, b.conflict_count,
                b.conflict_policy, b.processed_count, b.imported_count,
                b.skipped_count, b.overwritten_count, b.conflict_result_count,
                (
                    select count(*) from import_staging_rows staging
                    where staging.workspace_id = b.workspace_id
                      and staging.import_batch_id = b.id
                      and staging.staging_version = b.staging_version
                ) as total_rows,
                f.id as file_id, f.original_filename, f.declared_mime_type,
                f.detected_format, f.sha256, f.size_bytes, o.object_key,
                j.id as job_id, j.status as job_status, j.attempt_count as job_attempt_count,
                j.max_attempts as job_max_attempts, j.last_error_code as job_error_code
         from import_batches b
         join import_files f
           on f.workspace_id = b.workspace_id and f.import_batch_id = b.id
         join stored_objects o
           on o.workspace_id = f.workspace_id and o.id = f.stored_object_id
         left join lateral (
            select id, status, attempt_count, max_attempts, last_error_code
            from job_queue
            where workspace_id = b.workspace_id and aggregate_id = b.id
              and job_type = 'import_confirm'
            order by created_at, id
            limit 1
         ) j on true
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
        object_key: row.get("object_key"),
        original_filename: row.get("original_filename"),
        declared_mime_type: row.get("declared_mime_type"),
        detected_format: row.get("detected_format"),
        sha256: row.get("sha256"),
        size_bytes: row.get("size_bytes"),
        created_at: row.get("created_at"),
        updated_at: row.get("updated_at"),
        validation_version: row.get("validation_version"),
        staging_version: row.get("staging_version"),
        blocking_error_count: row.get("blocking_error_count"),
        warning_count: row.get("warning_count"),
        duplicate_count: row.get("duplicate_count"),
        conflict_count: row.get("conflict_count"),
        conflict_policy: row.get("conflict_policy"),
        job_id: row.get("job_id"),
        job_status: row.get("job_status"),
        job_attempt_count: row.get("job_attempt_count"),
        job_max_attempts: row.get("job_max_attempts"),
        job_error_code: row.get("job_error_code"),
        total_rows: row.get("total_rows"),
        processed_count: row.get("processed_count"),
        imported_count: row.get("imported_count"),
        skipped_count: row.get("skipped_count"),
        overwritten_count: row.get("overwritten_count"),
        conflict_result_count: row.get("conflict_result_count"),
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

pub async fn save_inspection(
    pool: &PgPool,
    update: InspectionUpdate<'_>,
) -> Result<InspectionSaveResult, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    let workspace_id = update.workspace_id;
    let import_id = update.import_id;
    set_workspace(&mut tx, workspace_id).await?;
    let status = lock_import_batch(&mut tx, workspace_id, import_id).await?;
    if !matches!(
        status,
        ImportBatchStatus::Uploaded
            | ImportBatchStatus::Inspected
            | ImportBatchStatus::Mapped
            | ImportBatchStatus::PreviewReady
    ) {
        return Err(ImportRepositoryError::InvalidTransition);
    }
    let previous = sqlx::query(
        "select detected_encoding, detected_delimiter, selected_sheet, header_row
         from import_files
         where workspace_id = $1 and import_batch_id = $2
         for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    let parameters_changed = previous.get::<Option<String>, _>("detected_encoding")
        != update.detected_encoding.map(str::to_string)
        || previous.get::<Option<String>, _>("detected_delimiter")
            != update.detected_delimiter.map(str::to_string)
        || previous.get::<Option<String>, _>("selected_sheet")
            != update.selected_sheet.map(str::to_string)
        || previous.get::<Option<i32>, _>("header_row") != Some(update.header_row);
    sqlx::query(
        "update import_files
         set detected_encoding = $1,
             detected_delimiter = $2,
             selected_sheet = $3,
             header_row = $4,
             inspected_at = now(),
             updated_at = now()
         where workspace_id = $5 and import_batch_id = $6",
    )
    .bind(update.detected_encoding)
    .bind(update.detected_delimiter)
    .bind(update.selected_sheet)
    .bind(update.header_row)
    .bind(workspace_id)
    .bind(import_id)
    .execute(&mut *tx)
    .await?;
    let (status, preview_invalidated) = if status == ImportBatchStatus::Uploaded {
        update_status_in_tx(
            &mut tx,
            workspace_id,
            import_id,
            ImportBatchStatus::Uploaded,
            ImportBatchStatus::Inspected,
        )
        .await?;
        (ImportBatchStatus::Inspected, false)
    } else if preview_invalidation_required(status, parameters_changed) {
        invalidate_preview_in_tx(&mut tx, workspace_id, import_id, status).await?;
        (ImportBatchStatus::Mapped, true)
    } else {
        if parameters_changed {
            delete_preview_data_in_tx(&mut tx, workspace_id, import_id).await?;
        }
        (status, false)
    };
    sqlx::query(
        "insert into audit_logs
            (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, 'import.inspect', 'success', $4, jsonb_build_object('import_id', $5::text))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(update.actor_user_id)
    .bind(Uuid::now_v7())
    .bind(import_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(InspectionSaveResult {
        status,
        preview_invalidated,
    })
}

pub async fn get_mapping_fields(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<Vec<ImportMappingField>, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let row = sqlx::query(
        "select mapping_json
         from import_mappings
         where workspace_id = $1 and import_batch_id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?;
    tx.commit().await?;
    let Some(row) = row else {
        return Ok(Vec::new());
    };
    let value: serde_json::Value = row.get("mapping_json");
    parse_fields_from_value(value)
}

pub async fn save_mapping(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    import_id: Uuid,
    dataset_type: &str,
    template_version_id: Option<Uuid>,
    fields: &[ImportMappingField],
) -> Result<ImportMappingResponse, ImportRepositoryError> {
    validate_mapping_fields(dataset_type, fields)
        .map_err(ImportRepositoryError::InvalidMappingDefinition)?;
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let status = lock_import_batch(&mut tx, workspace_id, import_id).await?;
    if !matches!(
        status,
        ImportBatchStatus::Inspected | ImportBatchStatus::Mapped | ImportBatchStatus::PreviewReady
    ) {
        return Err(ImportRepositoryError::InvalidTransition);
    }
    let existing_mapping = existing_mapping_for_update(&mut tx, workspace_id, import_id).await?;
    if !template_version_binding_is_allowed(
        existing_mapping
            .as_ref()
            .and_then(|mapping| mapping.template_version_id),
        template_version_id,
    ) {
        return Err(ImportRepositoryError::TemplateVersionMismatch);
    }
    if let Some(template_version_id) = template_version_id {
        validate_template_version_binding(
            &mut tx,
            workspace_id,
            template_version_id,
            dataset_type,
            fields,
        )
        .await?;
    }
    let mapping_changed = mapping_has_changed(
        existing_mapping.as_ref(),
        dataset_type,
        template_version_id,
        fields,
    );
    let (status, preview_invalidated) = if mapping_changed {
        if status == ImportBatchStatus::PreviewReady {
            invalidate_preview_in_tx(&mut tx, workspace_id, import_id, status).await?;
            (ImportBatchStatus::Mapped, true)
        } else {
            delete_preview_data_in_tx(&mut tx, workspace_id, import_id).await?;
            (status, false)
        }
    } else {
        (status, false)
    };
    let mapping_json = json!({ "fields": fields });
    let affected = sqlx::query(
        "insert into import_mappings
            (id, workspace_id, import_batch_id, template_version_id, dataset_type, mapping_json, created_by)
         values ($1, $2, $3, $4, $5, $6, $7)
         on conflict (workspace_id, import_batch_id) do update
         set template_version_id = excluded.template_version_id,
             dataset_type = excluded.dataset_type,
             mapping_json = excluded.mapping_json,
             updated_at = now()
          where import_mappings.template_version_id is null
             or import_mappings.template_version_id = excluded.template_version_id",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(import_id)
    .bind(template_version_id)
    .bind(dataset_type)
    .bind(mapping_json)
    .bind(actor_user_id)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    if affected != 1 {
        return Err(ImportRepositoryError::TemplateVersionMismatch);
    }
    sqlx::query(
        "update import_batches
         set dataset_type = $1, updated_at = now()
         where workspace_id = $2 and id = $3",
    )
    .bind(dataset_type)
    .bind(workspace_id)
    .bind(import_id)
    .execute(&mut *tx)
    .await?;
    let status = if status == ImportBatchStatus::Inspected {
        update_status_in_tx(
            &mut tx,
            workspace_id,
            import_id,
            ImportBatchStatus::Inspected,
            ImportBatchStatus::Mapped,
        )
        .await?;
        ImportBatchStatus::Mapped
    } else {
        status
    };
    insert_audit_event(
        &mut tx,
        workspace_id,
        actor_user_id,
        "import.mapping",
        import_id,
    )
    .await?;
    tx.commit().await?;
    Ok(ImportMappingResponse {
        import_id,
        status,
        dataset_type: dataset_type.to_string(),
        template_version_id,
        fields: fields.to_vec(),
        preview_invalidated,
    })
}

pub async fn save_preview(
    pool: &PgPool,
    update: PreviewSave<'_>,
) -> Result<(), ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    let workspace_id = update.workspace_id;
    let import_id = update.import_id;
    set_workspace(&mut tx, workspace_id).await?;
    let status = lock_import_batch(&mut tx, workspace_id, import_id).await?;
    if !matches!(
        status,
        ImportBatchStatus::Mapped | ImportBatchStatus::PreviewReady
    ) {
        return Err(ImportRepositoryError::InvalidTransition);
    }
    if !preview_inputs_match(&mut tx, workspace_id, import_id, &update.snapshot).await? {
        return Err(ImportRepositoryError::PreviewInputsChanged);
    }
    delete_preview_data_in_tx(&mut tx, workspace_id, import_id).await?;
    let staging_version = sqlx::query_scalar::<_, i64>(
        "update import_batches
         set staging_version = staging_version + 1,
             validation_version = null,
             validated_staging_version = null,
             validated_mapping_id = null,
             validated_mapping_hash = null,
             validated_at = null,
             blocking_error_count = 0,
             warning_count = 0,
             duplicate_count = 0,
             conflict_count = 0,
             updated_at = now()
         where workspace_id = $1 and id = $2
         returning staging_version",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_one(&mut *tx)
    .await?;
    for row in update.rows {
        let raw_values = json_object_from_cells(row, |cell| json!(cell.raw_value));
        let normalized_values = json_object_from_cells(row, |cell| json!(cell.normalized_value));
        let target_fields = json_object_from_cells(row, |cell| {
            json!(cell.target_field.as_deref().unwrap_or(""))
        });
        sqlx::query(
            "insert into import_staging_rows
                (id, workspace_id, import_batch_id, row_number, raw_values, normalized_values,
                 target_fields, warnings, created_by, staging_version)
             values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
        )
        .bind(Uuid::now_v7())
        .bind(workspace_id)
        .bind(import_id)
        .bind(row.row_number as i32)
        .bind(raw_values)
        .bind(normalized_values)
        .bind(target_fields)
        .bind(json!(&row.warnings))
        .bind(update.actor_user_id)
        .bind(staging_version)
        .execute(&mut *tx)
        .await?;
    }
    for item in update.errors.iter().chain(update.warnings.iter()) {
        insert_error(&mut tx, workspace_id, update.actor_user_id, import_id, item).await?;
    }
    if status == ImportBatchStatus::Mapped {
        update_status_in_tx(
            &mut tx,
            workspace_id,
            import_id,
            ImportBatchStatus::Mapped,
            ImportBatchStatus::PreviewReady,
        )
        .await?;
    }
    insert_audit_event(
        &mut tx,
        workspace_id,
        update.actor_user_id,
        "import.preview",
        import_id,
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

pub async fn load_validation_context(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<ValidationContext, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let batch = sqlx::query(
        "select status::text as status, dataset_type, staging_version
         from import_batches
         where workspace_id = $1 and id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    if batch.get::<String, _>("status") != "preview_ready" {
        return Err(ImportRepositoryError::InvalidTransition);
    }
    let mapping = sqlx::query(
        "select id, mapping_json
         from import_mappings
         where workspace_id = $1 and import_batch_id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ImportRepositoryError::ValidationRequired)?;
    let mapping_json: serde_json::Value = mapping.get("mapping_json");
    let mapping_fields = parse_fields_from_value(mapping_json.clone())?;
    let mapping_hash = format!("{:x}", Sha256::digest(mapping_json.to_string().as_bytes()));
    let rows = sqlx::query(
        "select id, row_number, normalized_values, target_fields, warnings
         from import_staging_rows
         where workspace_id = $1 and import_batch_id = $2 and staging_version = $3
         order by row_number, id",
    )
    .bind(workspace_id)
    .bind(import_id)
    .bind(batch.get::<i64, _>("staging_version"))
    .fetch_all(&mut *tx)
    .await?
    .into_iter()
    .map(|row| StagingRowInput {
        id: row.get("id"),
        row_number: row.get::<i32, _>("row_number") as u32,
        normalized_values: row.get("normalized_values"),
        target_fields: row.get("target_fields"),
        preview_warnings: row.get("warnings"),
    })
    .collect();
    let file = sqlx::query(
        "select detected_encoding, detected_delimiter, selected_sheet, header_row
         from import_files
         where workspace_id = $1 and import_batch_id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_one(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(ValidationContext {
        dataset_type: batch.get("dataset_type"),
        mapping_id: mapping.get("id"),
        mapping_hash,
        staging_version: batch.get("staging_version"),
        rows,
        detected_encoding: file.get("detected_encoding"),
        detected_delimiter: file.get("detected_delimiter"),
        selected_sheet: file.get("selected_sheet"),
        header_row: file.get::<Option<i32>, _>("header_row").unwrap_or(1),
        mapping_fields,
    })
}

pub async fn save_validation(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    import_id: Uuid,
    context: &ValidationContext,
    outcome: &ValidationOutcome,
) -> Result<SavedValidation, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let batch = sqlx::query(
        "select status::text as status, staging_version
         from import_batches
         where workspace_id = $1 and id = $2
         for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    if batch.get::<String, _>("status") != "preview_ready"
        || batch.get::<i64, _>("staging_version") != context.staging_version
    {
        return Err(ImportRepositoryError::ValidationStale);
    }
    let mapping = sqlx::query(
        "select id, mapping_json
         from import_mappings
         where workspace_id = $1 and import_batch_id = $2
         for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ImportRepositoryError::ValidationStale)?;
    let mapping_json: serde_json::Value = mapping.get("mapping_json");
    let mapping_hash = format!("{:x}", Sha256::digest(mapping_json.to_string().as_bytes()));
    if mapping.get::<Uuid, _>("id") != context.mapping_id || mapping_hash != context.mapping_hash {
        return Err(ImportRepositoryError::ValidationStale);
    }
    sqlx::query(
        "delete from import_errors
         where workspace_id = $1 and import_batch_id = $2 and validation_version is not null",
    )
    .bind(workspace_id)
    .bind(import_id)
    .execute(&mut *tx)
    .await?;

    let mut conflict_count = 0_u32;
    for row in &outcome.rows {
        let has_database_conflict = if let Some(key) = &row.business_key {
            sqlx::query_scalar::<_, bool>(
                "select exists(
                    select 1 from imported_records
                    where workspace_id = $1 and dataset_type = $2 and business_key = $3
                 )",
            )
            .bind(workspace_id)
            .bind(&context.dataset_type)
            .bind(key)
            .fetch_one(&mut *tx)
            .await?
        } else {
            false
        };
        conflict_count += u32::from(has_database_conflict);
        sqlx::query(
            "update import_staging_rows
             set validation_version = $1, business_key = $2, record_data = $3,
                 is_file_duplicate = $4, has_database_conflict = $5, validated_at = now()
             where workspace_id = $6 and import_batch_id = $7 and id = $8
               and staging_version = $9",
        )
        .bind(application::import_jobs::IMPORT_VALIDATION_VERSION)
        .bind(&row.business_key)
        .bind(&row.record_data)
        .bind(row.duplicate)
        .bind(has_database_conflict)
        .bind(workspace_id)
        .bind(import_id)
        .bind(row.staging_row_id)
        .bind(context.staging_version)
        .execute(&mut *tx)
        .await?;
        for item in row.blocking_errors.iter().chain(row.warnings.iter()) {
            sqlx::query(
                "insert into import_errors
                   (id, workspace_id, import_batch_id, staging_row_id, row_number,
                    field_name, severity, error_code, raw_value, message, created_by,
                    staging_version, validation_version, error_kind)
                 values ($1, $2, $3, $4, $5, $6, $7, $8, null, $9, $10, $11, $12, 'validation')",
            )
            .bind(Uuid::now_v7())
            .bind(workspace_id)
            .bind(import_id)
            .bind(row.staging_row_id)
            .bind(item.row_number.map(|value| value as i32))
            .bind(&item.field_name)
            .bind(item.severity.as_str())
            .bind(&item.error_code)
            .bind(&item.message)
            .bind(actor_user_id)
            .bind(context.staging_version)
            .bind(application::import_jobs::IMPORT_VALIDATION_VERSION)
            .execute(&mut *tx)
            .await?;
        }
    }
    let preserved_warning_count = sqlx::query_scalar::<_, i64>(
        "select count(*) from import_errors
         where workspace_id = $1 and import_batch_id = $2
           and validation_version is null and severity = 'warning'",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_one(&mut *tx)
    .await? as u32;
    let warning_count = outcome
        .warning_count
        .saturating_add(preserved_warning_count);
    sqlx::query(
        "update import_batches
         set validation_version = $1, validated_staging_version = $2,
             validated_mapping_id = $3, validated_mapping_hash = $4,
             validated_at = now(), blocking_error_count = $5, warning_count = $6,
             duplicate_count = $7, conflict_count = $8, updated_at = now()
         where workspace_id = $9 and id = $10",
    )
    .bind(application::import_jobs::IMPORT_VALIDATION_VERSION)
    .bind(context.staging_version)
    .bind(context.mapping_id)
    .bind(&context.mapping_hash)
    .bind(outcome.blocking_error_count as i32)
    .bind(warning_count as i32)
    .bind(outcome.duplicate_count as i32)
    .bind(conflict_count as i32)
    .bind(workspace_id)
    .bind(import_id)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "insert into audit_logs
           (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, 'import.validate', 'success', $4,
                 jsonb_build_object(
                   'import_id', $5::text, 'blocking_error_count', $6::int,
                   'warning_count', $7::int, 'duplicate_count', $8::int,
                   'conflict_count', $9::int
                 ))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(Uuid::now_v7())
    .bind(import_id)
    .bind(outcome.blocking_error_count as i32)
    .bind(warning_count as i32)
    .bind(outcome.duplicate_count as i32)
    .bind(conflict_count as i32)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(SavedValidation {
        validation_version: application::import_jobs::IMPORT_VALIDATION_VERSION,
        blocking_error_count: outcome.blocking_error_count,
        warning_count,
        duplicate_count: outcome.duplicate_count,
        conflict_count,
    })
}

pub async fn confirm_import(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    import_id: Uuid,
    policy: ImportConflictPolicy,
    idempotency_key_hash: &str,
    request_hash: &str,
) -> Result<ConfirmedImport, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    // The idempotency identity is workspace-wide, not batch-local. Serialize
    // the hashed key before taking the batch lock so concurrent requests for
    // different batches cannot race into the unique constraint and surface a
    // database error. All confirm paths use advisory-key -> batch lock order.
    sqlx::query(CONFIRM_IDEMPOTENCY_LOCK_SQL)
        .bind(workspace_id)
        .bind(idempotency_key_hash)
        .execute(&mut *tx)
        .await?;
    let batch = sqlx::query(
        "select status::text as status, dataset_type, staging_version,
                validated_staging_version, validation_version, validated_mapping_id,
                blocking_error_count, duplicate_count, conflict_count,
                confirmation_fingerprint
         from import_batches
         where workspace_id = $1 and id = $2
         for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;

    if let Some(existing) = sqlx::query(
        "select request_hash, job_id
         from import_confirmations
         where workspace_id = $1 and idempotency_key_hash = $2",
    )
    .bind(workspace_id)
    .bind(idempotency_key_hash)
    .fetch_optional(&mut *tx)
    .await?
    {
        if existing.get::<String, _>("request_hash") != request_hash {
            insert_confirmation_audit(
                &mut tx,
                workspace_id,
                actor_user_id,
                import_id,
                "import.confirm_idempotency_conflict",
                "failure",
                Some(&idempotency_key_hash[..12]),
            )
            .await?;
            tx.commit().await?;
            return Err(ImportRepositoryError::IdempotencyKeyReused);
        }
        let job_id: Uuid = existing.get("job_id");
        let status = sqlx::query_scalar::<_, String>(
            "select status::text from job_queue where workspace_id = $1 and id = $2",
        )
        .bind(workspace_id)
        .bind(job_id)
        .fetch_one(&mut *tx)
        .await?;
        insert_confirmation_audit(
            &mut tx,
            workspace_id,
            actor_user_id,
            import_id,
            "import.confirm_replayed",
            "success",
            Some(&idempotency_key_hash[..12]),
        )
        .await?;
        tx.commit().await?;
        return Ok(ConfirmedImport {
            job_id,
            status,
            replayed: true,
        });
    }

    let status: String = batch.get("status");
    let confirmation_fingerprint: Option<String> = batch.get("confirmation_fingerprint");
    if matches!(
        status.as_str(),
        "confirmed" | "importing" | "succeeded" | "failed"
    ) {
        if confirmation_fingerprint.as_deref() != Some(request_hash) {
            insert_confirmation_audit(
                &mut tx,
                workspace_id,
                actor_user_id,
                import_id,
                "import.confirmation_conflict",
                "failure",
                Some(&idempotency_key_hash[..12]),
            )
            .await?;
            tx.commit().await?;
            return Err(ImportRepositoryError::ConfirmationConflict);
        }
        let job_id = sqlx::query_scalar::<_, Uuid>(
            "select id from job_queue
             where workspace_id = $1 and job_type = 'import_confirm' and aggregate_id = $2",
        )
        .bind(workspace_id)
        .bind(import_id)
        .fetch_one(&mut *tx)
        .await?;
        sqlx::query(
            "insert into import_confirmations
               (id, workspace_id, import_batch_id, idempotency_key_hash, request_hash,
                job_id, confirmed_by)
             values ($1, $2, $3, $4, $5, $6, $7)",
        )
        .bind(Uuid::now_v7())
        .bind(workspace_id)
        .bind(import_id)
        .bind(idempotency_key_hash)
        .bind(request_hash)
        .bind(job_id)
        .bind(actor_user_id)
        .execute(&mut *tx)
        .await?;
        let job_status = sqlx::query_scalar::<_, String>(
            "select status::text from job_queue where workspace_id = $1 and id = $2",
        )
        .bind(workspace_id)
        .bind(job_id)
        .fetch_one(&mut *tx)
        .await?;
        tx.commit().await?;
        return Ok(ConfirmedImport {
            job_id,
            status: job_status,
            replayed: true,
        });
    }
    if status != "preview_ready" {
        return Err(ImportRepositoryError::InvalidTransition);
    }
    if batch.get::<Option<i32>, _>("validation_version").is_none() {
        return Err(ImportRepositoryError::ValidationRequired);
    }
    if batch.get::<i64, _>("staging_version")
        != batch
            .get::<Option<i64>, _>("validated_staging_version")
            .ok_or(ImportRepositoryError::ValidationStale)?
        || batch
            .get::<Option<Uuid>, _>("validated_mapping_id")
            .is_none()
    {
        return Err(ImportRepositoryError::ValidationStale);
    }
    if batch.get::<i32, _>("blocking_error_count") > 0 {
        return Err(ImportRepositoryError::BlockingErrorsPresent);
    }
    if batch.get::<String, _>("dataset_type") != "generic" {
        return Err(ImportRepositoryError::ConflictPolicyNotAllowed);
    }
    if policy == ImportConflictPolicy::Abort
        && (batch.get::<i32, _>("duplicate_count") > 0 || batch.get::<i32, _>("conflict_count") > 0)
    {
        return Err(ImportRepositoryError::BlockingErrorsPresent);
    }

    let job_id = Uuid::now_v7();
    sqlx::query(
        "insert into job_queue
           (id, workspace_id, job_type, aggregate_id, status, payload,
            attempt_count, max_attempts, available_at)
         values ($1, $2, 'import_confirm', $3, 'queued',
                 jsonb_build_object('import_id', $3::text), 0, 5, now())",
    )
    .bind(job_id)
    .bind(workspace_id)
    .bind(import_id)
    .execute(&mut *tx)
    .await?;
    sqlx::query(CONFIRM_BATCH_SQL)
        .bind(policy.as_str())
        .bind(request_hash)
        .bind(actor_user_id)
        .bind(workspace_id)
        .bind(import_id)
        .execute(&mut *tx)
        .await?;
    sqlx::query(
        "insert into import_confirmations
           (id, workspace_id, import_batch_id, idempotency_key_hash, request_hash,
            job_id, confirmed_by)
         values ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(import_id)
    .bind(idempotency_key_hash)
    .bind(request_hash)
    .bind(job_id)
    .bind(actor_user_id)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "insert into import_job_events
           (id, workspace_id, import_batch_id, job_id, event_seq, event_type, payload)
         values ($1, $2, $3, $4, 1, 'queued',
                 jsonb_build_object(
                    'status', 'queued', 'processed_rows', 0, 'total_rows', 0,
                    'inserted_count', 0, 'updated_count', 0,
                    'skipped_count', 0, 'conflict_count', 0
                 ))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(import_id)
    .bind(job_id)
    .execute(&mut *tx)
    .await?;
    insert_confirmation_audit(
        &mut tx,
        workspace_id,
        actor_user_id,
        import_id,
        "import.confirmed",
        "success",
        Some(&idempotency_key_hash[..12]),
    )
    .await?;
    tx.commit().await?;
    Ok(ConfirmedImport {
        job_id,
        status: "queued".to_string(),
        replayed: false,
    })
}

pub async fn list_errors(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
    cursor: Option<&str>,
    limit: u32,
) -> Result<ErrorPage, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let _ = current_status(&mut tx, workspace_id, import_id).await?;
    let (cursor_row, cursor_created, cursor_id) = cursor
        .map(|value| parse_error_cursor(value, workspace_id, import_id))
        .transpose()?
        .unwrap_or((0, OffsetDateTime::UNIX_EPOCH, Uuid::nil()));
    let page_size = limit.clamp(1, 200);
    let rows = sqlx::query(
        "select id, created_at, row_number, field_name, severity, error_code, raw_value, message
         from import_errors
         where workspace_id = $1 and import_batch_id = $2
           and (coalesce(row_number, 0), created_at, id) > ($3, $4, $5)
         order by coalesce(row_number, 0), created_at, id
         limit $6",
    )
    .bind(workspace_id)
    .bind(import_id)
    .bind(cursor_row)
    .bind(cursor_created)
    .bind(cursor_id)
    .bind(i64::from(page_size) + 1)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    let has_more = rows.len() > page_size as usize;
    let visible = rows
        .into_iter()
        .take(page_size as usize)
        .collect::<Vec<_>>();
    let next_cursor = if has_more {
        visible.last().map(|row| {
            format_error_cursor(
                workspace_id,
                import_id,
                row.get::<Option<i32>, _>("row_number").unwrap_or(0),
                row.get("created_at"),
                row.get("id"),
            )
        })
    } else {
        None
    };
    let items = visible
        .into_iter()
        .map(|row| {
            let severity: String = row.get("severity");
            Ok::<ImportErrorPreview, ImportRepositoryError>(ImportErrorPreview {
                row_number: row
                    .get::<Option<i32>, _>("row_number")
                    .map(|value| value as u32),
                field_name: row.get("field_name"),
                severity: ImportErrorSeverity::parse(&severity)
                    .ok_or(ImportRepositoryError::InvalidStoredStatus)?,
                error_code: row.get("error_code"),
                raw_value: row.get("raw_value"),
                message: row.get("message"),
            })
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(ErrorPage { items, next_cursor })
}

pub async fn create_template(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    dataset_type: &str,
    name: &str,
    description: Option<&str>,
    fields: &[ImportMappingField],
) -> Result<ImportTemplateVersionResponse, ImportRepositoryError> {
    validate_mapping_fields(dataset_type, fields)
        .map_err(ImportRepositoryError::InvalidMappingDefinition)?;
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let template_id = Uuid::now_v7();
    let version_id = Uuid::now_v7();
    sqlx::query(
        "insert into import_templates
            (id, workspace_id, dataset_type, name, description, created_by)
         values ($1, $2, $3, $4, $5, $6)",
    )
    .bind(template_id)
    .bind(workspace_id)
    .bind(dataset_type)
    .bind(name)
    .bind(description)
    .bind(actor_user_id)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "insert into import_template_versions
            (id, workspace_id, template_id, version_number, dataset_type,
             configuration_json, created_by)
         values ($1, $2, $3, 1, $4, $5, $6)",
    )
    .bind(version_id)
    .bind(workspace_id)
    .bind(template_id)
    .bind(dataset_type)
    .bind(json!({ "fields": fields }))
    .bind(actor_user_id)
    .execute(&mut *tx)
    .await?;
    insert_audit_event(
        &mut tx,
        workspace_id,
        actor_user_id,
        "import.template",
        template_id,
    )
    .await?;
    tx.commit().await?;
    Ok(ImportTemplateVersionResponse {
        id: version_id,
        template_id,
        version_number: 1,
        dataset_type: dataset_type.to_string(),
        fields: fields.to_vec(),
    })
}

pub async fn list_templates(
    pool: &PgPool,
    workspace_id: Uuid,
) -> Result<Vec<ImportTemplateSummary>, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(
        "select t.id, t.dataset_type, t.name, t.description,
                v.id as latest_version_id, v.version_number as latest_version_number,
                v.configuration_json
         from import_templates t
         join lateral (
             select id, version_number, configuration_json
             from import_template_versions v
             where v.workspace_id = t.workspace_id and v.template_id = t.id
             order by version_number desc
             limit 1
         ) v on true
         where t.workspace_id = $1
         order by t.created_at desc, t.id",
    )
    .bind(workspace_id)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    rows.into_iter()
        .map(|row| {
            let configuration: serde_json::Value = row.get("configuration_json");
            Ok(ImportTemplateSummary {
                id: row.get("id"),
                dataset_type: row.get("dataset_type"),
                name: row.get("name"),
                description: row.get("description"),
                latest_version_id: row.get("latest_version_id"),
                latest_version_number: row.get("latest_version_number"),
                fields: parse_fields_from_value(configuration)?,
            })
        })
        .collect()
}

async fn existing_mapping_for_update(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<Option<ExistingMapping>, ImportRepositoryError> {
    let row = sqlx::query(
        "select dataset_type, template_version_id, mapping_json
         from import_mappings
         where workspace_id = $1 and import_batch_id = $2
         for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut **tx)
    .await?;
    row.map(|row| {
        Ok(ExistingMapping {
            dataset_type: row.get("dataset_type"),
            template_version_id: row.get("template_version_id"),
            fields: parse_fields_from_value(row.get("mapping_json"))?,
        })
    })
    .transpose()
}

async fn validate_template_version_binding(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    template_version_id: Uuid,
    dataset_type: &str,
    fields: &[ImportMappingField],
) -> Result<(), ImportRepositoryError> {
    let row = sqlx::query(
        "select v.dataset_type, v.configuration_json
         from import_template_versions v
         where v.workspace_id = $1 and v.id = $2",
    )
    .bind(workspace_id)
    .bind(template_version_id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(ImportRepositoryError::TemplateVersionNotFound)?;
    let template_dataset_type: String = row.get("dataset_type");
    let configuration: serde_json::Value = row.get("configuration_json");
    let template_fields = parse_fields_from_value(configuration)?;
    if !template_binding_matches(
        &template_dataset_type,
        dataset_type,
        &template_fields,
        fields,
    ) {
        return Err(ImportRepositoryError::TemplateVersionMismatch);
    }
    Ok(())
}

fn template_binding_matches(
    template_dataset_type: &str,
    mapping_dataset_type: &str,
    template_fields: &[ImportMappingField],
    mapping_fields: &[ImportMappingField],
) -> bool {
    template_dataset_type == mapping_dataset_type && template_fields == mapping_fields
}

fn template_version_binding_is_allowed(existing: Option<Uuid>, requested: Option<Uuid>) -> bool {
    existing.is_none() || existing == requested
}

fn mapping_has_changed(
    existing: Option<&ExistingMapping>,
    dataset_type: &str,
    template_version_id: Option<Uuid>,
    fields: &[ImportMappingField],
) -> bool {
    match existing {
        None => true,
        Some(mapping) => {
            mapping.dataset_type != dataset_type
                || mapping.template_version_id != template_version_id
                || mapping.fields.as_slice() != fields
        }
    }
}

fn preview_invalidation_required(status: ImportBatchStatus, configuration_changed: bool) -> bool {
    configuration_changed && status == ImportBatchStatus::PreviewReady
}

async fn current_status(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<ImportBatchStatus, ImportRepositoryError> {
    let row = sqlx::query(
        "select status::text as status
         from import_batches
         where workspace_id = $1 and id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    let status: String = row.get("status");
    ImportBatchStatus::parse(&status).ok_or(ImportRepositoryError::InvalidStoredStatus)
}

async fn lock_import_batch(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<ImportBatchStatus, ImportRepositoryError> {
    let row = sqlx::query(
        "select status::text as status
         from import_batches
         where workspace_id = $1 and id = $2
         for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    let status: String = row.get("status");
    ImportBatchStatus::parse(&status).ok_or(ImportRepositoryError::InvalidStoredStatus)
}

async fn preview_inputs_match(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
    snapshot: &PreviewInputSnapshot<'_>,
) -> Result<bool, ImportRepositoryError> {
    let file = sqlx::query(
        "select detected_encoding, detected_delimiter, selected_sheet, header_row
         from import_files
         where workspace_id = $1 and import_batch_id = $2
         for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    let mapping = existing_mapping_for_update(tx, workspace_id, import_id)
        .await?
        .ok_or(ImportRepositoryError::PreviewInputsChanged)?;
    Ok(file.get::<Option<String>, _>("detected_encoding")
        == snapshot.detected_encoding.map(str::to_string)
        && file.get::<Option<String>, _>("detected_delimiter")
            == snapshot.detected_delimiter.map(str::to_string)
        && file.get::<Option<String>, _>("selected_sheet")
            == snapshot.selected_sheet.map(str::to_string)
        && file.get::<Option<i32>, _>("header_row") == Some(snapshot.header_row)
        && mapping.fields.as_slice() == snapshot.fields)
}

async fn delete_preview_data_in_tx(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<(), ImportRepositoryError> {
    sqlx::query("delete from import_errors where workspace_id = $1 and import_batch_id = $2")
        .bind(workspace_id)
        .bind(import_id)
        .execute(&mut **tx)
        .await?;
    sqlx::query("delete from import_staging_rows where workspace_id = $1 and import_batch_id = $2")
        .bind(workspace_id)
        .bind(import_id)
        .execute(&mut **tx)
        .await?;
    Ok(())
}

async fn invalidate_preview_in_tx(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
    from: ImportBatchStatus,
) -> Result<(), ImportRepositoryError> {
    delete_preview_data_in_tx(tx, workspace_id, import_id).await?;
    update_status_in_tx(tx, workspace_id, import_id, from, ImportBatchStatus::Mapped).await
}

async fn update_status_in_tx(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
    from: ImportBatchStatus,
    to: ImportBatchStatus,
) -> Result<(), ImportRepositoryError> {
    ensure_status_transition(from, to).map_err(|_| ImportRepositoryError::InvalidTransition)?;
    let affected = sqlx::query(
        "update import_batches
          set status = $1::import_batch_status, updated_at = now()
          where workspace_id = $2 and id = $3 and status = $4::import_batch_status",
    )
    .bind(to.as_str())
    .bind(workspace_id)
    .bind(import_id)
    .bind(from.as_str())
    .execute(&mut **tx)
    .await?
    .rows_affected();
    if affected == 1 {
        Ok(())
    } else {
        Err(ImportRepositoryError::InvalidTransition)
    }
}

async fn insert_audit_event(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    event_type: &'static str,
    resource_id: Uuid,
) -> Result<(), ImportRepositoryError> {
    sqlx::query(
        "insert into audit_logs
            (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, $4, 'success', $5,
                 jsonb_build_object('resource_id', $6::text))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(event_type)
    .bind(Uuid::now_v7())
    .bind(resource_id)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

async fn insert_confirmation_audit(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    import_id: Uuid,
    event_type: &'static str,
    outcome: &'static str,
    idempotency_hash_prefix: Option<&str>,
) -> Result<(), ImportRepositoryError> {
    sqlx::query(
        "insert into audit_logs
           (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, $4, $5, $6,
                 jsonb_strip_nulls(jsonb_build_object(
                    'import_id', $7::text, 'idempotency_hash_prefix', $8::text
                 )))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(event_type)
    .bind(outcome)
    .bind(Uuid::now_v7())
    .bind(import_id)
    .bind(idempotency_hash_prefix)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

fn format_error_cursor(
    workspace_id: Uuid,
    import_id: Uuid,
    row_number: i32,
    created_at: OffsetDateTime,
    id: Uuid,
) -> String {
    format!(
        "{workspace_id}:{import_id}:{row_number}:{}:{id}",
        created_at.unix_timestamp_nanos()
    )
}

fn parse_error_cursor(
    cursor: &str,
    expected_workspace_id: Uuid,
    expected_import_id: Uuid,
) -> Result<(i32, OffsetDateTime, Uuid), ImportRepositoryError> {
    let mut parts = cursor.split(':');
    let workspace_id = parts
        .next()
        .and_then(|value| Uuid::parse_str(value).ok())
        .ok_or(ImportRepositoryError::EventIdInvalid)?;
    let import_id = parts
        .next()
        .and_then(|value| Uuid::parse_str(value).ok())
        .ok_or(ImportRepositoryError::EventIdInvalid)?;
    if workspace_id != expected_workspace_id || import_id != expected_import_id {
        return Err(ImportRepositoryError::EventIdInvalid);
    }
    let row_number = parts
        .next()
        .and_then(|value| value.parse().ok())
        .ok_or(ImportRepositoryError::EventIdInvalid)?;
    let nanos = parts
        .next()
        .and_then(|value| value.parse().ok())
        .ok_or(ImportRepositoryError::EventIdInvalid)?;
    let id = parts
        .next()
        .and_then(|value| Uuid::parse_str(value).ok())
        .ok_or(ImportRepositoryError::EventIdInvalid)?;
    if parts.next().is_some() {
        return Err(ImportRepositoryError::EventIdInvalid);
    }
    let created_at = OffsetDateTime::from_unix_timestamp_nanos(nanos)
        .map_err(|_| ImportRepositoryError::EventIdInvalid)?;
    Ok((row_number, created_at, id))
}

async fn insert_error(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    import_id: Uuid,
    item: &ImportErrorPreview,
) -> Result<(), ImportRepositoryError> {
    sqlx::query(
        "insert into import_errors
            (id, workspace_id, import_batch_id, row_number, field_name, severity,
             error_code, raw_value, message, created_by)
         values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(import_id)
    .bind(item.row_number.map(|value| value as i32))
    .bind(&item.field_name)
    .bind(item.severity.as_str())
    .bind(&item.error_code)
    .bind(&item.raw_value)
    .bind(&item.message)
    .bind(actor_user_id)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

fn parse_fields_from_value(
    value: serde_json::Value,
) -> Result<Vec<ImportMappingField>, ImportRepositoryError> {
    let fields = value
        .get("fields")
        .cloned()
        .unwrap_or_else(|| serde_json::Value::Array(Vec::new()));
    serde_json::from_value(fields).map_err(|_| ImportRepositoryError::InvalidTemplateConfiguration)
}

fn json_object_from_cells<F>(row: &ImportPreviewRow, select: F) -> serde_json::Value
where
    F: Fn(&domain::import::ImportPreviewCell) -> serde_json::Value,
{
    let mut object = serde_json::Map::new();
    for cell in &row.cells {
        object.insert(cell.column.clone(), select(cell));
    }
    serde_json::Value::Object(object)
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn template_configuration_round_trips_and_requires_exact_binding() {
        let fields = vec![ImportMappingField {
            source_column: "日期".into(),
            target_field: "record_date".into(),
            transform: Some("date_ymd".into()),
        }];
        let parsed = parse_fields_from_value(json!({ "fields": fields.clone() })).unwrap();
        assert_eq!(parsed, fields);
        assert!(template_binding_matches(
            "generic", "generic", &parsed, &fields
        ));
        assert!(!template_binding_matches(
            "generic", "other", &parsed, &fields
        ));
        assert!(!template_binding_matches(
            "generic",
            "generic",
            &parsed,
            &[ImportMappingField {
                target_field: "code".into(),
                ..fields[0].clone()
            }]
        ));
    }

    #[test]
    fn inspection_parameter_change_invalidates_preview_ready_state() {
        assert!(preview_invalidation_required(
            ImportBatchStatus::PreviewReady,
            true
        ));
        assert!(!preview_invalidation_required(
            ImportBatchStatus::PreviewReady,
            false
        ));
        assert!(!preview_invalidation_required(
            ImportBatchStatus::Mapped,
            true
        ));
    }

    #[test]
    fn ordinary_and_template_mapping_changes_invalidate_preview_ready_state() {
        let fields = vec![ImportMappingField {
            source_column: "date".into(),
            target_field: "trade_date".into(),
            transform: Some("date_ymd".into()),
        }];
        let template_version_id = Uuid::now_v7();
        let ordinary_mapping = ExistingMapping {
            dataset_type: "generic".into(),
            template_version_id: None,
            fields: fields.clone(),
        };

        assert!(preview_invalidation_required(
            ImportBatchStatus::PreviewReady,
            mapping_has_changed(
                Some(&ordinary_mapping),
                "generic",
                None,
                &[ImportMappingField {
                    target_field: "price".into(),
                    ..fields[0].clone()
                }]
            )
        ));
        assert!(preview_invalidation_required(
            ImportBatchStatus::PreviewReady,
            mapping_has_changed(
                Some(&ordinary_mapping),
                "generic",
                Some(template_version_id),
                &fields
            )
        ));
    }

    #[test]
    fn template_version_rebinding_rejects_different_or_empty_second_binding() {
        let first = Uuid::now_v7();
        let second = Uuid::now_v7();
        assert!(template_version_binding_is_allowed(None, Some(first)));
        assert!(template_version_binding_is_allowed(
            Some(first),
            Some(first)
        ));
        assert!(!template_version_binding_is_allowed(
            Some(first),
            Some(second)
        ));
        assert!(!template_version_binding_is_allowed(Some(first), None));
    }

    #[test]
    fn error_cursor_is_bound_to_workspace_and_batch() {
        let workspace_id = Uuid::now_v7();
        let import_id = Uuid::now_v7();
        let created_at = OffsetDateTime::now_utc();
        let item_id = Uuid::now_v7();
        let cursor = format_error_cursor(workspace_id, import_id, 7, created_at, item_id);
        let parsed = parse_error_cursor(&cursor, workspace_id, import_id).unwrap();
        assert_eq!(parsed.0, 7);
        assert_eq!(parsed.2, item_id);
        assert!(
            parse_error_cursor(&cursor, Uuid::now_v7(), import_id).is_err(),
            "cross-workspace cursor must be rejected"
        );
        assert!(
            parse_error_cursor(&cursor, workspace_id, Uuid::now_v7()).is_err(),
            "cross-batch cursor must be rejected"
        );
    }

    #[test]
    fn phase_3c_migration_grants_runtime_full_validation_update() {
        let migration = include_str!(
            "../../../migrations/202607250008_phase_3c_validation_and_imported_records.sql"
        );
        assert!(
            migration.contains(
                "grant select, insert, update, delete on import_staging_rows to futures_runtime;"
            ),
            "validate rewrites every full-file staging row and therefore requires UPDATE"
        );
    }

    #[test]
    fn phase_3c_job_migration_contains_generation_fence() {
        let migration =
            include_str!("../../../migrations/202607250009_phase_3c_job_queue_and_events.sql");
        assert!(migration.contains("lease_generation bigint not null default 0"));
        assert!(
            migration
                .contains("job_queue_lease_generation_nonnegative check (lease_generation >= 0)")
        );
    }

    #[test]
    fn confirm_conflict_policy_binding_matches_text_column() {
        let migration = include_str!(
            "../../../migrations/202607250008_phase_3c_validation_and_imported_records.sql"
        );
        assert!(migration.contains("add column conflict_policy text"));
        assert!(CONFIRM_BATCH_SQL.contains("conflict_policy = $1"));
        assert!(
            !CONFIRM_BATCH_SQL.contains(concat!("::", "import_conflict_policy")),
            "the Phase 3C migration uses text plus CHECK, not a PostgreSQL enum"
        );
    }

    #[test]
    fn confirmation_serializes_workspace_wide_hashed_idempotency_key() {
        assert!(CONFIRM_IDEMPOTENCY_LOCK_SQL.contains("pg_advisory_xact_lock"));
        assert!(CONFIRM_IDEMPOTENCY_LOCK_SQL.contains("$1::text"));
        assert!(CONFIRM_IDEMPOTENCY_LOCK_SQL.contains("$2::text"));
    }
}
