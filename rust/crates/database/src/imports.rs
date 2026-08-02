use application::import_jobs::{StagingRowInput, ValidationOutcome};
use domain::import::{
    ImportBatchStatus, ImportConflictPolicy, ImportErrorPreview, ImportErrorSeverity,
    ImportMappingDefinitionError, ImportMappingField, ImportMappingResponse, ImportPreviewRow,
    ImportRollbackConflict, ImportRollbackConflictType, ImportRollbackRequestStatus,
    ImportTemplateSummary, ImportTemplateVersionResponse, RollbackCapability,
    ensure_status_transition, validate_mapping_fields,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Row};
use std::collections::HashSet;
use time::{Date, OffsetDateTime};
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
    pub automatic: Option<AutomaticImportMetadata>,
}

#[derive(Debug, Clone)]
pub struct AutomaticImportMetadata {
    pub dataset_type: String,
    pub data_source_code: String,
    pub collection_date: Date,
    pub fixed_template_code: String,
}

#[derive(Debug, Clone)]
pub struct AutomaticImportContext {
    pub dataset_type: String,
    pub fixed_template_code: String,
    pub collection_date: Date,
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
    #[error("automatic import metadata is invalid")]
    InvalidAutomaticMetadata,
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
    #[error("rollback is not allowed for this batch")]
    RollbackNotAllowed,
    #[error("direct rollback is not available")]
    RollbackNotAvailable,
    #[error("rollback precheck is stale")]
    RollbackPreconditionStale,
    #[error("rollback precheck has conflicts")]
    RollbackConflict,
    #[error("rollback is already complete")]
    RollbackAlreadyCompleted,
    #[error("rollback is already in progress")]
    RollbackInProgress,
    #[error("rollback idempotency key was reused")]
    RollbackIdempotencyKeyReused,
    #[error("rollback conflict cursor is invalid")]
    RollbackCursorInvalid,
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

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImportConfirmationMode {
    Manual,
    Automatic,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ImportConfirmationScope {
    pub mode: ImportConfirmationMode,
    pub policy: ImportConflictPolicy,
}

impl ImportConfirmationScope {
    pub fn manual(policy: ImportConflictPolicy) -> Self {
        Self {
            mode: ImportConfirmationMode::Manual,
            policy,
        }
    }

    pub fn automatic() -> Self {
        Self {
            mode: ImportConfirmationMode::Automatic,
            policy: ImportConflictPolicy::Skip,
        }
    }
}

fn confirmation_scope_allowed(
    mode: ImportConfirmationMode,
    ingestion_mode: &str,
    dataset_type: &str,
    policy: ImportConflictPolicy,
) -> bool {
    match mode {
        ImportConfirmationMode::Manual => ingestion_mode == "manual" && dataset_type == "generic",
        ImportConfirmationMode::Automatic => {
            ingestion_mode == "automatic"
                && dataset_type != "generic"
                && policy == ImportConflictPolicy::Skip
        }
    }
}

#[derive(Debug, Clone)]
pub struct RollbackPrecheck {
    pub import_id: Uuid,
    pub request_id: Uuid,
    pub fingerprint: String,
    pub rollback_capability: RollbackCapability,
    pub change_log_version: Option<i32>,
    pub affected_count: u32,
    pub conflicts: Vec<ImportRollbackConflict>,
}

impl RollbackPrecheck {
    pub fn can_rollback(&self) -> bool {
        self.conflicts.is_empty() && self.rollback_capability == RollbackCapability::Direct
    }
}

#[derive(Debug, Clone)]
pub struct RollbackConflictPage {
    pub items: Vec<ImportRollbackConflict>,
    pub next_cursor: Option<String>,
}

#[derive(Debug, Clone)]
pub struct QueuedRollback {
    pub request_id: Uuid,
    pub job_id: Uuid,
    pub status: ImportRollbackRequestStatus,
    pub replayed: bool,
}

#[derive(Debug, Clone)]
pub enum QueueRollbackResult {
    Queued(QueuedRollback),
    Conflict(RollbackPrecheck),
}

#[derive(Debug)]
pub(crate) struct EvaluatedRollback {
    pub(crate) import_id: Uuid,
    pub(crate) fingerprint: String,
    pub(crate) rollback_capability: RollbackCapability,
    pub(crate) change_log_version: Option<i32>,
    pub(crate) affected_count: u32,
    pub(crate) conflicts: Vec<ImportRollbackConflict>,
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
    let source_id = if let Some(metadata) = &upload.automatic {
        let definition = automatic_source(&metadata.data_source_code)
            .ok_or(ImportRepositoryError::InvalidAutomaticMetadata)?;
        let source_id = sqlx::query_scalar::<_, Uuid>(
            "insert into data_sources
                    (id, workspace_id, code, name, source_type, base_domain,
                     authorization_status, connector_code, priority)
                 values ($1, $2, $3, $4, $5, $6, $7, 'akshare_v1', $8)
                 on conflict (workspace_id, code) do update
                    set name = excluded.name, base_domain = excluded.base_domain,
                        priority = excluded.priority, updated_at = now()
                  where data_sources.source_type = excluded.source_type
                    and data_sources.authorization_status = excluded.authorization_status
                    and data_sources.connector_code = 'akshare_v1'
                 returning id",
        )
        .bind(Uuid::now_v7())
        .bind(upload.workspace_id)
        .bind(&metadata.data_source_code)
        .bind(definition.name)
        .bind(definition.source_type)
        .bind(definition.base_domain)
        .bind(definition.authorization_status)
        .bind(definition.priority)
        .fetch_one(&mut *tx)
        .await?;
        for allowed_domain in definition.allowed_domains {
            sqlx::query(
                "insert into data_source_allowed_domains
                    (id, workspace_id, data_source_id, domain)
                 values ($1, $2, $3, $4)
                 on conflict (workspace_id, data_source_id, domain) do nothing",
            )
            .bind(Uuid::now_v7())
            .bind(upload.workspace_id)
            .bind(source_id)
            .bind(allowed_domain)
            .execute(&mut *tx)
            .await?;
        }
        Some(source_id)
    } else {
        None
    };
    let dataset_type = upload
        .automatic
        .as_ref()
        .map(|metadata| metadata.dataset_type.as_str())
        .unwrap_or("generic");
    let batch_row = sqlx::query(
        "insert into import_batches
            (id, workspace_id, status, dataset_type, created_by, ingestion_mode,
             data_source_id, collection_date, fixed_template_code)
         values ($1, $2, 'uploaded', $3, $4,
                 case when $5::uuid is null then 'manual' else 'automatic' end,
                 $5, $6, $7)
         returning created_at, updated_at",
    )
    .bind(upload.import_id)
    .bind(upload.workspace_id)
    .bind(dataset_type)
    .bind(upload.actor_user_id)
    .bind(source_id)
    .bind(
        upload
            .automatic
            .as_ref()
            .map(|metadata| metadata.collection_date),
    )
    .bind(
        upload
            .automatic
            .as_ref()
            .map(|metadata| metadata.fixed_template_code.as_str()),
    )
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
    if let (Some(metadata), Some(source_id)) = (&upload.automatic, source_id) {
        sqlx::query(
            "insert into extraction_jobs
                (id, workspace_id, data_source_id, import_batch_id, status, dataset_type,
                 collection_scope_json, output_object_id)
             values ($1, $2, $3, $4, 'uploaded', $5,
                     jsonb_build_object('date', $6::date), $7)",
        )
        .bind(Uuid::now_v7())
        .bind(upload.workspace_id)
        .bind(source_id)
        .bind(upload.import_id)
        .bind(&metadata.dataset_type)
        .bind(metadata.collection_date)
        .bind(upload.object_id)
        .execute(&mut *tx)
        .await?;
    }
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

#[derive(Debug, Clone, Copy)]
struct AutomaticSourceDefinition {
    name: &'static str,
    source_type: &'static str,
    base_domain: &'static str,
    authorization_status: &'static str,
    priority: i32,
    allowed_domains: &'static [&'static str],
}

fn automatic_source(code: &str) -> Option<AutomaticSourceDefinition> {
    match code {
        "akshare_dce_official" => Some(AutomaticSourceDefinition {
            name: "大连商品交易所",
            source_type: "exchange_public",
            base_domain: "www.dce.com.cn",
            authorization_status: "whitelisted",
            priority: 100,
            allowed_domains: &["www.dce.com.cn", "portal.dce.com.cn"],
        }),
        "akshare_sina_dce_fallback" => Some(AutomaticSourceDefinition {
            name: "新浪财经（DCE 聚合）",
            source_type: "aggregator_public",
            base_domain: "vip.stock.finance.sina.com.cn",
            authorization_status: "whitelisted_exception",
            priority: 200,
            allowed_domains: &[
                "vip.stock.finance.sina.com.cn",
                "finance.sina.com.cn",
                "stock2.finance.sina.com.cn",
            ],
        }),
        "akshare_shfe_official" => Some(AutomaticSourceDefinition {
            name: "上海期货交易所",
            source_type: "exchange_public",
            base_domain: "www.shfe.com.cn",
            authorization_status: "whitelisted",
            priority: 100,
            allowed_domains: &["www.shfe.com.cn", "tsite.shfe.com.cn"],
        }),
        "akshare_czce_official" => Some(AutomaticSourceDefinition {
            name: "郑州商品交易所",
            source_type: "exchange_public",
            base_domain: "www.czce.com.cn",
            authorization_status: "whitelisted",
            priority: 100,
            allowed_domains: &["www.czce.com.cn"],
        }),
        "akshare_gfex_official" => Some(AutomaticSourceDefinition {
            name: "广州期货交易所",
            source_type: "exchange_public",
            base_domain: "www.gfex.com.cn",
            authorization_status: "whitelisted",
            priority: 100,
            allowed_domains: &["www.gfex.com.cn"],
        }),
        "akshare_cffex_official" => Some(AutomaticSourceDefinition {
            name: "中国金融期货交易所",
            source_type: "exchange_public",
            base_domain: "www.cffex.com.cn",
            authorization_status: "whitelisted",
            priority: 100,
            allowed_domains: &["www.cffex.com.cn"],
        }),
        _ => None,
    }
}

pub async fn automatic_import_context(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<AutomaticImportContext, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let row = sqlx::query(
        "select ingestion_mode, dataset_type, fixed_template_code, collection_date
           from import_batches
          where workspace_id = $1 and id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    tx.commit().await?;
    if row.get::<String, _>("ingestion_mode") != "automatic" {
        return Err(ImportRepositoryError::InvalidAutomaticMetadata);
    }
    Ok(AutomaticImportContext {
        dataset_type: row.get("dataset_type"),
        fixed_template_code: row
            .get::<Option<String>, _>("fixed_template_code")
            .ok_or(ImportRepositoryError::InvalidAutomaticMetadata)?,
        collection_date: row
            .get::<Option<Date>, _>("collection_date")
            .ok_or(ImportRepositoryError::InvalidAutomaticMetadata)?,
    })
}

pub async fn import_ingestion_mode(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<String, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let mode = sqlx::query_scalar::<_, String>(
        "select ingestion_mode from import_batches where workspace_id = $1 and id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    tx.commit().await?;
    Ok(mode)
}

pub async fn fail_automatic_import(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    import_id: Uuid,
    stable_error_code: &str,
) -> Result<(), ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let affected = sqlx::query(
        "update import_batches
            set status = 'failed', updated_at = now()
          where workspace_id = $1 and id = $2 and ingestion_mode = 'automatic'
            and status in ('uploaded', 'inspected', 'mapped', 'preview_ready')",
    )
    .bind(workspace_id)
    .bind(import_id)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    if affected == 1 {
        sqlx::query(
            "update extraction_jobs
                set status = 'failed', stable_error_code = $1, completed_at = now()
              where workspace_id = $2 and import_batch_id = $3
                and status in ('uploaded', 'queued', 'running')",
        )
        .bind(stable_error_code)
        .bind(workspace_id)
        .bind(import_id)
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "insert into audit_logs
                (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
             values ($1, $2, $3, 'import.automatic_failed', 'failure', $4,
                     jsonb_build_object('import_id', $5::text, 'reason_code', $6::text))",
        )
        .bind(Uuid::now_v7())
        .bind(workspace_id)
        .bind(actor_user_id)
        .bind(Uuid::now_v7())
        .bind(import_id)
        .bind(stable_error_code)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(())
}

pub async fn mark_automatic_queued(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<(), ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    sqlx::query(
        "update extraction_jobs set status = 'queued'
          where workspace_id = $1 and import_batch_id = $2 and status = 'uploaded'",
    )
    .bind(workspace_id)
    .bind(import_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(())
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
    scope: ImportConfirmationScope,
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
        "select status::text as status, ingestion_mode, dataset_type, staging_version,
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
    if !confirmation_scope_allowed(
        scope.mode,
        &batch.get::<String, _>("ingestion_mode"),
        &batch.get::<String, _>("dataset_type"),
        scope.policy,
    ) {
        return Err(ImportRepositoryError::ConflictPolicyNotAllowed);
    }
    if scope.policy == ImportConflictPolicy::Abort
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
        .bind(scope.policy.as_str())
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

pub async fn create_rollback_check(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    import_id: Uuid,
    audit_request_id: Uuid,
) -> Result<RollbackPrecheck, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let evaluated = evaluate_rollback(&mut tx, workspace_id, import_id, "succeeded", None).await?;
    let rollback_request_id = Uuid::now_v7();
    persist_rollback_precheck(
        &mut tx,
        workspace_id,
        actor_user_id,
        rollback_request_id,
        audit_request_id,
        &evaluated,
    )
    .await?;
    tx.commit().await?;
    Ok(RollbackPrecheck {
        import_id,
        request_id: rollback_request_id,
        fingerprint: evaluated.fingerprint,
        rollback_capability: evaluated.rollback_capability,
        change_log_version: evaluated.change_log_version,
        affected_count: evaluated.affected_count,
        conflicts: evaluated.conflicts,
    })
}

#[allow(clippy::too_many_arguments)]
pub async fn queue_rollback(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    import_id: Uuid,
    precheck_request_id: Uuid,
    supplied_fingerprint: &str,
    idempotency_key_hash: &str,
    request_hash: &str,
    audit_request_id: Uuid,
) -> Result<QueueRollbackResult, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    sqlx::query(CONFIRM_IDEMPOTENCY_LOCK_SQL)
        .bind(workspace_id)
        .bind(idempotency_key_hash)
        .execute(&mut *tx)
        .await?;

    if let Some(existing) = sqlx::query(
        "select id, import_batch_id, request_hash, job_id, status
           from import_rollback_requests
          where workspace_id = $1 and idempotency_key_hash = $2",
    )
    .bind(workspace_id)
    .bind(idempotency_key_hash)
    .fetch_optional(&mut *tx)
    .await?
    {
        if existing.get::<Option<String>, _>("request_hash").as_deref() != Some(request_hash)
            || existing.get::<Uuid, _>("import_batch_id") != import_id
        {
            tx.commit().await?;
            return Err(ImportRepositoryError::RollbackIdempotencyKeyReused);
        }
        let status = existing.get::<String, _>("status");
        let parsed_status = ImportRollbackRequestStatus::parse(&status)
            .ok_or(ImportRepositoryError::InvalidStoredStatus)?;
        let job_id = existing
            .get::<Option<Uuid>, _>("job_id")
            .ok_or(ImportRepositoryError::InvalidStoredStatus)?;
        tx.commit().await?;
        return Ok(QueueRollbackResult::Queued(QueuedRollback {
            request_id: existing.get("id"),
            job_id,
            status: parsed_status,
            replayed: true,
        }));
    }

    lock_rollback_batch(&mut tx, workspace_id, import_id).await?;
    let request = sqlx::query(
        "select status, precheck_fingerprint, job_id
           from import_rollback_requests
          where workspace_id = $1 and id = $2 and import_batch_id = $3
          for update",
    )
    .bind(workspace_id)
    .bind(precheck_request_id)
    .bind(import_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    let stored_fingerprint: String = request.get("precheck_fingerprint");
    if stored_fingerprint != supplied_fingerprint {
        return Err(ImportRepositoryError::RollbackPreconditionStale);
    }
    let request_status = request.get::<String, _>("status");
    if request_status != "prechecked" {
        if matches!(request_status.as_str(), "queued" | "running") {
            let job_id = request
                .get::<Option<Uuid>, _>("job_id")
                .ok_or(ImportRepositoryError::InvalidStoredStatus)?;
            tx.commit().await?;
            return Ok(QueueRollbackResult::Queued(QueuedRollback {
                request_id: precheck_request_id,
                job_id,
                status: ImportRollbackRequestStatus::parse(&request_status)
                    .ok_or(ImportRepositoryError::InvalidStoredStatus)?,
                replayed: true,
            }));
        }
        return Err(if request_status == "succeeded" {
            ImportRepositoryError::RollbackAlreadyCompleted
        } else if matches!(
            request_status.as_str(),
            "precheck_conflict" | "worker_conflict"
        ) {
            ImportRepositoryError::RollbackConflict
        } else {
            ImportRepositoryError::RollbackInProgress
        });
    }

    let evaluated = evaluate_rollback(&mut tx, workspace_id, import_id, "succeeded", None).await?;
    if !evaluated.conflicts.is_empty() {
        sqlx::query(
            "update import_rollback_requests
                set status = 'precheck_conflict', precheck_fingerprint = $1,
                    conflict_count = $2, updated_at = now(), finished_at = now()
              where workspace_id = $3 and id = $4 and status = 'prechecked'",
        )
        .bind(&evaluated.fingerprint)
        .bind(evaluated.conflicts.len() as i32)
        .bind(workspace_id)
        .bind(precheck_request_id)
        .execute(&mut *tx)
        .await?;
        insert_rollback_conflicts(
            &mut tx,
            workspace_id,
            import_id,
            precheck_request_id,
            &evaluated.conflicts,
        )
        .await?;
        insert_rollback_audit(
            &mut tx,
            workspace_id,
            actor_user_id,
            audit_request_id,
            import_id,
            precheck_request_id,
            "import.rollback_recheck",
            "failure",
            evaluated.conflicts.len(),
        )
        .await?;
        tx.commit().await?;
        return Ok(QueueRollbackResult::Conflict(RollbackPrecheck {
            import_id,
            request_id: precheck_request_id,
            fingerprint: evaluated.fingerprint,
            rollback_capability: evaluated.rollback_capability,
            change_log_version: evaluated.change_log_version,
            affected_count: evaluated.affected_count,
            conflicts: evaluated.conflicts,
        }));
    }
    if evaluated.fingerprint != supplied_fingerprint {
        return Err(ImportRepositoryError::RollbackPreconditionStale);
    }

    let job_id = Uuid::now_v7();
    sqlx::query(
        "insert into job_queue
           (id, workspace_id, job_type, aggregate_id, status, payload,
            attempt_count, max_attempts, available_at)
         values ($1, $2, 'import_rollback', $3, 'queued',
                 jsonb_build_object(
                    'import_id', $3::text,
                    'rollback_request_id', $4::text,
                    'precheck_fingerprint', $5::text
                 ), 0, 5, now())",
    )
    .bind(job_id)
    .bind(workspace_id)
    .bind(import_id)
    .bind(precheck_request_id)
    .bind(&evaluated.fingerprint)
    .execute(&mut *tx)
    .await?;
    let event_seq = sqlx::query_scalar::<_, i64>(
        "select coalesce(max(event_seq), 0) + 1
           from import_job_events
          where workspace_id = $1 and import_batch_id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_one(&mut *tx)
    .await?;
    sqlx::query(
        "insert into import_job_events
           (id, workspace_id, import_batch_id, job_id, event_seq, event_type, payload)
         values ($1, $2, $3, $4, $5, 'rollback_queued',
                 jsonb_build_object(
                    'status', 'queued',
                    'rollback_request_id', $6::text
                 ))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(import_id)
    .bind(job_id)
    .bind(event_seq)
    .bind(precheck_request_id)
    .execute(&mut *tx)
    .await?;
    let updated = sqlx::query(
        "update import_rollback_requests
            set status = 'queued', idempotency_key_hash = $1, request_hash = $2,
                job_id = $3, conflict_count = 0, finished_at = null, updated_at = now()
          where workspace_id = $4 and id = $5 and import_batch_id = $6
            and status = 'prechecked' and precheck_fingerprint = $7",
    )
    .bind(idempotency_key_hash)
    .bind(request_hash)
    .bind(job_id)
    .bind(workspace_id)
    .bind(precheck_request_id)
    .bind(import_id)
    .bind(&evaluated.fingerprint)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    if updated != 1 {
        return Err(ImportRepositoryError::RollbackPreconditionStale);
    }
    insert_rollback_audit(
        &mut tx,
        workspace_id,
        actor_user_id,
        audit_request_id,
        import_id,
        precheck_request_id,
        "import.rollback_queued",
        "success",
        0,
    )
    .await?;
    tx.commit().await?;
    Ok(QueueRollbackResult::Queued(QueuedRollback {
        request_id: precheck_request_id,
        job_id,
        status: ImportRollbackRequestStatus::Queued,
        replayed: false,
    }))
}

async fn lock_rollback_batch(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
) -> Result<(), ImportRepositoryError> {
    let visible = sqlx::query_scalar::<_, Uuid>(
        "select id from import_batches
          where workspace_id = $1 and id = $2
          for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut **tx)
    .await?;
    if visible.is_none() {
        return Err(ImportRepositoryError::NotFound);
    }
    Ok(())
}

pub async fn list_rollback_conflicts(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
    precheck_request_id: Uuid,
    cursor: Option<&str>,
    limit: u32,
) -> Result<RollbackConflictPage, ImportRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let visible = sqlx::query_scalar::<_, bool>(
        "select exists(
            select 1 from import_rollback_requests
             where workspace_id = $1 and id = $2 and import_batch_id = $3
         )",
    )
    .bind(workspace_id)
    .bind(precheck_request_id)
    .bind(import_id)
    .fetch_one(&mut *tx)
    .await?;
    if !visible {
        return Err(ImportRepositoryError::NotFound);
    }
    let (after_seq, after_id) = cursor
        .map(|value| parse_rollback_cursor(value, workspace_id, import_id, precheck_request_id))
        .transpose()?
        .unwrap_or((0, Uuid::nil()));
    let page_size = limit.clamp(1, 200);
    let rows = sqlx::query(
        "select id, conflict_seq, conflict_type, target_kind, target_id,
                expected_row_version, current_row_version, dependency_kind, detail_code
           from import_rollback_conflicts
          where workspace_id = $1 and rollback_request_id = $2
            and (conflict_seq, id) > ($3, $4)
          order by conflict_seq, id
          limit $5",
    )
    .bind(workspace_id)
    .bind(precheck_request_id)
    .bind(after_seq)
    .bind(after_id)
    .bind(i64::from(page_size) + 1)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    let has_more = rows.len() > page_size as usize;
    let visible_rows = rows
        .into_iter()
        .take(page_size as usize)
        .collect::<Vec<_>>();
    let next_cursor = if has_more {
        visible_rows.last().map(|row| {
            format_rollback_cursor(
                workspace_id,
                import_id,
                precheck_request_id,
                row.get("conflict_seq"),
                row.get("id"),
            )
        })
    } else {
        None
    };
    let items = visible_rows
        .iter()
        .map(rollback_conflict_from_row)
        .collect::<Result<Vec<_>, _>>()?;
    Ok(RollbackConflictPage { items, next_cursor })
}

pub(crate) async fn evaluate_rollback(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
    expected_batch_status: &str,
    excluded_rollback_request_id: Option<Uuid>,
) -> Result<EvaluatedRollback, ImportRepositoryError> {
    let batch = sqlx::query(
        "select status::text, rollback_capability, change_log_version,
                imported_count, overwritten_count
           from import_batches
          where workspace_id = $1 and id = $2
          for update",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(ImportRepositoryError::NotFound)?;
    let batch_status = batch.get::<String, _>("status");
    let capability_value = batch.get::<String, _>("rollback_capability");
    let rollback_capability = RollbackCapability::parse(&capability_value)
        .ok_or(ImportRepositoryError::InvalidStoredStatus)?;
    let change_log_version = batch.get::<Option<i32>, _>("change_log_version");
    let expected_change_count = i64::from(batch.get::<i32, _>("imported_count"))
        + i64::from(batch.get::<i32, _>("overwritten_count"));
    let mut conflicts = Vec::new();
    let mut fingerprint_material = vec![json!({
        "import_id": import_id,
        "rollback_capability": rollback_capability.as_str(),
        "change_log_version": change_log_version,
        "expected_change_count": expected_change_count,
    })];
    let active_rollback_exists = sqlx::query_scalar::<_, bool>(
        "select exists(
            select 1 from import_rollback_requests
             where workspace_id = $1 and import_batch_id = $2
               and status in ('queued', 'running')
               and ($3::uuid is null or id <> $3)
         )",
    )
    .bind(workspace_id)
    .bind(import_id)
    .bind(excluded_rollback_request_id)
    .fetch_one(&mut **tx)
    .await?;
    fingerprint_material.push(json!({
        "active_rollback_exists": active_rollback_exists,
    }));
    let compensation_batch_exists = sqlx::query_scalar::<_, bool>(
        "select exists(
            select 1 from import_compensations compensation
             where compensation.workspace_id = $1
               and compensation.original_import_batch_id = $2
         )",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_one(&mut **tx)
    .await?;
    fingerprint_material.push(json!({
        "compensation_batch_exists": compensation_batch_exists,
    }));

    if batch_status != expected_batch_status {
        push_rollback_conflict(
            &mut conflicts,
            ImportRollbackConflictType::IllegalChange,
            None,
            None,
            None,
            None,
            None,
            "batch_status_not_succeeded",
        );
    }
    if active_rollback_exists {
        push_rollback_conflict(
            &mut conflicts,
            ImportRollbackConflictType::IllegalChange,
            None,
            None,
            None,
            None,
            None,
            "rollback_request_in_progress",
        );
    }
    if compensation_batch_exists {
        push_rollback_conflict(
            &mut conflicts,
            ImportRollbackConflictType::DownstreamDependency,
            None,
            None,
            None,
            None,
            Some("compensation_batch"),
            "compensation_batch_exists",
        );
    }
    if rollback_capability != RollbackCapability::Direct {
        push_rollback_conflict(
            &mut conflicts,
            ImportRollbackConflictType::RollbackNotAvailable,
            None,
            None,
            None,
            None,
            None,
            "rollback_capability_compensation_only",
        );
        return finalize_rollback_evaluation(
            import_id,
            rollback_capability,
            change_log_version,
            0,
            conflicts,
            fingerprint_material,
        );
    }
    if change_log_version != Some(1) {
        push_rollback_conflict(
            &mut conflicts,
            ImportRollbackConflictType::ChangeLogIncomplete,
            None,
            None,
            None,
            None,
            None,
            "unsupported_change_log_version",
        );
    }

    let change_rows = sqlx::query(
        "select change_row.id, change_row.sequence_no, change_row.target_kind,
                change_row.target_id, change_row.operation, change_row.before_json,
                change_row.after_json, change_row.target_row_version,
                change_row.source_file_id, change_row.source_row_number,
                change_row.created_at,
                exists(
                    select 1 from import_files source_file
                     where source_file.workspace_id = change_row.workspace_id
                       and source_file.id = change_row.source_file_id
                       and source_file.import_batch_id = change_row.import_batch_id
                ) as source_valid
           from import_row_changes change_row
          where change_row.workspace_id = $1 and change_row.import_batch_id = $2
          order by change_row.target_kind, change_row.target_id,
                   change_row.sequence_no, change_row.id",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_all(&mut **tx)
    .await?;
    if change_rows.len() as i64 != expected_change_count {
        push_rollback_conflict(
            &mut conflicts,
            ImportRollbackConflictType::ChangeLogIncomplete,
            None,
            None,
            None,
            None,
            None,
            "change_count_mismatch",
        );
    }
    let mut sequence_numbers = change_rows
        .iter()
        .map(|row| row.get::<i64, _>("sequence_no"))
        .collect::<Vec<_>>();
    sequence_numbers.sort_unstable();
    if sequence_numbers
        .iter()
        .enumerate()
        .any(|(index, sequence)| *sequence != index as i64 + 1)
    {
        push_rollback_conflict(
            &mut conflicts,
            ImportRollbackConflictType::ChangeLogIncomplete,
            None,
            None,
            None,
            None,
            None,
            "non_contiguous_change_sequence",
        );
    }

    let mut seen_targets = HashSet::new();
    for change in &change_rows {
        let sequence_no = change.get::<i64, _>("sequence_no");
        let target_kind = change.get::<String, _>("target_kind");
        let target_id = change.get::<Uuid, _>("target_id");
        let operation = change.get::<String, _>("operation");
        let before_json = change.get::<Option<serde_json::Value>, _>("before_json");
        let after_json = change.get::<Option<serde_json::Value>, _>("after_json");
        let expected_row_version = change.get::<i64, _>("target_row_version");
        let source_valid = change.get::<bool, _>("source_valid");
        let change_created_at = change.get::<OffsetDateTime, _>("created_at");
        let target_label = Some(target_kind.as_str());
        let expected_version = u64::try_from(expected_row_version).ok();

        fingerprint_material.push(json!({
            "change_id": change.get::<Uuid, _>("id"),
            "sequence_no": sequence_no,
            "target_kind": target_kind,
            "target_id": target_id,
            "operation": operation,
            "before_json": before_json,
            "after_json": after_json,
            "target_row_version": expected_row_version,
            "source_file_id": change.get::<Uuid, _>("source_file_id"),
            "source_row_number": change.get::<i32, _>("source_row_number"),
            "source_valid": source_valid,
        }));

        if !seen_targets.insert((target_kind.clone(), target_id)) {
            push_rollback_conflict(
                &mut conflicts,
                ImportRollbackConflictType::IllegalChange,
                target_label,
                Some(target_id),
                expected_version,
                None,
                None,
                "duplicate_change_target",
            );
        }
        if !source_valid {
            push_rollback_conflict(
                &mut conflicts,
                ImportRollbackConflictType::SourceChainBroken,
                target_label,
                Some(target_id),
                expected_version,
                None,
                None,
                "source_file_not_linked",
            );
        }
        if target_kind != "imported_record" {
            push_rollback_conflict(
                &mut conflicts,
                ImportRollbackConflictType::IllegalChange,
                None,
                Some(target_id),
                expected_version,
                None,
                None,
                "unsupported_target_kind",
            );
            continue;
        }
        if !matches!(operation.as_str(), "insert" | "update" | "soft_delete") {
            push_rollback_conflict(
                &mut conflicts,
                ImportRollbackConflictType::IllegalChange,
                target_label,
                Some(target_id),
                expected_version,
                None,
                None,
                "unsupported_change_operation",
            );
        }
        let snapshot_valid = match operation.as_str() {
            "insert" => before_json.is_none() && after_json.as_ref().is_some_and(valid_snapshot),
            "update" | "soft_delete" => {
                before_json.as_ref().is_some_and(valid_snapshot)
                    && after_json.as_ref().is_some_and(valid_snapshot)
            }
            _ => false,
        };
        if !snapshot_valid {
            push_rollback_conflict(
                &mut conflicts,
                ImportRollbackConflictType::IllegalChange,
                target_label,
                Some(target_id),
                expected_version,
                None,
                None,
                "invalid_change_snapshot",
            );
        }

        let current = sqlx::query(
            "select record_data, source_import_batch_id, source_row_number, row_version
               from imported_records
              where workspace_id = $1 and id = $2
              for update",
        )
        .bind(workspace_id)
        .bind(target_id)
        .fetch_optional(&mut **tx)
        .await?;
        let Some(current) = current else {
            fingerprint_material.push(json!({
                "target_id": target_id,
                "current": null,
            }));
            push_rollback_conflict(
                &mut conflicts,
                ImportRollbackConflictType::TargetMissing,
                target_label,
                Some(target_id),
                expected_version,
                None,
                None,
                "target_missing",
            );
            continue;
        };
        let current_row_version = current.get::<i64, _>("row_version");
        let current_version = u64::try_from(current_row_version).ok();
        let current_source_batch = current.get::<Uuid, _>("source_import_batch_id");
        let current_snapshot = json!({
            "record_data": current.get::<serde_json::Value, _>("record_data"),
            "source_import_batch_id": current_source_batch,
            "source_row_number": current.get::<i32, _>("source_row_number"),
            "row_version": current_row_version,
        });
        fingerprint_material.push(json!({
            "target_id": target_id,
            "current": current_snapshot,
        }));

        if current_source_batch != import_id {
            push_rollback_conflict(
                &mut conflicts,
                ImportRollbackConflictType::LaterImport,
                target_label,
                Some(target_id),
                expected_version,
                current_version,
                None,
                "source_batch_changed",
            );
        } else if current_row_version != expected_row_version {
            push_rollback_conflict(
                &mut conflicts,
                ImportRollbackConflictType::TargetVersionChanged,
                target_label,
                Some(target_id),
                expected_version,
                current_version,
                None,
                "target_row_version_changed",
            );
            push_rollback_conflict(
                &mut conflicts,
                ImportRollbackConflictType::LaterModification,
                target_label,
                Some(target_id),
                expected_version,
                current_version,
                None,
                "target_modified_after_import",
            );
        } else if after_json.as_ref() != Some(&current_snapshot) {
            push_rollback_conflict(
                &mut conflicts,
                ImportRollbackConflictType::TargetDataChanged,
                target_label,
                Some(target_id),
                expected_version,
                current_version,
                None,
                "after_snapshot_mismatch",
            );
        }

        let later_change_exists = sqlx::query_scalar::<_, bool>(
            "select exists(
                select 1 from import_row_changes later_change
                 where later_change.workspace_id = $1
                   and later_change.target_kind = 'imported_record'
                   and later_change.target_id = $2
                   and later_change.import_batch_id <> $3
                   and later_change.created_at > $4
             )",
        )
        .bind(workspace_id)
        .bind(target_id)
        .bind(import_id)
        .bind(change_created_at)
        .fetch_one(&mut **tx)
        .await?;
        fingerprint_material.push(json!({
            "target_id": target_id,
            "later_change_exists": later_change_exists,
        }));
        if later_change_exists && current_source_batch == import_id {
            push_rollback_conflict(
                &mut conflicts,
                ImportRollbackConflictType::LaterImport,
                target_label,
                Some(target_id),
                expected_version,
                current_version,
                None,
                "later_import_change_log_exists",
            );
        }

        let dependency_exists = sqlx::query_scalar::<_, bool>(
            "select exists(
                select 1 from import_conflict_candidates candidate
                 where candidate.workspace_id = $1
                   and candidate.existing_record_id = $2
                   and candidate.import_batch_id <> $3
             )",
        )
        .bind(workspace_id)
        .bind(target_id)
        .bind(import_id)
        .fetch_one(&mut **tx)
        .await?;
        fingerprint_material.push(json!({
            "target_id": target_id,
            "import_conflict_candidate_dependency": dependency_exists,
        }));
        if dependency_exists {
            push_rollback_conflict(
                &mut conflicts,
                ImportRollbackConflictType::DownstreamDependency,
                target_label,
                Some(target_id),
                expected_version,
                current_version,
                Some("import_conflict_candidate"),
                "downstream_import_conflict_candidate",
            );
        }
    }

    finalize_rollback_evaluation(
        import_id,
        rollback_capability,
        change_log_version,
        change_rows.len() as u32,
        conflicts,
        fingerprint_material,
    )
}

fn valid_snapshot(value: &serde_json::Value) -> bool {
    let Some(object) = value.as_object() else {
        return false;
    };
    object.len() == 4
        && object.contains_key("record_data")
        && object
            .get("source_import_batch_id")
            .and_then(serde_json::Value::as_str)
            .is_some_and(|value| Uuid::parse_str(value).is_ok())
        && object
            .get("source_row_number")
            .and_then(serde_json::Value::as_i64)
            .and_then(|value| i32::try_from(value).ok())
            .is_some_and(|value| value > 0)
        && object
            .get("row_version")
            .and_then(serde_json::Value::as_i64)
            .is_some_and(|value| value > 0)
}

fn finalize_rollback_evaluation(
    import_id: Uuid,
    rollback_capability: RollbackCapability,
    change_log_version: Option<i32>,
    affected_count: u32,
    conflicts: Vec<ImportRollbackConflict>,
    fingerprint_material: Vec<serde_json::Value>,
) -> Result<EvaluatedRollback, ImportRepositoryError> {
    let fingerprint = format!(
        "{:x}",
        Sha256::digest(
            serde_json::to_vec(&fingerprint_material)
                .map_err(|_| ImportRepositoryError::InvalidStoredStatus)?
        )
    );
    Ok(EvaluatedRollback {
        import_id,
        fingerprint,
        rollback_capability,
        change_log_version,
        affected_count,
        conflicts,
    })
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn push_rollback_conflict(
    conflicts: &mut Vec<ImportRollbackConflict>,
    conflict_type: ImportRollbackConflictType,
    target_kind: Option<&str>,
    target_id: Option<Uuid>,
    expected_row_version: Option<u64>,
    current_row_version: Option<u64>,
    dependency_kind: Option<&str>,
    detail_code: &str,
) {
    conflicts.push(ImportRollbackConflict {
        conflict_seq: conflicts.len() as u64 + 1,
        conflict_type,
        target_kind: target_kind.map(str::to_string),
        target_id,
        expected_row_version,
        current_row_version,
        dependency_kind: dependency_kind.map(str::to_string),
        detail_code: detail_code.to_string(),
    });
}

async fn persist_rollback_precheck(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    rollback_request_id: Uuid,
    audit_request_id: Uuid,
    evaluated: &EvaluatedRollback,
) -> Result<(), ImportRepositoryError> {
    let status = if evaluated.conflicts.is_empty()
        && evaluated.rollback_capability == RollbackCapability::Direct
    {
        "prechecked"
    } else {
        "precheck_conflict"
    };
    sqlx::query(
        "insert into import_rollback_requests
           (id, workspace_id, import_batch_id, requested_by, precheck_fingerprint,
            status, conflict_count, finished_at)
         values ($1, $2, $3, $4, $5, $6, $7, now())",
    )
    .bind(rollback_request_id)
    .bind(workspace_id)
    .bind(evaluated.import_id)
    .bind(actor_user_id)
    .bind(&evaluated.fingerprint)
    .bind(status)
    .bind(evaluated.conflicts.len() as i32)
    .execute(&mut **tx)
    .await?;
    insert_rollback_conflicts(
        tx,
        workspace_id,
        evaluated.import_id,
        rollback_request_id,
        &evaluated.conflicts,
    )
    .await?;
    insert_rollback_audit(
        tx,
        workspace_id,
        actor_user_id,
        audit_request_id,
        evaluated.import_id,
        rollback_request_id,
        "import.rollback_check",
        if evaluated.conflicts.is_empty() {
            "success"
        } else {
            "failure"
        },
        evaluated.conflicts.len(),
    )
    .await
}

pub(crate) async fn insert_rollback_conflicts(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
    rollback_request_id: Uuid,
    conflicts: &[ImportRollbackConflict],
) -> Result<(), ImportRepositoryError> {
    for conflict in conflicts {
        sqlx::query(
            "insert into import_rollback_conflicts
               (id, workspace_id, rollback_request_id, import_batch_id, conflict_seq,
                conflict_type, target_kind, target_id, expected_row_version,
                current_row_version, dependency_kind, detail_code)
             values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
        )
        .bind(Uuid::now_v7())
        .bind(workspace_id)
        .bind(rollback_request_id)
        .bind(import_id)
        .bind(conflict.conflict_seq as i64)
        .bind(conflict.conflict_type.as_str())
        .bind(&conflict.target_kind)
        .bind(conflict.target_id)
        .bind(conflict.expected_row_version.map(|value| value as i64))
        .bind(conflict.current_row_version.map(|value| value as i64))
        .bind(&conflict.dependency_kind)
        .bind(&conflict.detail_code)
        .execute(&mut **tx)
        .await?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn insert_rollback_audit(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    audit_request_id: Uuid,
    import_id: Uuid,
    rollback_request_id: Uuid,
    event_type: &'static str,
    outcome: &'static str,
    conflict_count: usize,
) -> Result<(), ImportRepositoryError> {
    sqlx::query(
        "insert into audit_logs
           (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, $4, $5, $6,
                 jsonb_build_object(
                    'import_id', $7::text,
                    'rollback_request_id', $8::text,
                    'conflict_count', $9::integer
                 ))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(event_type)
    .bind(outcome)
    .bind(audit_request_id)
    .bind(import_id)
    .bind(rollback_request_id)
    .bind(conflict_count as i32)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

fn format_rollback_cursor(
    workspace_id: Uuid,
    import_id: Uuid,
    rollback_request_id: Uuid,
    conflict_seq: i64,
    conflict_id: Uuid,
) -> String {
    format!("{workspace_id}:{import_id}:{rollback_request_id}:{conflict_seq}:{conflict_id}")
}

fn parse_rollback_cursor(
    cursor: &str,
    expected_workspace_id: Uuid,
    expected_import_id: Uuid,
    expected_rollback_request_id: Uuid,
) -> Result<(i64, Uuid), ImportRepositoryError> {
    let mut parts = cursor.split(':');
    let workspace_id = parts.next().and_then(|value| Uuid::parse_str(value).ok());
    let import_id = parts.next().and_then(|value| Uuid::parse_str(value).ok());
    let rollback_request_id = parts.next().and_then(|value| Uuid::parse_str(value).ok());
    let conflict_seq = parts.next().and_then(|value| value.parse::<i64>().ok());
    let conflict_id = parts.next().and_then(|value| Uuid::parse_str(value).ok());
    if workspace_id != Some(expected_workspace_id)
        || import_id != Some(expected_import_id)
        || rollback_request_id != Some(expected_rollback_request_id)
        || conflict_seq.is_none_or(|value| value < 1)
        || conflict_id.is_none()
        || parts.next().is_some()
    {
        return Err(ImportRepositoryError::RollbackCursorInvalid);
    }
    Ok((
        conflict_seq.expect("validated conflict sequence"),
        conflict_id.expect("validated conflict id"),
    ))
}

fn rollback_conflict_from_row(
    row: &sqlx::postgres::PgRow,
) -> Result<ImportRollbackConflict, ImportRepositoryError> {
    let conflict_type =
        ImportRollbackConflictType::parse(row.get::<String, _>("conflict_type").as_str())
            .ok_or(ImportRepositoryError::InvalidStoredStatus)?;
    Ok(ImportRollbackConflict {
        conflict_seq: row.get::<i64, _>("conflict_seq") as u64,
        conflict_type,
        target_kind: row.get("target_kind"),
        target_id: row.get("target_id"),
        expected_row_version: row
            .get::<Option<i64>, _>("expected_row_version")
            .and_then(|value| u64::try_from(value).ok()),
        current_row_version: row
            .get::<Option<i64>, _>("current_row_version")
            .and_then(|value| u64::try_from(value).ok()),
        dependency_kind: row.get("dependency_kind"),
        detail_code: row.get("detail_code"),
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
    fn dce_fallback_is_the_only_aggregator_in_the_automatic_source_allowlist() {
        let fallback = automatic_source("akshare_sina_dce_fallback").unwrap();
        assert_eq!(fallback.source_type, "aggregator_public");
        assert_eq!(fallback.authorization_status, "whitelisted_exception");
        assert_eq!(fallback.priority, 200);
        assert_eq!(
            fallback.allowed_domains,
            [
                "vip.stock.finance.sina.com.cn",
                "finance.sina.com.cn",
                "stock2.finance.sina.com.cn",
            ]
        );
        assert!(automatic_source("akshare_sina_shfe_fallback").is_none());

        let migration = include_str!("../../../migrations/202608020002_dce_fallback_source.sql");
        assert!(migration.contains("source_type = 'aggregator_public'"));
        assert!(migration.contains("authorization_status = 'whitelisted_exception'"));
        assert!(migration.contains("values ('202608020002'"));

        let deployment = include_str!("../../../../.github/workflows/deploy-futures.yml");
        assert_eq!(
            deployment
                .matches("202608020002_dce_fallback_source.sql")
                .count(),
            2
        );
        assert!(
            deployment.contains("migrations=202607260001,202607260002,202608020001,202608020002")
        );
        assert_eq!(deployment.matches("ServerAliveInterval=30").count(), 1);
        assert_eq!(deployment.matches("ServerAliveCountMax=6").count(), 1);
    }

    #[test]
    fn confirmation_mode_keeps_manual_and_automatic_scopes_disjoint() {
        assert!(confirmation_scope_allowed(
            ImportConfirmationMode::Manual,
            "manual",
            "generic",
            ImportConflictPolicy::Skip,
        ));
        assert!(!confirmation_scope_allowed(
            ImportConfirmationMode::Manual,
            "automatic",
            "market_prices",
            ImportConflictPolicy::Skip,
        ));
        assert!(confirmation_scope_allowed(
            ImportConfirmationMode::Automatic,
            "automatic",
            "market_prices",
            ImportConflictPolicy::Skip,
        ));
        assert!(!confirmation_scope_allowed(
            ImportConfirmationMode::Automatic,
            "automatic",
            "generic",
            ImportConflictPolicy::Skip,
        ));
        assert!(!confirmation_scope_allowed(
            ImportConfirmationMode::Automatic,
            "manual",
            "market_prices",
            ImportConflictPolicy::Skip,
        ));
        assert!(!confirmation_scope_allowed(
            ImportConfirmationMode::Automatic,
            "automatic",
            "market_prices",
            ImportConflictPolicy::Overwrite,
        ));
    }

    #[test]
    fn rollback_snapshot_validation_accepts_controlled_soft_delete_snapshots() {
        let snapshot = json!({
            "record_data": {"value": "before"},
            "source_import_batch_id": Uuid::now_v7(),
            "source_row_number": 1,
            "row_version": 2,
        });
        assert!(valid_snapshot(&snapshot));
        assert!(!valid_snapshot(&json!({
            "record_data": {},
            "source_import_batch_id": Uuid::now_v7(),
            "source_row_number": 1,
            "row_version": 2,
            "uncontrolled": true,
        })));
    }

    const IMPORTS_SOURCE: &str = include_str!("imports.rs");

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

    #[test]
    fn rollback_conflict_cursor_is_bound_to_workspace_batch_and_precheck() {
        let workspace_id = Uuid::now_v7();
        let import_id = Uuid::now_v7();
        let request_id = Uuid::now_v7();
        let conflict_id = Uuid::now_v7();
        let cursor = format_rollback_cursor(workspace_id, import_id, request_id, 7, conflict_id);
        assert_eq!(
            parse_rollback_cursor(&cursor, workspace_id, import_id, request_id).unwrap(),
            (7, conflict_id)
        );
        assert!(parse_rollback_cursor(&cursor, Uuid::now_v7(), import_id, request_id).is_err());
        assert!(parse_rollback_cursor(&cursor, workspace_id, Uuid::now_v7(), request_id).is_err());
        assert!(parse_rollback_cursor(&cursor, workspace_id, import_id, Uuid::now_v7()).is_err());
        assert!(
            parse_rollback_cursor(
                &format!("{workspace_id}:{import_id}:{request_id}:0:{conflict_id}"),
                workspace_id,
                import_id,
                request_id
            )
            .is_err()
        );
    }

    #[test]
    fn rollback_precheck_fingerprint_is_deterministic_and_state_sensitive() {
        let import_id = Uuid::now_v7();
        let material = vec![json!({
            "import_id": import_id,
            "target_id": Uuid::nil(),
            "row_version": 3,
        })];
        let first = finalize_rollback_evaluation(
            import_id,
            RollbackCapability::Direct,
            Some(1),
            1,
            Vec::new(),
            material.clone(),
        )
        .unwrap();
        let second = finalize_rollback_evaluation(
            import_id,
            RollbackCapability::Direct,
            Some(1),
            1,
            Vec::new(),
            material,
        )
        .unwrap();
        let changed = finalize_rollback_evaluation(
            import_id,
            RollbackCapability::Direct,
            Some(1),
            1,
            Vec::new(),
            vec![json!({
                "import_id": import_id,
                "target_id": Uuid::nil(),
                "row_version": 4,
            })],
        )
        .unwrap();
        assert_eq!(first.fingerprint, second.fingerprint);
        assert_ne!(first.fingerprint, changed.fingerprint);
        assert_eq!(first.fingerprint.len(), 64);
    }

    #[test]
    fn rollback_enqueue_rechecks_before_creating_any_job() {
        let queue_body = IMPORTS_SOURCE
            .split("pub async fn queue_rollback")
            .nth(1)
            .expect("queue function")
            .split("pub async fn list_rollback_conflicts")
            .next()
            .expect("queue function end");
        let recheck = queue_body
            .find("evaluate_rollback(&mut tx")
            .expect("synchronous recheck");
        let conflict_return = queue_body
            .find("QueueRollbackResult::Conflict")
            .expect("conflict return");
        let job_insert = queue_body
            .find("insert into job_queue")
            .expect("rollback job insert");
        assert!(recheck < conflict_return);
        assert!(conflict_return < job_insert);
        assert_eq!(queue_body.matches("pool.begin().await?").count(), 1);
        assert_eq!(queue_body.matches("insert into job_queue").count(), 1);
    }

    #[test]
    fn rollback_mutation_lock_order_is_advisory_batch_request_then_targets() {
        let queue_body = IMPORTS_SOURCE
            .split("pub async fn queue_rollback")
            .nth(1)
            .expect("queue function")
            .split("async fn lock_rollback_batch")
            .next()
            .expect("queue function end");
        let advisory = queue_body
            .find("CONFIRM_IDEMPOTENCY_LOCK_SQL")
            .expect("workspace advisory lock");
        let batch = queue_body
            .find("lock_rollback_batch(&mut tx")
            .expect("batch lock");
        let request = queue_body
            .find("select status, precheck_fingerprint, job_id")
            .expect("rollback request lock");
        let targets = queue_body
            .find("evaluate_rollback(&mut tx")
            .expect("target precheck locks");
        assert!(advisory < batch);
        assert!(batch < request);
        assert!(request < targets);

        let batch_lock = IMPORTS_SOURCE
            .split("async fn lock_rollback_batch")
            .nth(1)
            .expect("batch lock helper")
            .split("pub async fn list_rollback_conflicts")
            .next()
            .expect("batch lock helper end");
        assert!(batch_lock.contains("from import_batches"));
        assert!(batch_lock.contains("for update"));
    }

    #[test]
    fn rollback_precheck_is_deny_by_default_for_unknown_targets_and_dependencies() {
        let evaluation = IMPORTS_SOURCE
            .split("async fn evaluate_rollback")
            .nth(1)
            .expect("evaluation")
            .split("fn finalize_rollback_evaluation")
            .next()
            .expect("evaluation end");
        assert!(evaluation.contains("unsupported_target_kind"));
        assert!(evaluation.contains("unsupported_change_operation"));
        assert!(evaluation.contains("import_conflict_candidates candidate"));
        assert!(evaluation.contains("downstream_import_conflict_candidate"));
        assert!(evaluation.contains("from import_compensations compensation"));
        assert!(evaluation.contains("compensation_batch_exists"));
        assert!(evaluation.contains("Some(\"compensation_batch\")"));
        assert!(evaluation.contains("for update"));
    }

    #[test]
    fn rollback_checks_compensation_dependency_after_locking_original_batch() {
        let evaluation = IMPORTS_SOURCE
            .split("async fn evaluate_rollback")
            .nth(1)
            .expect("evaluation")
            .split("fn finalize_rollback_evaluation")
            .next()
            .expect("evaluation end");
        let original_lock = evaluation
            .find("from import_batches")
            .expect("original batch query");
        let for_update = evaluation[original_lock..]
            .find("for update")
            .map(|offset| original_lock + offset)
            .expect("original batch lock");
        let compensation_dependency = evaluation
            .find("from import_compensations compensation")
            .expect("compensation dependency query");
        assert!(for_update < compensation_dependency);
        assert!(evaluation.contains("ImportRollbackConflictType::DownstreamDependency"));
        assert!(evaluation.contains("\"compensation_batch_exists\""));
    }

    #[test]
    fn phase_3d_migration_allows_atomic_recheck_conflict_transition() {
        let migration = include_str!(
            "../../../migrations/202607260001_phase_3d_rollback_and_object_governance.sql"
        );
        assert!(migration.contains("('prechecked', 'precheck_conflict')"));
        assert!(migration.contains("import_rollback_conflicts_immutable"));
        assert!(migration.contains("force row level security"));
    }
}
