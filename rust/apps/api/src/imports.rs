use crate::auth::{self, AuthError, AuthState, Permission};
use application::imports::{
    ImportParseError, UploadValidationError, UploadValidator, ValidatedUpload,
};
use axum::{
    Json,
    extract::{Multipart, Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{
        IntoResponse, Response, Sse,
        sse::{Event, KeepAlive},
    },
};
use common::ApiResponse;
use database::compensations::{
    CompensationRepositoryError, CompensationUploadPreparation, NewCompensationUpload,
};
use database::imports::{
    AutomaticImportMetadata, ImportRepositoryError, ImportUploadRecord, InspectionUpdate,
    NewImportUpload, PreviewInputSnapshot, PreviewSave, QueueRollbackResult, RollbackPrecheck,
};
use domain::import::{
    ImportBatchStatus, ImportCompensationResponse, ImportConfirmRequest, ImportConflictPolicy,
    ImportDatasetDefinition, ImportErrorsResponse, ImportInspectRequest, ImportInspectResponse,
    ImportLineageResponse, ImportMappingField, ImportMappingRequest, ImportMappingResponse,
    ImportPreviewRequest, ImportRollbackCheckResponse, ImportRollbackConflictsResponse,
    ImportRollbackRequest, ImportRollbackResponse, ImportTemplateCreateRequest,
    ImportTemplateSummary, ImportTemplateVersionResponse, RollbackCapability,
    import_dataset_definitions,
};
use domain::object_governance::{
    ObjectConsistencyReport, ObjectConsistencyRun, ObjectQuarantineResponse,
};
use infrastructure::object_storage::{ObjectStorage, ObjectStorageError, ObjectUpload};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{collections::VecDeque, convert::Infallible, sync::Arc, time::Duration};
use time::{Date, OffsetDateTime, macros::format_description};
use utoipa::ToSchema;
use uuid::Uuid;

#[derive(Clone)]
pub struct ImportState {
    pub auth: Arc<AuthState>,
    pub storage: Arc<infrastructure::object_storage::LocalObjectStorage>,
    pub upload_policy: application::imports::UploadPolicy,
    pub idempotency_pepper: String,
    pub sse_revalidate_seconds: u64,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct ImportErrorBody {
    code: &'static str,
    message: &'static str,
}

#[derive(Debug, Serialize, ToSchema)]
#[serde(rename_all = "snake_case")]
#[allow(dead_code)]
pub enum ImportEventStreamErrorCode {
    AuthRequired,
    PermissionDenied,
    EventIdInvalid,
    EventNotVisible,
    InternalError,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct ImportEventStreamErrorBody {
    pub code: ImportEventStreamErrorCode,
    pub message: String,
}

macro_rules! import_sse_frame {
    ($frame:ident, $kind:ident, $variant:ident) => {
        #[derive(Debug, Serialize, ToSchema)]
        #[serde(rename_all = "snake_case")]
        #[allow(dead_code)]
        pub enum $kind {
            $variant,
        }

        #[derive(Debug, Serialize, ToSchema)]
        pub struct $frame {
            pub event_seq: i64,
            pub event_type: $kind,
            pub status: String,
            pub processed_rows: i64,
            pub total_rows: i64,
            pub inserted_count: i64,
            pub updated_count: i64,
            pub skipped_count: i64,
            pub conflict_count: i64,
            pub error_code: Option<String>,
        }
    };
}

import_sse_frame!(ImportQueuedEventFrame, ImportQueuedEventType, Queued);
import_sse_frame!(ImportRunningEventFrame, ImportRunningEventType, Running);
import_sse_frame!(ImportProgressEventFrame, ImportProgressEventType, Progress);
import_sse_frame!(
    ImportSucceededEventFrame,
    ImportSucceededEventType,
    Succeeded
);
import_sse_frame!(ImportFailedEventFrame, ImportFailedEventType, Failed);
import_sse_frame!(
    ImportDeadLetterEventFrame,
    ImportDeadLetterEventType,
    DeadLetter
);
import_sse_frame!(
    ImportRollbackQueuedEventFrame,
    ImportRollbackQueuedEventType,
    RollbackQueued
);
import_sse_frame!(
    ImportRollbackRunningEventFrame,
    ImportRollbackRunningEventType,
    RollbackRunning
);
import_sse_frame!(
    ImportRollbackConflictEventFrame,
    ImportRollbackConflictEventType,
    RollbackConflict
);
import_sse_frame!(
    ImportRolledBackEventFrame,
    ImportRolledBackEventType,
    RolledBack
);
import_sse_frame!(
    ImportRollbackFailedEventFrame,
    ImportRollbackFailedEventType,
    RollbackFailed
);

#[derive(Debug, Serialize, ToSchema)]
#[serde(untagged)]
#[schema(discriminator = "event_type")]
#[allow(dead_code)]
pub enum ImportSseEventFrame {
    Queued(ImportQueuedEventFrame),
    Running(ImportRunningEventFrame),
    Progress(ImportProgressEventFrame),
    Succeeded(ImportSucceededEventFrame),
    Failed(ImportFailedEventFrame),
    DeadLetter(ImportDeadLetterEventFrame),
    RollbackQueued(ImportRollbackQueuedEventFrame),
    RollbackRunning(ImportRollbackRunningEventFrame),
    RollbackConflict(ImportRollbackConflictEventFrame),
    RolledBack(ImportRolledBackEventFrame),
    RollbackFailed(ImportRollbackFailedEventFrame),
}

#[derive(Debug, Serialize, ToSchema)]
#[serde(untagged)]
#[allow(dead_code)]
pub enum ImportRollbackConflictApiResponse {
    Precheck(ImportRollbackCheckResponse),
    Error(ImportErrorBody),
}

#[derive(Debug, ToSchema)]
#[allow(dead_code)]
pub struct ImportUploadRequest {
    #[schema(value_type = String, format = Binary)]
    pub file: Vec<u8>,
}

#[derive(Debug, ToSchema)]
#[allow(dead_code)]
pub struct ImportCompensationUploadRequest {
    #[schema(value_type = String, format = Binary)]
    pub file: Vec<u8>,
    pub reason: String,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct ImportFileResponse {
    pub id: Uuid,
    pub original_filename: String,
    pub declared_mime_type: String,
    pub detected_format: String,
    pub sha256: String,
    pub size_bytes: i64,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct ImportResponse {
    pub id: Uuid,
    pub status: ImportBatchStatus,
    pub file: ImportFileResponse,
    #[serde(with = "time::serde::rfc3339")]
    #[schema(value_type = String)]
    pub created_at: OffsetDateTime,
    #[serde(with = "time::serde::rfc3339")]
    #[schema(value_type = String)]
    pub updated_at: OffsetDateTime,
    pub validation: Option<ImportValidateApiResponse>,
    pub job: Option<ImportJobApiResponse>,
    pub conflict_policy: Option<ImportConflictPolicy>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct ImportTemplatesResponse {
    pub items: Vec<ImportTemplateSummary>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct ImportDatasetsResponse {
    pub items: Vec<ImportDatasetDefinition>,
}

impl From<ImportUploadRecord> for ImportResponse {
    fn from(record: ImportUploadRecord) -> Self {
        let validation =
            record
                .validation_version
                .map(|validation_version| ImportValidateApiResponse {
                    import_id: record.import_id,
                    validation_version: validation_version.to_string(),
                    blocking_error_count: record.blocking_error_count as u32,
                    warning_count: record.warning_count as u32,
                    duplicate_count: record.duplicate_count as u32,
                    conflict_count: record.conflict_count as u32,
                    allowed_conflict_policies: ImportConflictPolicy::ALL.to_vec(),
                });
        let job = record.job_id.map(|job_id| ImportJobApiResponse {
            job_id,
            status: record.job_status.unwrap_or_else(|| "queued".into()),
            processed_rows: record.processed_count as u32,
            total_rows: record.total_rows as u32,
            inserted_count: record.imported_count as u32,
            updated_count: record.overwritten_count as u32,
            skipped_count: record.skipped_count as u32,
            conflict_count: record.conflict_result_count as u32,
            error_code: record.job_error_code,
            attempt_count: record.job_attempt_count.unwrap_or(0) as u32,
            max_attempts: record.job_max_attempts.unwrap_or(5) as u32,
        });
        let conflict_policy = record
            .conflict_policy
            .as_deref()
            .and_then(ImportConflictPolicy::parse);
        Self {
            id: record.import_id,
            status: record.status,
            file: ImportFileResponse {
                id: record.file_id,
                original_filename: record.original_filename,
                declared_mime_type: record.declared_mime_type,
                detected_format: record.detected_format,
                sha256: record.sha256,
                size_bytes: record.size_bytes,
            },
            created_at: record.created_at,
            updated_at: record.updated_at,
            validation,
            job,
            conflict_policy,
        }
    }
}

#[derive(Debug)]
pub struct ImportApiError {
    status: StatusCode,
    code: &'static str,
    message: &'static str,
    request_id: Uuid,
}

impl ImportApiError {
    fn auth(error: AuthError, request_id: Uuid) -> Self {
        Self {
            status: error.status(),
            code: error.code(),
            message: error.message(),
            request_id,
        }
    }

    fn bad_request(code: &'static str, request_id: Uuid) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            code,
            message: "request is invalid",
            request_id,
        }
    }

    fn forbidden(code: &'static str, request_id: Uuid) -> Self {
        Self {
            status: StatusCode::FORBIDDEN,
            code,
            message: "request is forbidden",
            request_id,
        }
    }

    fn unsupported(code: &'static str, request_id: Uuid) -> Self {
        Self {
            status: StatusCode::UNSUPPORTED_MEDIA_TYPE,
            code,
            message: "file type is not supported",
            request_id,
        }
    }

    fn too_large(request_id: Uuid) -> Self {
        Self {
            status: StatusCode::PAYLOAD_TOO_LARGE,
            code: "file_too_large",
            message: "file exceeds upload limit",
            request_id,
        }
    }

    fn not_found(request_id: Uuid) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            code: "import_not_found",
            message: "import is not visible",
            request_id,
        }
    }

    fn not_found_with_code(code: &'static str, request_id: Uuid) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            code,
            message: "resource is not visible",
            request_id,
        }
    }

    fn internal(request_id: Uuid) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "internal_error",
            message: "internal error",
            request_id,
        }
    }

    fn object_consistency_not_found(request_id: Uuid) -> Self {
        Self {
            status: StatusCode::NOT_FOUND,
            code: "object_consistency_not_found",
            message: "object consistency resource is not visible",
            request_id,
        }
    }

    fn object_consistency_error(request_id: Uuid) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "object_consistency_error",
            message: "object consistency operation failed",
            request_id,
        }
    }

    fn conflict(code: &'static str, request_id: Uuid) -> Self {
        Self {
            status: StatusCode::CONFLICT,
            code,
            message: "request conflicts with current state",
            request_id,
        }
    }
}

#[derive(Debug, Deserialize, utoipa::IntoParams)]
pub struct ImportErrorsQuery {
    pub cursor: Option<String>,
    pub limit: Option<u32>,
}

#[derive(Debug, Deserialize, utoipa::IntoParams)]
pub struct ImportRollbackConflictsQuery {
    pub precheck_request_id: Uuid,
    pub cursor: Option<String>,
    pub limit: Option<u32>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct ImportValidateApiResponse {
    pub import_id: Uuid,
    pub validation_version: String,
    pub blocking_error_count: u32,
    pub warning_count: u32,
    pub duplicate_count: u32,
    pub conflict_count: u32,
    pub allowed_conflict_policies: Vec<ImportConflictPolicy>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct ImportConfirmApiResponse {
    pub import_id: Uuid,
    pub job_id: Uuid,
    pub status: String,
    pub conflict_policy: ImportConflictPolicy,
    pub replayed: bool,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct ImportJobApiResponse {
    pub job_id: Uuid,
    pub status: String,
    pub processed_rows: u32,
    pub total_rows: u32,
    pub inserted_count: u32,
    pub updated_count: u32,
    pub skipped_count: u32,
    pub conflict_count: u32,
    pub error_code: Option<String>,
    pub attempt_count: u32,
    pub max_attempts: u32,
}

impl IntoResponse for ImportApiError {
    fn into_response(self) -> Response {
        (
            self.status,
            Json(ApiResponse::new(
                ImportErrorBody {
                    code: self.code,
                    message: self.message,
                },
                self.request_id,
            )),
        )
            .into_response()
    }
}

#[utoipa::path(
    post,
    path = "/api/v1/imports",
    params(
        ("x-csrf-token" = String, Header, description = "Session-bound CSRF token"),
        ("Origin" = String, Header, description = "Expected public origin")
    ),
    request_body(
        content = ImportUploadRequest,
        content_type = "multipart/form-data",
        description = "One file field. TXT, CSV, XLS and XLSX are accepted up to IMPORT_MAX_BYTES."
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 201, body = ImportResponse),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 413, body = ImportErrorBody),
        (status = 415, body = ImportErrorBody)
    )
)]
pub async fn upload(
    State(state): State<Arc<ImportState>>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> Result<Response, ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, &headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    if let Err(error) = auth::ensure_allowed_origin(&state.auth.config, &headers) {
        return Err(audit_upload_denial(&state, &context, request_id, error).await);
    }
    if let Err(error) = auth::ensure_csrf(&state.auth, &headers).await {
        return Err(audit_upload_denial(&state, &context, request_id, error).await);
    }
    if let Err(error) = context.require_permission(Permission::Upload) {
        return Err(audit_upload_denial(&state, &context, request_id, error).await);
    }
    let automatic = automatic_metadata(&headers, request_id)?;
    if automatic.is_some() && !context.is_collector_account() {
        return Err(ImportApiError::forbidden(
            "automatic_account_required",
            request_id,
        ));
    }
    let mut field = multipart
        .next_field()
        .await
        .map_err(|_| ImportApiError::bad_request("invalid_multipart", request_id))?
        .ok_or_else(|| ImportApiError::bad_request("single_file_required", request_id))?;
    if field.name() != Some("file") {
        return Err(ImportApiError::bad_request(
            "single_file_required",
            request_id,
        ));
    }
    let filename = field
        .file_name()
        .map(str::to_string)
        .ok_or_else(|| ImportApiError::bad_request("filename_required", request_id))?;
    let declared_mime = field.content_type().map(str::to_string);
    let mut validator = UploadValidator::new(
        state.upload_policy.clone(),
        &filename,
        declared_mime.as_deref(),
    )
    .map_err(|error| map_validation_error(error, request_id))?;
    let mut object_upload = state
        .storage
        .begin_upload(context.workspace_id())
        .await
        .map_err(|error| map_storage_error(error, request_id))?;

    loop {
        let next_chunk = match field.chunk().await {
            Ok(chunk) => chunk,
            Err(_) => {
                let _ = object_upload.abort().await;
                return Err(ImportApiError::bad_request(
                    "upload_interrupted",
                    request_id,
                ));
            }
        };
        let Some(chunk) = next_chunk else {
            break;
        };
        if let Err(error) = validator.observe(&chunk) {
            let _ = object_upload.abort().await;
            return Err(map_validation_error(error, request_id));
        }
        if object_upload.write_chunk(&chunk).await.is_err() {
            let _ = object_upload.abort().await;
            return Err(ImportApiError::internal(request_id));
        }
    }
    drop(field);
    match multipart.next_field().await {
        Ok(None) => {}
        Ok(Some(_)) | Err(_) => {
            let _ = object_upload.abort().await;
            return Err(ImportApiError::bad_request(
                "single_file_required",
                request_id,
            ));
        }
    }
    let validated = match validator.finish() {
        Ok(validated) => validated,
        Err(error) => {
            let _ = object_upload.abort().await;
            return Err(map_validation_error(error, request_id));
        }
    };
    let stored = object_upload
        .commit()
        .await
        .map_err(|error| map_storage_error(error, request_id))?;
    if stored.size_bytes != validated.size_bytes {
        return Err(ImportApiError::internal(request_id));
    }
    let size_bytes = match i64::try_from(stored.size_bytes) {
        Ok(size) => size,
        Err(_) => return Err(ImportApiError::too_large(request_id)),
    };
    let record = NewImportUpload {
        object_id: Uuid::now_v7(),
        import_id: Uuid::now_v7(),
        file_id: Uuid::now_v7(),
        workspace_id: context.workspace_id(),
        actor_user_id: context.user_id(),
        object_key: stored.object_key.clone(),
        sha256: stored.sha256,
        size_bytes,
        original_filename: filename,
        declared_mime_type: validated.declared_mime_type,
        detected_format: validated.format.as_str().to_string(),
        request_id,
        automatic,
    };
    let result = match database::imports::register_upload(&state.auth.pool, &record).await {
        Ok(result) => result,
        Err(_) => match database::imports::get_import(
            &state.auth.pool,
            context.workspace_id(),
            record.import_id,
        )
        .await
        {
            Ok(committed) => committed,
            Err(ImportRepositoryError::NotFound) => {
                return Err(ImportApiError::internal(request_id));
            }
            Err(_) => return Err(ImportApiError::internal(request_id)),
        },
    };
    Ok((
        StatusCode::CREATED,
        Json(ApiResponse::new(ImportResponse::from(result), request_id)),
    )
        .into_response())
}

#[utoipa::path(
    post,
    path = "/api/v1/imports/{import_id}/compensations",
    params(
        ("import_id" = Uuid, Path, description = "Original ended import batch id"),
        ("Idempotency-Key" = String, Header, description = "Stable key for this compensation upload"),
        ("x-csrf-token" = String, Header),
        ("Origin" = String, Header)
    ),
    request_body(
        content = ImportCompensationUploadRequest,
        content_type = "multipart/form-data",
        description = "Exactly one corrective file and one reason field."
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 201, body = ImportCompensationResponse),
        (status = 200, body = ImportCompensationResponse),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody),
        (status = 409, body = ImportErrorBody),
        (status = 413, body = ImportErrorBody),
        (status = 415, body = ImportErrorBody)
    )
)]
pub async fn create_compensation(
    State(state): State<Arc<ImportState>>,
    Path(original_import_id): Path<Uuid>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> Result<Response, ImportApiError> {
    let (request_id, context) = require_import_compensation_write(&state, &headers).await?;
    let result: Result<Response, ImportApiError> = async {
        let raw_key = headers
            .get("idempotency-key")
            .and_then(|value| value.to_str().ok())
            .filter(|value| (16..=200).contains(&value.len()))
            .ok_or_else(|| ImportApiError::bad_request("idempotency_key_required", request_id))?;
        let mut reason: Option<String> = None;
        let mut pending_upload: Option<(Box<dyn ObjectUpload>, ValidatedUpload, String, String)> =
            None;
        while let Some(mut field) = multipart
            .next_field()
            .await
            .map_err(|_| ImportApiError::bad_request("invalid_multipart", request_id))?
        {
            match field.name() {
                Some("reason") if reason.is_none() => {
                    let mut bytes = Vec::new();
                    while let Some(chunk) = field
                        .chunk()
                        .await
                        .map_err(|_| ImportApiError::bad_request("invalid_reason", request_id))?
                    {
                        if bytes.len().saturating_add(chunk.len()) > 1000 {
                            return Err(ImportApiError::bad_request(
                                "compensation_reason_invalid",
                                request_id,
                            ));
                        }
                        bytes.extend_from_slice(&chunk);
                    }
                    let value = String::from_utf8(bytes)
                        .map_err(|_| ImportApiError::bad_request("invalid_reason", request_id))?;
                    let value = value.trim();
                    if value.is_empty() {
                        return Err(ImportApiError::bad_request(
                            "compensation_reason_invalid",
                            request_id,
                        ));
                    }
                    reason = Some(value.to_string());
                }
                Some("file") if pending_upload.is_none() => {
                    let filename = field.file_name().map(str::to_string).ok_or_else(|| {
                        ImportApiError::bad_request("filename_required", request_id)
                    })?;
                    let declared_mime = field.content_type().map(str::to_string);
                    let mut validator = UploadValidator::new(
                        state.upload_policy.clone(),
                        &filename,
                        declared_mime.as_deref(),
                    )
                    .map_err(|error| map_validation_error(error, request_id))?;
                    let mut object_upload = state
                        .storage
                        .begin_upload(context.workspace_id())
                        .await
                        .map_err(|error| map_storage_error(error, request_id))?;
                    let mut staged_hasher = Sha256::new();
                    while let Some(chunk) = field.chunk().await.map_err(|_| {
                        ImportApiError::bad_request("upload_interrupted", request_id)
                    })? {
                        validator
                            .observe(&chunk)
                            .map_err(|error| map_validation_error(error, request_id))?;
                        object_upload
                            .write_chunk(&chunk)
                            .await
                            .map_err(|error| map_storage_error(error, request_id))?;
                        staged_hasher.update(&chunk);
                    }
                    let validated = validator
                        .finish()
                        .map_err(|error| map_validation_error(error, request_id))?;
                    pending_upload = Some((
                        object_upload,
                        validated,
                        filename,
                        format!("{:x}", staged_hasher.finalize()),
                    ));
                }
                _ => {
                    return Err(ImportApiError::bad_request(
                        "compensation_multipart_invalid",
                        request_id,
                    ));
                }
            }
        }
        let reason = reason.ok_or_else(|| {
            ImportApiError::bad_request("compensation_reason_required", request_id)
        })?;
        let (object_upload, validated, filename, staged_sha256) = pending_upload
            .ok_or_else(|| ImportApiError::bad_request("single_file_required", request_id))?;
        let size_bytes = match i64::try_from(validated.size_bytes) {
            Ok(size_bytes) => size_bytes,
            Err(_) => {
                let _ = object_upload.abort().await;
                return Err(ImportApiError::too_large(request_id));
            }
        };
        let idempotency_key_hash = digest(&[&state.idempotency_pepper, raw_key]);
        let request_hash = digest(&[
            &context.workspace_id().to_string(),
            &original_import_id.to_string(),
            &reason,
            &staged_sha256,
            &filename,
            &validated.declared_mime_type,
            &context.user_id().to_string(),
        ]);
        let upload = NewCompensationUpload {
            object_id: Uuid::now_v7(),
            compensation_import_id: Uuid::now_v7(),
            file_id: Uuid::now_v7(),
            workspace_id: context.workspace_id(),
            actor_user_id: context.user_id(),
            original_import_id,
            reason,
            object_key: object_upload.object_key().to_string(),
            sha256: staged_sha256,
            size_bytes,
            original_filename: filename,
            declared_mime_type: validated.declared_mime_type,
            detected_format: validated.format.as_str().to_string(),
            idempotency_key_hash: idempotency_key_hash.clone(),
            request_hash: request_hash.clone(),
            audit_request_id: request_id,
        };
        let preparation =
            match database::compensations::prepare_compensation_upload(&state.auth.pool, &upload)
                .await
            {
                Ok(preparation) => preparation,
                Err(error) => {
                    let _ = object_upload.abort().await;
                    return Err(map_compensation_error(error, request_id));
                }
            };
        let prepared = match preparation {
            CompensationUploadPreparation::Replay(response) => {
                let _ = object_upload.abort().await;
                return Ok(
                    (StatusCode::OK, Json(ApiResponse::new(response, request_id))).into_response(),
                );
            }
            CompensationUploadPreparation::Ready(prepared) => prepared,
        };
        let stored = object_upload
            .commit()
            .await
            .map_err(|error| map_storage_error(error, request_id))?;
        // The database visibility, status and idempotency decision is complete while this
        // object is still temporary. A committed object is never physically deleted by the
        // import path; failures after this boundary remain visible to object governance.
        if stored.object_key != upload.object_key
            || stored.sha256 != upload.sha256
            || stored.size_bytes != validated.size_bytes
        {
            return Err(ImportApiError::internal(request_id));
        }
        let response =
            match database::compensations::create_compensation_upload(prepared, &upload).await {
                Ok(response) => response,
                Err(error) => {
                    match database::compensations::recover_compensation(
                        &state.auth.pool,
                        context.workspace_id(),
                        original_import_id,
                        &idempotency_key_hash,
                        &request_hash,
                    )
                    .await
                    {
                        Ok(Some(response)) => response,
                        Ok(None) => return Err(map_compensation_error(error, request_id)),
                        Err(recovery_error) => {
                            return Err(map_compensation_error(recovery_error, request_id));
                        }
                    }
                }
            };
        let status = if response.replayed {
            StatusCode::OK
        } else {
            StatusCode::CREATED
        };
        Ok((status, Json(ApiResponse::new(response, request_id))).into_response())
    }
    .await;
    match result {
        Ok(response) => Ok(response),
        Err(error) => Err(audit_import_failure(
            &state,
            &context,
            request_id,
            original_import_id,
            "import.compensation",
            error,
        )
        .await),
    }
}

#[utoipa::path(
    get,
    path = "/api/v1/imports/{import_id}/lineage",
    params(("import_id" = Uuid, Path, description = "Any import batch in the lineage")),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = ImportLineageResponse),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody),
        (status = 409, body = ImportErrorBody)
    )
)]
pub async fn lineage(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, &headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    context
        .require_permission(Permission::ReadImports)
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    let response =
        database::compensations::get_lineage(&state.auth.pool, context.workspace_id(), import_id)
            .await
            .map_err(|error| map_compensation_error(error, request_id))?;
    Ok(Json(ApiResponse::new(response, request_id)).into_response())
}

#[utoipa::path(
    post,
    path = "/api/v1/object-consistency/scans",
    params(
        ("Idempotency-Key" = String, Header),
        ("x-csrf-token" = String, Header),
        ("Origin" = String, Header)
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 202, body = ObjectConsistencyRun),
        (status = 200, body = ObjectConsistencyRun),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 409, body = ImportErrorBody)
    )
)]
pub async fn queue_object_scan(
    State(state): State<Arc<ImportState>>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let (request_id, context) = require_object_governance_write(&state, &headers).await?;
    let raw_key = require_idempotency_key(&headers, request_id)?;
    let root_fingerprint = state.storage.root_fingerprint();
    let key_hash = digest(&[&state.idempotency_pepper, raw_key]);
    let request_hash = digest(&[
        &context.workspace_id().to_string(),
        "object_consistency_scan:v1",
        &root_fingerprint,
    ]);
    let response = database::object_governance::queue_scan(
        &state.auth.pool,
        context.workspace_id(),
        context.user_id(),
        &key_hash,
        &request_hash,
        &root_fingerprint,
        request_id,
    )
    .await;
    let response = match response {
        Ok(response) => response,
        Err(error) => {
            let code = error.code();
            database::imports::record_import_audit(
                &state.auth.pool,
                context.workspace_id(),
                context.user_id(),
                request_id,
                None,
                "object.scan",
                "failure",
                code,
            )
            .await
            .map_err(|_| ImportApiError::internal(request_id))?;
            return Err(map_object_governance_error(error, request_id));
        }
    };
    let status = if response.replayed {
        StatusCode::OK
    } else {
        StatusCode::ACCEPTED
    };
    Ok((status, Json(ApiResponse::new(response, request_id))).into_response())
}

#[utoipa::path(
    get,
    path = "/api/v1/object-consistency/scans/{run_id}",
    params(("run_id" = Uuid, Path)),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = ObjectConsistencyReport),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody)
    )
)]
pub async fn object_scan_report(
    State(state): State<Arc<ImportState>>,
    Path(run_id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, &headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    context
        .require_permission(Permission::GovernObjects)
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    let report =
        database::object_governance::get_report(&state.auth.pool, context.workspace_id(), run_id)
            .await
            .map_err(|error| map_object_governance_error(error, request_id))?;
    Ok(Json(ApiResponse::new(report, request_id)).into_response())
}

#[utoipa::path(
    post,
    path = "/api/v1/object-consistency/findings/{finding_id}/quarantine",
    params(
        ("finding_id" = Uuid, Path),
        ("Idempotency-Key" = String, Header),
        ("x-csrf-token" = String, Header),
        ("Origin" = String, Header)
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 202, body = ObjectQuarantineResponse),
        (status = 200, body = ObjectQuarantineResponse),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody),
        (status = 409, body = ImportErrorBody)
    )
)]
pub async fn queue_object_quarantine(
    State(state): State<Arc<ImportState>>,
    Path(finding_id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let (request_id, context) = require_object_governance_write(&state, &headers).await?;
    let raw_key = require_idempotency_key(&headers, request_id)?;
    let key_hash = digest(&[&state.idempotency_pepper, raw_key]);
    let request_hash = digest(&[
        &context.workspace_id().to_string(),
        "object_quarantine:v1",
        &finding_id.to_string(),
        &context.user_id().to_string(),
    ]);
    let response = database::object_governance::queue_quarantine(
        &state.auth.pool,
        context.workspace_id(),
        context.user_id(),
        finding_id,
        &key_hash,
        &request_hash,
        request_id,
    )
    .await;
    let response = match response {
        Ok(response) => response,
        Err(error) => {
            let code = error.code();
            database::imports::record_import_audit(
                &state.auth.pool,
                context.workspace_id(),
                context.user_id(),
                request_id,
                None,
                "object.quarantine",
                "failure",
                code,
            )
            .await
            .map_err(|_| ImportApiError::internal(request_id))?;
            return Err(map_object_governance_error(error, request_id));
        }
    };
    let status = if response.replayed {
        StatusCode::OK
    } else {
        StatusCode::ACCEPTED
    };
    Ok((status, Json(ApiResponse::new(response, request_id))).into_response())
}

#[utoipa::path(
    get,
    path = "/api/v1/imports/{import_id}",
    params(("import_id" = Uuid, Path, description = "Import batch id")),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = ImportResponse),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody)
    )
)]
pub async fn get(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, &headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    context
        .require_permission(Permission::ReadImports)
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    let record = database::imports::get_import(&state.auth.pool, context.workspace_id(), import_id)
        .await
        .map_err(|error| match error {
            ImportRepositoryError::NotFound => ImportApiError::not_found(request_id),
            _ => ImportApiError::internal(request_id),
        })?;
    Ok(Json(ApiResponse::new(ImportResponse::from(record), request_id)).into_response())
}

#[utoipa::path(
    post,
    path = "/api/v1/imports/{import_id}/inspect",
    params(
        ("import_id" = Uuid, Path, description = "Import batch id"),
        ("x-csrf-token" = String, Header, description = "Session-bound CSRF token"),
        ("Origin" = String, Header, description = "Expected public origin")
    ),
    request_body = ImportInspectRequest,
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = ImportInspectResponse),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody)
    )
)]
pub async fn inspect(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    headers: HeaderMap,
    Json(request): Json<ImportInspectRequest>,
) -> Result<Response, ImportApiError> {
    let (request_id, context) = require_import_write(&state, &headers).await?;
    let record = load_import_object(&state, context.workspace_id(), import_id, request_id).await?;
    let bytes = state
        .storage
        .read(&record.object_key, state.upload_policy.max_bytes)
        .await
        .map_err(|_| ImportApiError::internal(request_id))?;
    let mapping =
        database::imports::get_mapping_fields(&state.auth.pool, context.workspace_id(), import_id)
            .await
            .map_err(|_| ImportApiError::internal(request_id))?;
    let mut parsed = application::imports::inspect_content(
        import_id,
        record.status,
        &record.detected_format,
        &bytes,
        request,
        &mapping,
    )
    .map_err(|error| map_parse_error(error, request_id))?;
    let saved = database::imports::save_inspection(
        &state.auth.pool,
        InspectionUpdate {
            workspace_id: context.workspace_id(),
            actor_user_id: context.user_id(),
            import_id,
            detected_encoding: parsed.encoding.value.as_deref(),
            detected_delimiter: parsed.delimiter.value.as_deref(),
            selected_sheet: parsed.selected_sheet.as_deref(),
            header_row: parsed.header_row as i32,
        },
    )
    .await
    .map_err(|error| map_repository_error(error, request_id))?;
    parsed.status = saved.status;
    parsed.preview_invalidated = saved.preview_invalidated;
    Ok(Json(ApiResponse::new(parsed, request_id)).into_response())
}

#[utoipa::path(
    put,
    path = "/api/v1/imports/{import_id}/mapping",
    params(
        ("import_id" = Uuid, Path, description = "Import batch id"),
        ("x-csrf-token" = String, Header, description = "Session-bound CSRF token"),
        ("Origin" = String, Header, description = "Expected public origin")
    ),
    request_body = ImportMappingRequest,
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = ImportMappingResponse),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody)
    )
)]
pub async fn save_mapping(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    headers: HeaderMap,
    Json(request): Json<ImportMappingRequest>,
) -> Result<Response, ImportApiError> {
    let (request_id, context) = require_import_write(&state, &headers).await?;
    let response = database::imports::save_mapping(
        &state.auth.pool,
        context.workspace_id(),
        context.user_id(),
        import_id,
        &request.dataset_type,
        request.template_version_id,
        &request.fields,
    )
    .await
    .map_err(|error| map_repository_error(error, request_id))?;
    Ok(Json(ApiResponse::new(response, request_id)).into_response())
}

#[utoipa::path(
    post,
    path = "/api/v1/imports/{import_id}/preview",
    params(
        ("import_id" = Uuid, Path, description = "Import batch id"),
        ("x-csrf-token" = String, Header, description = "Session-bound CSRF token"),
        ("Origin" = String, Header, description = "Expected public origin")
    ),
    request_body = ImportPreviewRequest,
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = ImportInspectResponse),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody)
    )
)]
pub async fn preview(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    headers: HeaderMap,
    Json(request): Json<ImportPreviewRequest>,
) -> Result<Response, ImportApiError> {
    let (request_id, context) = require_import_write(&state, &headers).await?;
    let record = load_import_object(&state, context.workspace_id(), import_id, request_id).await?;
    let bytes = state
        .storage
        .read(&record.object_key, state.upload_policy.max_bytes)
        .await
        .map_err(|_| ImportApiError::internal(request_id))?;
    let mapping =
        database::imports::get_mapping_fields(&state.auth.pool, context.workspace_id(), import_id)
            .await
            .map_err(|_| ImportApiError::internal(request_id))?;
    let mut parsed = application::imports::preview_content(
        import_id,
        record.status,
        &record.detected_format,
        &bytes,
        request,
        &mapping,
    )
    .map_err(|error| map_parse_error(error, request_id))?;
    database::imports::save_preview(
        &state.auth.pool,
        PreviewSave {
            workspace_id: context.workspace_id(),
            actor_user_id: context.user_id(),
            import_id,
            snapshot: PreviewInputSnapshot {
                detected_encoding: parsed.encoding.value.as_deref(),
                detected_delimiter: parsed.delimiter.value.as_deref(),
                selected_sheet: parsed.selected_sheet.as_deref(),
                header_row: parsed.header_row as i32,
                fields: &mapping,
            },
            rows: &parsed.preview_rows,
            errors: &parsed.errors,
            warnings: &parsed.warnings,
        },
    )
    .await
    .map_err(|error| map_repository_error(error, request_id))?;
    if record.status == ImportBatchStatus::Mapped {
        parsed.status = ImportBatchStatus::PreviewReady;
    }
    Ok(Json(ApiResponse::new(parsed, request_id)).into_response())
}

#[utoipa::path(
    post,
    path = "/api/v1/imports/{import_id}/validate",
    params(
        ("import_id" = Uuid, Path, description = "Import batch id"),
        ("x-csrf-token" = String, Header),
        ("Origin" = String, Header)
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = ImportValidateApiResponse),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody)
    )
)]
pub async fn validate(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let (request_id, context) = require_import_write(&state, &headers).await?;
    let result: Result<Response, ImportApiError> = async {
        let initial_context = database::imports::load_validation_context(
            &state.auth.pool,
            context.workspace_id(),
            import_id,
        )
        .await
        .map_err(|error| map_repository_error(error, request_id))?;
        let record =
            load_import_object(&state, context.workspace_id(), import_id, request_id).await?;
        let bytes = state
            .storage
            .read(&record.object_key, state.upload_policy.max_bytes)
            .await
            .map_err(|_| ImportApiError::internal(request_id))?;
        let full_parse = application::imports::parse_all_content_with_warnings(
            &record.detected_format,
            &bytes,
            ImportPreviewRequest {
                encoding: initial_context.detected_encoding.clone(),
                delimiter: initial_context.detected_delimiter.clone(),
                selected_sheet: initial_context.selected_sheet.clone(),
                header_row: Some(initial_context.header_row as u32),
            },
            &initial_context.mapping_fields,
            application::imports::DEFAULT_IMPORT_MAX_ROWS,
        )
        .map_err(|error| map_parse_error(error, request_id))?;
        database::imports::save_preview(
            &state.auth.pool,
            PreviewSave {
                workspace_id: context.workspace_id(),
                actor_user_id: context.user_id(),
                import_id,
                snapshot: PreviewInputSnapshot {
                    detected_encoding: initial_context.detected_encoding.as_deref(),
                    detected_delimiter: initial_context.detected_delimiter.as_deref(),
                    selected_sheet: initial_context.selected_sheet.as_deref(),
                    header_row: initial_context.header_row,
                    fields: &initial_context.mapping_fields,
                },
                rows: &full_parse.rows,
                errors: &[],
                warnings: &full_parse.warnings,
            },
        )
        .await
        .map_err(|error| map_repository_error(error, request_id))?;
        let validation_context = database::imports::load_validation_context(
            &state.auth.pool,
            context.workspace_id(),
            import_id,
        )
        .await
        .map_err(|error| map_repository_error(error, request_id))?;
        let outcome = application::import_jobs::validate_staging_rows(
            &validation_context.dataset_type,
            validation_context.rows.clone(),
        )
        .map_err(|error| match error {
            application::import_jobs::ValidationDefinitionError::DatasetNotConfirmable => {
                ImportApiError::bad_request("dataset_not_confirmable", request_id)
            }
            application::import_jobs::ValidationDefinitionError::InvalidStagingShape => {
                ImportApiError::bad_request("validation_stale", request_id)
            }
            application::import_jobs::ValidationDefinitionError::AutomaticSourceRequired => {
                ImportApiError::bad_request("automatic_source_required", request_id)
            }
        })?;
        let saved = database::imports::save_validation(
            &state.auth.pool,
            context.workspace_id(),
            context.user_id(),
            import_id,
            &validation_context,
            &outcome,
        )
        .await
        .map_err(|error| map_repository_error(error, request_id))?;
        let allowed_conflict_policies =
            application::import_jobs::allowed_conflict_policies(&validation_context.dataset_type)
                .to_vec();
        Ok(Json(ApiResponse::new(
            ImportValidateApiResponse {
                import_id,
                validation_version: saved.validation_version.to_string(),
                blocking_error_count: saved.blocking_error_count,
                warning_count: saved.warning_count,
                duplicate_count: saved.duplicate_count,
                conflict_count: saved.conflict_count,
                allowed_conflict_policies,
            },
            request_id,
        ))
        .into_response())
    }
    .await;
    match result {
        Ok(response) => Ok(response),
        Err(error) => Err(audit_import_failure(
            &state,
            &context,
            request_id,
            import_id,
            "import.validate",
            error,
        )
        .await),
    }
}

#[utoipa::path(
    post,
    path = "/api/v1/imports/{import_id}/confirm",
    params(
        ("import_id" = Uuid, Path, description = "Import batch id"),
        ("Idempotency-Key" = String, Header, description = "Stable key for this confirmation"),
        ("x-csrf-token" = String, Header),
        ("Origin" = String, Header)
    ),
    request_body = ImportConfirmRequest,
    security(("session_cookie" = [])),
    responses(
        (status = 202, body = ImportConfirmApiResponse),
        (status = 400, body = ImportErrorBody),
        (status = 409, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody)
    )
)]
pub async fn confirm(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    headers: HeaderMap,
    Json(request): Json<ImportConfirmRequest>,
) -> Result<Response, ImportApiError> {
    let (request_id, context) = require_import_write(&state, &headers).await?;
    let result: Result<Response, ImportApiError> = async {
        if database::imports::import_ingestion_mode(
            &state.auth.pool,
            context.workspace_id(),
            import_id,
        )
        .await
        .map_err(|error| map_repository_error(error, request_id))?
            != "manual"
        {
            return Err(ImportApiError::bad_request(
                "manual_confirmation_required",
                request_id,
            ));
        }
        let raw_key = headers
            .get("idempotency-key")
            .and_then(|value| value.to_str().ok())
            .filter(|value| (16..=200).contains(&value.len()))
            .ok_or_else(|| ImportApiError::bad_request("idempotency_key_required", request_id))?;
        let idempotency_key_hash = digest(&[&state.idempotency_pepper, raw_key]);
        let request_hash = digest(&[
            &context.workspace_id().to_string(),
            &import_id.to_string(),
            request.conflict_policy.as_str(),
            &context.user_id().to_string(),
        ]);
        let confirmed = database::imports::confirm_import(
            &state.auth.pool,
            context.workspace_id(),
            context.user_id(),
            import_id,
            database::imports::ImportConfirmationScope::manual(request.conflict_policy),
            &idempotency_key_hash,
            &request_hash,
        )
        .await
        .map_err(|error| map_repository_error(error, request_id))?;
        Ok((
            StatusCode::ACCEPTED,
            Json(ApiResponse::new(
                ImportConfirmApiResponse {
                    import_id,
                    job_id: confirmed.job_id,
                    status: confirmed.status,
                    conflict_policy: request.conflict_policy,
                    replayed: confirmed.replayed,
                },
                request_id,
            )),
        )
            .into_response())
    }
    .await;
    match result {
        Ok(response) => Ok(response),
        Err(error) => Err(audit_import_failure(
            &state,
            &context,
            request_id,
            import_id,
            "import.confirm",
            error,
        )
        .await),
    }
}

#[utoipa::path(
    post,
    path = "/api/v1/imports/{import_id}/automatic-confirm",
    params(
        ("import_id" = Uuid, Path),
        ("Idempotency-Key" = String, Header),
        ("x-csrf-token" = String, Header),
        ("Origin" = String, Header)
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 202, body = ImportConfirmApiResponse),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody),
        (status = 409, body = ImportErrorBody)
    )
)]
pub async fn automatic_confirm(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let (request_id, context) = require_import_write(&state, &headers).await?;
    let result: Result<Response, ImportApiError> = async {
        if !context.is_collector_account() {
            return Err(ImportApiError::forbidden(
                "automatic_account_required",
                request_id,
            ));
        }
        let automatic = database::imports::automatic_import_context(
            &state.auth.pool,
            context.workspace_id(),
            import_id,
        )
        .await
        .map_err(|error| map_repository_error(error, request_id))?;
        if automatic.fixed_template_code != format!("{}@1", automatic.dataset_type) {
            return Err(ImportApiError::bad_request(
                "automatic_template_invalid",
                request_id,
            ));
        }
        let raw_key = headers
            .get("idempotency-key")
            .and_then(|value| value.to_str().ok())
            .filter(|value| (16..=200).contains(&value.len()))
            .ok_or_else(|| ImportApiError::bad_request("idempotency_key_required", request_id))?;
        let mut record =
            load_import_object(&state, context.workspace_id(), import_id, request_id).await?;
        if record.status == ImportBatchStatus::Uploaded {
            let bytes = state
                .storage
                .read(&record.object_key, state.upload_policy.max_bytes)
                .await
                .map_err(|_| ImportApiError::internal(request_id))?;
            let definition = import_dataset_definitions()
                .into_iter()
                .find(|definition| definition.dataset_type == automatic.dataset_type)
                .ok_or_else(|| {
                    ImportApiError::bad_request("automatic_dataset_invalid", request_id)
                })?;
            let fields = definition
                .fields
                .iter()
                .map(|field| ImportMappingField {
                    source_column: field.code.clone(),
                    target_field: field.code.clone(),
                    transform: None,
                })
                .collect::<Vec<_>>();
            let mut inspected = application::imports::inspect_content(
                import_id,
                record.status,
                &record.detected_format,
                &bytes,
                ImportInspectRequest {
                    encoding: Some("utf-8".into()),
                    delimiter: Some(",".into()),
                    selected_sheet: None,
                    header_row: Some(1),
                },
                &fields,
            )
            .map_err(|error| map_parse_error(error, request_id))?;
            if !inspected.errors.is_empty() || inspected.total_rows == 0 {
                return Err(ImportApiError::bad_request(
                    "automatic_parse_failed",
                    request_id,
                ));
            }
            let saved = database::imports::save_inspection(
                &state.auth.pool,
                InspectionUpdate {
                    workspace_id: context.workspace_id(),
                    actor_user_id: context.user_id(),
                    import_id,
                    detected_encoding: inspected.encoding.value.as_deref(),
                    detected_delimiter: inspected.delimiter.value.as_deref(),
                    selected_sheet: None,
                    header_row: 1,
                },
            )
            .await
            .map_err(|error| map_repository_error(error, request_id))?;
            inspected.status = saved.status;
            database::imports::save_mapping(
                &state.auth.pool,
                context.workspace_id(),
                context.user_id(),
                import_id,
                &automatic.dataset_type,
                None,
                &fields,
            )
            .await
            .map_err(|error| map_repository_error(error, request_id))?;
            let full_parse = application::imports::parse_all_content_with_warnings(
                &record.detected_format,
                &bytes,
                ImportPreviewRequest {
                    encoding: Some("utf-8".into()),
                    delimiter: Some(",".into()),
                    selected_sheet: None,
                    header_row: Some(1),
                },
                &fields,
                application::imports::DEFAULT_IMPORT_MAX_ROWS,
            )
            .map_err(|error| map_parse_error(error, request_id))?;
            database::imports::save_preview(
                &state.auth.pool,
                PreviewSave {
                    workspace_id: context.workspace_id(),
                    actor_user_id: context.user_id(),
                    import_id,
                    snapshot: PreviewInputSnapshot {
                        detected_encoding: Some("utf-8"),
                        detected_delimiter: Some(","),
                        selected_sheet: None,
                        header_row: 1,
                        fields: &fields,
                    },
                    rows: &full_parse.rows,
                    errors: &[],
                    warnings: &full_parse.warnings,
                },
            )
            .await
            .map_err(|error| map_repository_error(error, request_id))?;
            let validation_context = database::imports::load_validation_context(
                &state.auth.pool,
                context.workspace_id(),
                import_id,
            )
            .await
            .map_err(|error| map_repository_error(error, request_id))?;
            let outcome = application::import_jobs::validate_automatic_staging_rows(
                &validation_context.dataset_type,
                &automatic.data_source_code,
                validation_context.rows.clone(),
            )
            .map_err(|_| ImportApiError::bad_request("automatic_validation_failed", request_id))?;
            let saved = database::imports::save_validation(
                &state.auth.pool,
                context.workspace_id(),
                context.user_id(),
                import_id,
                &validation_context,
                &outcome,
            )
            .await
            .map_err(|error| map_repository_error(error, request_id))?;
            if saved.blocking_error_count > 0 {
                return Err(ImportApiError::bad_request(
                    "automatic_validation_failed",
                    request_id,
                ));
            }
            record =
                load_import_object(&state, context.workspace_id(), import_id, request_id).await?;
        }
        if !matches!(
            record.status,
            ImportBatchStatus::PreviewReady
                | ImportBatchStatus::Confirmed
                | ImportBatchStatus::Importing
                | ImportBatchStatus::Succeeded
        ) {
            return Err(ImportApiError::bad_request(
                "automatic_state_invalid",
                request_id,
            ));
        }
        let idempotency_key_hash = digest(&[&state.idempotency_pepper, raw_key]);
        let request_hash = digest(&[
            &context.workspace_id().to_string(),
            &import_id.to_string(),
            "automatic",
            &automatic.dataset_type,
            &automatic.collection_date.to_string(),
            &context.user_id().to_string(),
        ]);
        let confirmed = database::imports::confirm_import(
            &state.auth.pool,
            context.workspace_id(),
            context.user_id(),
            import_id,
            database::imports::ImportConfirmationScope::automatic(),
            &idempotency_key_hash,
            &request_hash,
        )
        .await
        .map_err(|error| map_repository_error(error, request_id))?;
        database::imports::mark_automatic_queued(
            &state.auth.pool,
            context.workspace_id(),
            import_id,
        )
        .await
        .map_err(|error| map_repository_error(error, request_id))?;
        Ok((
            StatusCode::ACCEPTED,
            Json(ApiResponse::new(
                ImportConfirmApiResponse {
                    import_id,
                    job_id: confirmed.job_id,
                    status: confirmed.status,
                    conflict_policy: ImportConflictPolicy::Skip,
                    replayed: confirmed.replayed,
                },
                request_id,
            )),
        )
            .into_response())
    }
    .await;
    match result {
        Ok(response) => Ok(response),
        Err(error) => {
            let _ = database::imports::fail_automatic_import(
                &state.auth.pool,
                context.workspace_id(),
                context.user_id(),
                import_id,
                error.code,
            )
            .await;
            Err(audit_import_failure(
                &state,
                &context,
                request_id,
                import_id,
                "import.automatic_confirm",
                error,
            )
            .await)
        }
    }
}

#[utoipa::path(
    post,
    path = "/api/v1/imports/{import_id}/rollback-check",
    params(
        ("import_id" = Uuid, Path, description = "Import batch id"),
        ("x-csrf-token" = String, Header),
        ("Origin" = String, Header)
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = ImportRollbackCheckResponse),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody),
        (status = 409, body = ImportErrorBody)
    )
)]
pub async fn rollback_check(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let (request_id, context) = require_import_rollback_write(&state, &headers).await?;
    let result: Result<Response, ImportApiError> = async {
        let precheck = database::imports::create_rollback_check(
            &state.auth.pool,
            context.workspace_id(),
            context.user_id(),
            import_id,
            request_id,
        )
        .await
        .map_err(|error| map_repository_error(error, request_id))?;
        let response =
            rollback_check_response(&state, context.workspace_id(), precheck, request_id).await?;
        Ok(Json(ApiResponse::new(response, request_id)).into_response())
    }
    .await;
    match result {
        Ok(response) => Ok(response),
        Err(error) => Err(audit_import_failure(
            &state,
            &context,
            request_id,
            import_id,
            "import.rollback_check",
            error,
        )
        .await),
    }
}

#[utoipa::path(
    get,
    path = "/api/v1/imports/{import_id}/rollback-conflicts",
    params(
        ("import_id" = Uuid, Path, description = "Import batch id"),
        ImportRollbackConflictsQuery
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = ImportRollbackConflictsResponse),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody)
    )
)]
pub async fn rollback_conflicts(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    Query(query): Query<ImportRollbackConflictsQuery>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, &headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    context
        .require_permission(Permission::Rollback)
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    let page = database::imports::list_rollback_conflicts(
        &state.auth.pool,
        context.workspace_id(),
        import_id,
        query.precheck_request_id,
        query.cursor.as_deref(),
        query.limit.unwrap_or(100),
    )
    .await
    .map_err(|error| map_repository_error(error, request_id))?;
    Ok(Json(ApiResponse::new(
        ImportRollbackConflictsResponse {
            import_id,
            precheck_request_id: query.precheck_request_id,
            items: page.items,
            next_cursor: page.next_cursor,
        },
        request_id,
    ))
    .into_response())
}

#[utoipa::path(
    post,
    path = "/api/v1/imports/{import_id}/rollback",
    params(
        ("import_id" = Uuid, Path, description = "Import batch id"),
        ("Idempotency-Key" = String, Header, description = "Stable key for this rollback request"),
        ("x-csrf-token" = String, Header),
        ("Origin" = String, Header)
    ),
    request_body = ImportRollbackRequest,
    security(("session_cookie" = [])),
    responses(
        (status = 202, body = ImportRollbackResponse),
        (status = 400, body = ImportErrorBody),
        (
            status = 409,
            description = "Full recheck conflict result or stable rollback request error",
            body = ImportRollbackConflictApiResponse
        ),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody)
    )
)]
pub async fn rollback(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    headers: HeaderMap,
    Json(request): Json<ImportRollbackRequest>,
) -> Result<Response, ImportApiError> {
    let (request_id, context) = require_import_rollback_write(&state, &headers).await?;
    let result: Result<Response, ImportApiError> = async {
        let raw_key = headers
            .get("idempotency-key")
            .and_then(|value| value.to_str().ok())
            .filter(|value| (16..=200).contains(&value.len()))
            .ok_or_else(|| ImportApiError::bad_request("idempotency_key_required", request_id))?;
        if request.precheck_fingerprint.len() != 64
            || !request
                .precheck_fingerprint
                .bytes()
                .all(|value| value.is_ascii_digit() || (b'a'..=b'f').contains(&value))
        {
            return Err(ImportApiError::bad_request(
                "rollback_fingerprint_invalid",
                request_id,
            ));
        }
        let idempotency_key_hash = digest(&[&state.idempotency_pepper, raw_key]);
        let request_hash = digest(&[
            &context.workspace_id().to_string(),
            &import_id.to_string(),
            &request.precheck_request_id.to_string(),
            &request.precheck_fingerprint,
            &context.user_id().to_string(),
        ]);
        match database::imports::queue_rollback(
            &state.auth.pool,
            context.workspace_id(),
            context.user_id(),
            import_id,
            request.precheck_request_id,
            &request.precheck_fingerprint,
            &idempotency_key_hash,
            &request_hash,
            request_id,
        )
        .await
        .map_err(|error| map_repository_error(error, request_id))?
        {
            QueueRollbackResult::Queued(queued) => Ok((
                StatusCode::ACCEPTED,
                Json(ApiResponse::new(
                    ImportRollbackResponse {
                        import_id,
                        precheck_request_id: queued.request_id,
                        job_id: queued.job_id,
                        status: queued.status,
                        replayed: queued.replayed,
                    },
                    request_id,
                )),
            )
                .into_response()),
            QueueRollbackResult::Conflict(precheck) => {
                let response =
                    rollback_check_response(&state, context.workspace_id(), precheck, request_id)
                        .await?;
                Ok((
                    StatusCode::CONFLICT,
                    Json(ApiResponse::new(response, request_id)),
                )
                    .into_response())
            }
        }
    }
    .await;
    match result {
        Ok(response) => Ok(response),
        Err(error) => Err(audit_import_failure(
            &state,
            &context,
            request_id,
            import_id,
            "import.rollback",
            error,
        )
        .await),
    }
}

#[utoipa::path(
    get,
    path = "/api/v1/imports/{import_id}/events",
    params(
        ("import_id" = Uuid, Path, description = "Import batch id"),
        ("Last-Event-ID" = Option<String>, Header, description = "Last processed decimal event sequence")
    ),
    security(("session_cookie" = [])),
    responses(
        (
            status = 200,
            body = ImportSseEventFrame,
            content_type = "text/event-stream",
            description = "SSE frames use event_seq as id, event_type as event, and the matching discriminated JSON schema as data"
        ),
        (status = 400, body = ImportEventStreamErrorBody, description = "event_id_invalid"),
        (status = 401, body = ImportEventStreamErrorBody, description = "auth_required"),
        (status = 403, body = ImportEventStreamErrorBody, description = "permission_denied"),
        (status = 404, body = ImportEventStreamErrorBody, description = "event_not_visible"),
        (status = 500, body = ImportEventStreamErrorBody, description = "internal_error")
    )
)]
pub async fn events(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, &headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    context
        .require_permission(Permission::ReadImports)
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    database::imports::get_import(&state.auth.pool, context.workspace_id(), import_id)
        .await
        .map_err(|error| map_event_repository_error(error, request_id))?;
    let after = match headers.get("last-event-id") {
        None => 0,
        Some(value) => value
            .to_str()
            .ok()
            .and_then(|value| value.parse::<i64>().ok())
            .filter(|value| *value >= 0)
            .ok_or_else(|| ImportApiError::bad_request("event_id_invalid", request_id))?,
    };
    let initial = database::job_queue::list_events_after(
        &state.auth.pool,
        context.workspace_id(),
        import_id,
        after,
    )
    .await
    .map_err(|error| map_event_queue_error(error, request_id))?;
    let (sender, receiver) = tokio::sync::mpsc::channel::<Result<Event, Infallible>>(32);
    let pool = state.auth.pool.clone();
    let auth_state = state.auth.clone();
    let revalidation_headers = headers;
    let workspace_id = context.workspace_id();
    let user_id = context.user_id();
    let session_id = context.session_id();
    let revalidate_seconds = state.sse_revalidate_seconds;
    tokio::spawn(async move {
        let mut cursor = after;
        let mut pending = VecDeque::from(initial);
        let mut poll = tokio::time::interval(Duration::from_secs(1));
        let mut revalidate = tokio::time::interval(Duration::from_secs(revalidate_seconds));
        poll.tick().await;
        revalidate.tick().await;
        loop {
            if let Some(event) = pending.front() {
                let terminal = is_terminal_event_type(&event.event_type);
                let sse = Event::default()
                    .id(event.event_seq.to_string())
                    .event(event.event_type.clone())
                    .data(serde_json::to_string(&event).unwrap_or_else(|_| "{}".to_string()));
                match send_frame_or_revalidate(&sender, &mut revalidate, sse).await {
                    FrameWait::Sent(Ok(())) => {
                        cursor = event.event_seq;
                        pending.pop_front();
                        if terminal {
                            break;
                        }
                    }
                    FrameWait::Sent(Err(_)) => break,
                    FrameWait::Revalidate => {
                        if let Err(reason_code) = revalidate_event_stream(
                            &auth_state,
                            &revalidation_headers,
                            session_id,
                            user_id,
                            workspace_id,
                            import_id,
                        )
                        .await
                        {
                            audit_event_stream_termination(
                                &pool,
                                workspace_id,
                                user_id,
                                request_id,
                                import_id,
                                reason_code,
                            )
                            .await;
                            break;
                        }
                    }
                }
                continue;
            }
            tokio::select! {
                biased;
                _ = revalidate.tick() => {
                    if let Err(reason_code) = revalidate_event_stream(
                        &auth_state,
                        &revalidation_headers,
                        session_id,
                        user_id,
                        workspace_id,
                        import_id,
                    ).await {
                        audit_event_stream_termination(
                            &pool,
                            workspace_id,
                            user_id,
                            request_id,
                            import_id,
                            reason_code,
                        ).await;
                        break;
                    }
                }
                _ = poll.tick() => {
                    match database::job_queue::list_events_after(
                        &pool,
                        workspace_id,
                        import_id,
                        cursor,
                    ).await {
                        Ok(events) if !events.is_empty() => pending = VecDeque::from(events),
                        Ok(_) => {}
                        Err(error) => {
                            let reason_code = match error {
                                database::job_queue::JobQueueError::EventNotVisible => {
                                    "event_not_visible"
                                }
                                database::job_queue::JobQueueError::EventIdInvalid => {
                                    "event_id_invalid"
                                }
                                _ => "internal_error",
                            };
                            audit_event_stream_termination(
                                &pool,
                                workspace_id,
                                user_id,
                                request_id,
                                import_id,
                                reason_code,
                            ).await;
                            break;
                        }
                    }
                }
            }
        }
    });
    let stream = tokio_stream::wrappers::ReceiverStream::new(receiver);
    Ok(Sse::new(stream)
        .keep_alive(
            KeepAlive::new()
                .interval(Duration::from_secs(15))
                .text("heartbeat"),
        )
        .into_response())
}

enum FrameWait {
    Sent(Result<(), tokio::sync::mpsc::error::SendError<Result<Event, Infallible>>>),
    Revalidate,
}

async fn send_frame_or_revalidate(
    sender: &tokio::sync::mpsc::Sender<Result<Event, Infallible>>,
    revalidate: &mut tokio::time::Interval,
    event: Event,
) -> FrameWait {
    tokio::select! {
        biased;
        _ = revalidate.tick() => FrameWait::Revalidate,
        result = sender.send(Ok(event)) => FrameWait::Sent(result),
    }
}

async fn audit_event_stream_termination(
    pool: &sqlx::PgPool,
    workspace_id: Uuid,
    user_id: Uuid,
    request_id: Uuid,
    import_id: Uuid,
    reason_code: &'static str,
) {
    let outcome = if reason_code == "internal_error" {
        "failure"
    } else {
        "denied"
    };
    let _ = database::imports::record_import_audit(
        pool,
        workspace_id,
        user_id,
        request_id,
        Some(import_id),
        "import.events_access_terminated",
        outcome,
        reason_code,
    )
    .await;
}

fn is_terminal_event_type(event_type: &str) -> bool {
    matches!(
        event_type,
        "succeeded"
            | "failed"
            | "dead_letter"
            | "rolled_back"
            | "rollback_conflict"
            | "rollback_failed"
    )
}

async fn revalidate_event_stream(
    auth_state: &AuthState,
    headers: &HeaderMap,
    expected_session_id: Uuid,
    expected_user_id: Uuid,
    expected_workspace_id: Uuid,
    import_id: Uuid,
) -> Result<(), &'static str> {
    let context = auth::current_context(auth_state, headers)
        .await
        .map_err(|error| error.code())?;
    if context.session_id() != expected_session_id || context.user_id() != expected_user_id {
        return Err("auth_required");
    }
    if context.workspace_id() != expected_workspace_id {
        return Err("event_not_visible");
    }
    context
        .require_permission(Permission::ReadImports)
        .map_err(|error| error.code())?;
    match database::imports::get_import(&auth_state.pool, expected_workspace_id, import_id).await {
        Ok(_) => Ok(()),
        Err(ImportRepositoryError::NotFound) => Err("event_not_visible"),
        Err(_) => Err("internal_error"),
    }
}

#[utoipa::path(
    get,
    path = "/api/v1/imports/{import_id}/errors",
    params(("import_id" = Uuid, Path, description = "Import batch id"), ImportErrorsQuery),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = ImportErrorsResponse),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody)
    )
)]
pub async fn errors(
    State(state): State<Arc<ImportState>>,
    Path(import_id): Path<Uuid>,
    Query(query): Query<ImportErrorsQuery>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, &headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    context
        .require_permission(Permission::ReadImports)
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    let page = database::imports::list_errors(
        &state.auth.pool,
        context.workspace_id(),
        import_id,
        query.cursor.as_deref(),
        query.limit.unwrap_or(100),
    )
    .await
    .map_err(|error| map_repository_error(error, request_id))?;
    Ok(Json(ApiResponse::new(
        ImportErrorsResponse {
            import_id,
            items: page.items,
            next_cursor: page.next_cursor,
        },
        request_id,
    ))
    .into_response())
}

#[utoipa::path(
    get,
    path = "/api/v1/import-templates",
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = ImportTemplatesResponse),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody)
    )
)]
pub async fn list_templates(
    State(state): State<Arc<ImportState>>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, &headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    context
        .require_permission(Permission::ReadImports)
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    let items = database::imports::list_templates(&state.auth.pool, context.workspace_id())
        .await
        .map_err(|_| ImportApiError::internal(request_id))?;
    Ok(Json(ApiResponse::new(
        ImportTemplatesResponse { items },
        request_id,
    ))
    .into_response())
}

#[utoipa::path(
    get,
    path = "/api/v1/import-datasets",
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = ImportDatasetsResponse),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody)
    )
)]
pub async fn list_datasets(
    State(state): State<Arc<ImportState>>,
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, &headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    context
        .require_permission(Permission::ReadImports)
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    Ok(Json(ApiResponse::new(
        ImportDatasetsResponse {
            items: import_dataset_definitions(),
        },
        request_id,
    ))
    .into_response())
}

#[utoipa::path(
    post,
    path = "/api/v1/import-templates",
    params(
        ("x-csrf-token" = String, Header, description = "Session-bound CSRF token"),
        ("Origin" = String, Header, description = "Expected public origin")
    ),
    request_body = ImportTemplateCreateRequest,
    security(("session_cookie" = [])),
    responses(
        (status = 201, body = ImportTemplateVersionResponse),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody)
    )
)]
pub async fn create_template(
    State(state): State<Arc<ImportState>>,
    headers: HeaderMap,
    Json(request): Json<ImportTemplateCreateRequest>,
) -> Result<Response, ImportApiError> {
    let (request_id, context) = require_import_write(&state, &headers).await?;
    let response = database::imports::create_template(
        &state.auth.pool,
        context.workspace_id(),
        context.user_id(),
        &request.dataset_type,
        &request.name,
        request.description.as_deref(),
        &request.fields,
    )
    .await
    .map_err(|error| map_repository_error(error, request_id))?;
    Ok((
        StatusCode::CREATED,
        Json(ApiResponse::new(response, request_id)),
    )
        .into_response())
}

async fn rollback_check_response(
    state: &ImportState,
    workspace_id: Uuid,
    precheck: RollbackPrecheck,
    request_id: Uuid,
) -> Result<ImportRollbackCheckResponse, ImportApiError> {
    let page = database::imports::list_rollback_conflicts(
        &state.auth.pool,
        workspace_id,
        precheck.import_id,
        precheck.request_id,
        None,
        100,
    )
    .await
    .map_err(|error| map_repository_error(error, request_id))?;
    let can_rollback = precheck.can_rollback();
    Ok(ImportRollbackCheckResponse {
        import_id: precheck.import_id,
        precheck_request_id: precheck.request_id,
        precheck_fingerprint: precheck.fingerprint,
        rollback_capability: precheck.rollback_capability,
        change_log_version: precheck
            .change_log_version
            .and_then(|value| u32::try_from(value).ok()),
        can_rollback,
        compensation_recommended: !can_rollback
            || precheck.rollback_capability == RollbackCapability::CompensationOnly,
        affected_count: precheck.affected_count,
        conflict_count: precheck.conflicts.len() as u32,
        conflicts: page.items,
        next_cursor: page.next_cursor,
    })
}

fn map_validation_error(error: UploadValidationError, request_id: Uuid) -> ImportApiError {
    match error {
        UploadValidationError::InvalidConfiguration => ImportApiError::internal(request_id),
        UploadValidationError::MissingFilename => {
            ImportApiError::bad_request("filename_required", request_id)
        }
        UploadValidationError::DangerousFilename => {
            ImportApiError::bad_request("dangerous_filename", request_id)
        }
        UploadValidationError::UnsupportedFormat => {
            ImportApiError::unsupported("unsupported_format", request_id)
        }
        UploadValidationError::MimeMismatch => {
            ImportApiError::unsupported("mime_mismatch", request_id)
        }
        UploadValidationError::FileTooLarge => ImportApiError::too_large(request_id),
        UploadValidationError::EmptyFile => ImportApiError::bad_request("empty_file", request_id),
    }
}

fn map_storage_error(_: ObjectStorageError, request_id: Uuid) -> ImportApiError {
    ImportApiError::internal(request_id)
}

fn map_parse_error(error: ImportParseError, request_id: Uuid) -> ImportApiError {
    match error {
        ImportParseError::UnsupportedFormat => {
            ImportApiError::unsupported("unsupported_format", request_id)
        }
        ImportParseError::InvalidEncoding => {
            ImportApiError::bad_request("invalid_encoding", request_id)
        }
        ImportParseError::InvalidDelimiter => {
            ImportApiError::bad_request("invalid_delimiter", request_id)
        }
        ImportParseError::InvalidSheet => ImportApiError::bad_request("invalid_sheet", request_id),
        ImportParseError::SpreadsheetReadFailed => {
            ImportApiError::bad_request("spreadsheet_read_failed", request_id)
        }
        ImportParseError::TooManyRows => {
            ImportApiError::bad_request("import_row_limit_exceeded", request_id)
        }
        ImportParseError::UnsupportedTransform => {
            ImportApiError::bad_request("unsupported_transform", request_id)
        }
    }
}

fn map_repository_error(error: ImportRepositoryError, request_id: Uuid) -> ImportApiError {
    match error {
        ImportRepositoryError::NotFound => ImportApiError::not_found(request_id),
        ImportRepositoryError::InvalidAutomaticMetadata => {
            ImportApiError::bad_request("automatic_metadata_invalid", request_id)
        }
        ImportRepositoryError::InvalidTransition => {
            ImportApiError::bad_request("invalid_import_status", request_id)
        }
        ImportRepositoryError::InvalidMappingDefinition(error) => match error {
            domain::import::ImportMappingDefinitionError::UnknownDatasetType => {
                ImportApiError::bad_request("unknown_dataset_type", request_id)
            }
            domain::import::ImportMappingDefinitionError::UnknownTargetField => {
                ImportApiError::bad_request("unknown_target_field", request_id)
            }
            domain::import::ImportMappingDefinitionError::UnsupportedTransform => {
                ImportApiError::bad_request("unsupported_transform", request_id)
            }
        },
        ImportRepositoryError::TemplateVersionNotFound => {
            ImportApiError::bad_request("template_version_not_found", request_id)
        }
        ImportRepositoryError::TemplateVersionMismatch => {
            ImportApiError::bad_request("template_version_mismatch", request_id)
        }
        ImportRepositoryError::PreviewInputsChanged => {
            ImportApiError::bad_request("preview_inputs_changed", request_id)
        }
        ImportRepositoryError::ValidationRequired => {
            ImportApiError::bad_request("validation_required", request_id)
        }
        ImportRepositoryError::ValidationStale => {
            ImportApiError::bad_request("validation_stale", request_id)
        }
        ImportRepositoryError::BlockingErrorsPresent => {
            ImportApiError::bad_request("blocking_errors_present", request_id)
        }
        ImportRepositoryError::ConflictPolicyNotAllowed => {
            ImportApiError::bad_request("conflict_policy_not_allowed", request_id)
        }
        ImportRepositoryError::IdempotencyKeyReused => {
            ImportApiError::conflict("idempotency_key_reused", request_id)
        }
        ImportRepositoryError::ConfirmationConflict => {
            ImportApiError::conflict("confirmation_conflict", request_id)
        }
        ImportRepositoryError::EventIdInvalid => {
            ImportApiError::bad_request("event_id_invalid", request_id)
        }
        ImportRepositoryError::RollbackNotAllowed => {
            ImportApiError::conflict("rollback_not_allowed", request_id)
        }
        ImportRepositoryError::RollbackNotAvailable => {
            ImportApiError::conflict("rollback_not_available", request_id)
        }
        ImportRepositoryError::RollbackPreconditionStale => {
            ImportApiError::conflict("rollback_precondition_stale", request_id)
        }
        ImportRepositoryError::RollbackConflict => {
            ImportApiError::conflict("rollback_conflict", request_id)
        }
        ImportRepositoryError::RollbackAlreadyCompleted => {
            ImportApiError::conflict("rollback_already_completed", request_id)
        }
        ImportRepositoryError::RollbackInProgress => {
            ImportApiError::conflict("rollback_in_progress", request_id)
        }
        ImportRepositoryError::RollbackIdempotencyKeyReused => {
            ImportApiError::conflict("rollback_idempotency_key_reused", request_id)
        }
        ImportRepositoryError::RollbackCursorInvalid => {
            ImportApiError::bad_request("rollback_cursor_invalid", request_id)
        }
        _ => ImportApiError::internal(request_id),
    }
}

fn automatic_metadata(
    headers: &HeaderMap,
    request_id: Uuid,
) -> Result<Option<AutomaticImportMetadata>, ImportApiError> {
    let mode = headers
        .get("x-ingestion-mode")
        .and_then(|value| value.to_str().ok());
    let automatic = match mode {
        None | Some("manual") => return Ok(None),
        Some("automatic") => true,
        Some(_) => false,
    };
    if !automatic {
        return Err(ImportApiError::bad_request(
            "automatic_metadata_invalid",
            request_id,
        ));
    }
    let header = |name: &'static str| {
        headers
            .get(name)
            .and_then(|value| value.to_str().ok())
            .filter(|value| !value.trim().is_empty())
            .map(str::to_string)
            .ok_or_else(|| ImportApiError::bad_request("automatic_metadata_required", request_id))
    };
    let dataset_type = header("x-dataset-type")?;
    if !matches!(
        dataset_type.as_str(),
        "futures_catalog_v1"
            | "trading_calendar_v1"
            | "daily_market_prices_v1"
            | "seat_positions_v1"
    ) {
        return Err(ImportApiError::bad_request(
            "automatic_dataset_invalid",
            request_id,
        ));
    }
    let fixed_template_code = header("x-template-version")?;
    if fixed_template_code != format!("{dataset_type}@1") {
        return Err(ImportApiError::bad_request(
            "automatic_template_invalid",
            request_id,
        ));
    }
    let collection_date = Date::parse(
        &header("x-collection-date")?,
        format_description!("[year]-[month]-[day]"),
    )
    .map_err(|_| ImportApiError::bad_request("automatic_date_invalid", request_id))?;
    Ok(Some(AutomaticImportMetadata {
        dataset_type,
        data_source_code: header("x-data-source-code")?,
        collection_date,
        fixed_template_code,
    }))
}

fn map_event_repository_error(error: ImportRepositoryError, request_id: Uuid) -> ImportApiError {
    match error {
        ImportRepositoryError::NotFound => {
            ImportApiError::not_found_with_code("event_not_visible", request_id)
        }
        _ => ImportApiError::internal(request_id),
    }
}

fn map_event_queue_error(
    error: database::job_queue::JobQueueError,
    request_id: Uuid,
) -> ImportApiError {
    use database::job_queue::JobQueueError;
    match error {
        JobQueueError::EventNotVisible => {
            ImportApiError::not_found_with_code("event_not_visible", request_id)
        }
        JobQueueError::EventIdInvalid => {
            ImportApiError::bad_request("event_id_invalid", request_id)
        }
        _ => ImportApiError::internal(request_id),
    }
}

fn map_compensation_error(error: CompensationRepositoryError, request_id: Uuid) -> ImportApiError {
    match error {
        CompensationRepositoryError::NotFound => ImportApiError::not_found(request_id),
        CompensationRepositoryError::NotAllowed => {
            ImportApiError::conflict("compensation_not_allowed", request_id)
        }
        CompensationRepositoryError::Cycle => {
            ImportApiError::conflict("compensation_cycle", request_id)
        }
        CompensationRepositoryError::LineageDepthExceeded => {
            ImportApiError::conflict("compensation_lineage_too_deep", request_id)
        }
        CompensationRepositoryError::IdempotencyKeyReused => {
            ImportApiError::conflict("idempotency_key_reused", request_id)
        }
        _ => ImportApiError::internal(request_id),
    }
}

fn map_object_governance_error(
    error: database::object_governance::ObjectGovernanceError,
    request_id: Uuid,
) -> ImportApiError {
    use database::object_governance::ObjectGovernanceError;
    match error {
        ObjectGovernanceError::NotFound => ImportApiError::object_consistency_not_found(request_id),
        ObjectGovernanceError::IdempotencyKeyReused => {
            ImportApiError::conflict("idempotency_key_reused", request_id)
        }
        ObjectGovernanceError::QuarantineNotAllowed => {
            ImportApiError::conflict("object_quarantine_not_allowed", request_id)
        }
        ObjectGovernanceError::FindingStale => {
            ImportApiError::conflict("object_finding_stale", request_id)
        }
        _ => ImportApiError::object_consistency_error(request_id),
    }
}

fn require_idempotency_key(headers: &HeaderMap, request_id: Uuid) -> Result<&str, ImportApiError> {
    headers
        .get("idempotency-key")
        .and_then(|value| value.to_str().ok())
        .filter(|value| (16..=200).contains(&value.len()))
        .ok_or_else(|| ImportApiError::bad_request("idempotency_key_required", request_id))
}

async fn require_import_write(
    state: &ImportState,
    headers: &HeaderMap,
) -> Result<(Uuid, auth::AuthContext), ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    if let Err(error) = auth::ensure_allowed_origin(&state.auth.config, headers) {
        return Err(audit_write_denial(state, &context, request_id, error).await);
    }
    if let Err(error) = auth::ensure_csrf(&state.auth, headers).await {
        return Err(audit_write_denial(state, &context, request_id, error).await);
    }
    if let Err(error) = context.require_permission(Permission::Upload) {
        return Err(audit_write_denial(state, &context, request_id, error).await);
    }
    Ok((request_id, context))
}

async fn require_import_rollback_write(
    state: &ImportState,
    headers: &HeaderMap,
) -> Result<(Uuid, auth::AuthContext), ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    if let Err(error) = auth::ensure_allowed_origin(&state.auth.config, headers) {
        return Err(audit_write_denial(state, &context, request_id, error).await);
    }
    if let Err(error) = auth::ensure_csrf(&state.auth, headers).await {
        return Err(audit_write_denial(state, &context, request_id, error).await);
    }
    if let Err(error) = context.require_permission(Permission::Rollback) {
        return Err(audit_write_denial(state, &context, request_id, error).await);
    }
    Ok((request_id, context))
}

async fn require_import_compensation_write(
    state: &ImportState,
    headers: &HeaderMap,
) -> Result<(Uuid, auth::AuthContext), ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    if let Err(error) = auth::ensure_allowed_origin(&state.auth.config, headers) {
        return Err(audit_write_denial(state, &context, request_id, error).await);
    }
    if let Err(error) = auth::ensure_csrf(&state.auth, headers).await {
        return Err(audit_write_denial(state, &context, request_id, error).await);
    }
    if let Err(error) = context.require_permission(Permission::Compensate) {
        return Err(audit_write_denial(state, &context, request_id, error).await);
    }
    Ok((request_id, context))
}

async fn require_object_governance_write(
    state: &ImportState,
    headers: &HeaderMap,
) -> Result<(Uuid, auth::AuthContext), ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    if let Err(error) = auth::ensure_allowed_origin(&state.auth.config, headers) {
        return Err(audit_write_denial(state, &context, request_id, error).await);
    }
    if let Err(error) = auth::ensure_csrf(&state.auth, headers).await {
        return Err(audit_write_denial(state, &context, request_id, error).await);
    }
    if let Err(error) = context.require_permission(Permission::GovernObjects) {
        return Err(audit_write_denial(state, &context, request_id, error).await);
    }
    Ok((request_id, context))
}

async fn audit_write_denial(
    state: &ImportState,
    context: &auth::AuthContext,
    request_id: Uuid,
    error: AuthError,
) -> ImportApiError {
    let error_code = error.code();
    match database::imports::record_import_audit(
        &state.auth.pool,
        context.workspace_id(),
        context.user_id(),
        request_id,
        None,
        "import.write",
        "denied",
        error_code,
    )
    .await
    {
        Ok(()) => ImportApiError::auth(error, request_id),
        Err(_) => ImportApiError::internal(request_id),
    }
}

async fn audit_import_failure(
    state: &ImportState,
    context: &auth::AuthContext,
    request_id: Uuid,
    import_id: Uuid,
    event_type: &str,
    error: ImportApiError,
) -> ImportApiError {
    match database::imports::record_import_audit(
        &state.auth.pool,
        context.workspace_id(),
        context.user_id(),
        request_id,
        Some(import_id),
        event_type,
        "failure",
        error.code,
    )
    .await
    {
        Ok(()) => error,
        Err(_) => ImportApiError::internal(request_id),
    }
}

async fn load_import_object(
    state: &ImportState,
    workspace_id: Uuid,
    import_id: Uuid,
    request_id: Uuid,
) -> Result<ImportUploadRecord, ImportApiError> {
    database::imports::get_import(&state.auth.pool, workspace_id, import_id)
        .await
        .map_err(|error| map_repository_error(error, request_id))
}

async fn audit_upload_denial(
    state: &ImportState,
    context: &auth::AuthContext,
    request_id: Uuid,
    error: AuthError,
) -> ImportApiError {
    let error_code = error.code();
    if database::imports::record_upload_denied(
        &state.auth.pool,
        context.workspace_id(),
        context.user_id(),
        request_id,
        error_code,
    )
    .await
    .is_err()
    {
        ImportApiError::internal(request_id)
    } else {
        ImportApiError::auth(error, request_id)
    }
}

fn digest(parts: &[&str]) -> String {
    let mut hasher = Sha256::new();
    for part in parts {
        hasher.update((part.len() as u64).to_be_bytes());
        hasher.update(part.as_bytes());
    }
    format!("{:x}", hasher.finalize())
}

#[cfg(test)]
mod phase_3d_compensation_contract {
    use super::{Uuid, map_object_governance_error};
    use database::object_governance::ObjectGovernanceError;
    use std::{convert::Infallible, time::Duration};

    const SOURCE: &str = include_str!("imports.rs");

    #[test]
    fn committed_compensation_objects_are_never_physically_deleted() {
        let compensation = SOURCE
            .split("pub async fn create_compensation")
            .nth(1)
            .expect("compensation handler")
            .split("pub async fn lineage")
            .next()
            .expect("compensation handler end");
        assert!(compensation.contains(".commit()"));
        assert!(!compensation.contains(".delete(&stored.object_key)"));
    }

    #[test]
    fn compensation_decides_visibility_and_idempotency_before_commit() {
        let compensation = SOURCE
            .split("pub async fn create_compensation")
            .nth(1)
            .expect("compensation handler")
            .split("pub async fn lineage")
            .next()
            .expect("compensation handler end");
        let prepare = compensation
            .find("prepare_compensation_upload")
            .expect("database preparation");
        let commit = compensation.find(".commit()").expect("object commit");
        assert!(prepare < commit);
        assert!(compensation.contains("CompensationUploadPreparation::Replay"));
        assert!(compensation.matches("object_upload.abort().await").count() >= 3);
    }

    #[test]
    fn compensation_reuses_the_full_import_entry_state() {
        let compensation = SOURCE
            .split("pub async fn create_compensation")
            .nth(1)
            .expect("compensation handler")
            .split("pub async fn lineage")
            .next()
            .expect("compensation handler end");
        assert!(compensation.contains("UploadValidator::new"));
        assert!(compensation.contains("begin_upload"));
        assert!(compensation.contains("NewCompensationUpload"));
    }

    #[test]
    fn ordinary_upload_never_deletes_a_committed_object() {
        let upload = SOURCE
            .split("pub async fn upload")
            .nth(1)
            .expect("upload handler")
            .split("pub async fn create_compensation")
            .next()
            .expect("upload handler end");
        assert!(upload.contains(".commit()"));
        assert!(!upload.contains(".delete("));
        assert!(upload.contains(".abort()"));
    }

    #[test]
    fn object_governance_errors_have_dedicated_stable_codes() {
        let request_id = Uuid::now_v7();
        let not_found = map_object_governance_error(ObjectGovernanceError::NotFound, request_id);
        assert_eq!(not_found.code, "object_consistency_not_found");
        let internal =
            map_object_governance_error(ObjectGovernanceError::InvalidStoredState, request_id);
        assert_eq!(internal.code, "object_consistency_error");
    }

    #[test]
    fn every_import_and_rollback_terminal_event_closes_sse() {
        for event_type in [
            "succeeded",
            "failed",
            "dead_letter",
            "rolled_back",
            "rollback_conflict",
            "rollback_failed",
        ] {
            assert!(super::is_terminal_event_type(event_type));
        }
        for event_type in [
            "queued",
            "running",
            "progress",
            "rollback_queued",
            "rollback_running",
        ] {
            assert!(!super::is_terminal_event_type(event_type));
        }
    }

    #[test]
    fn event_visibility_and_cursor_errors_use_stable_codes() {
        let request_id = Uuid::now_v7();
        let invisible = super::map_event_queue_error(
            database::job_queue::JobQueueError::EventNotVisible,
            request_id,
        );
        assert_eq!(invisible.code, "event_not_visible");
        let invalid = super::map_event_queue_error(
            database::job_queue::JobQueueError::EventIdInvalid,
            request_id,
        );
        assert_eq!(invalid.code, "event_id_invalid");
    }

    #[tokio::test]
    async fn slow_sse_sender_yields_to_periodic_revalidation() {
        let (sender, mut receiver) =
            tokio::sync::mpsc::channel::<Result<axum::response::sse::Event, Infallible>>(1);
        sender
            .send(Ok(axum::response::sse::Event::default().data("backlog")))
            .await
            .unwrap();
        let mut revalidate = tokio::time::interval(Duration::from_millis(5));
        revalidate.tick().await;
        let outcome = tokio::time::timeout(
            Duration::from_millis(100),
            super::send_frame_or_revalidate(
                &sender,
                &mut revalidate,
                axum::response::sse::Event::default().data("next"),
            ),
        )
        .await
        .expect("revalidation must not be blocked by a full client channel");
        assert!(matches!(outcome, super::FrameWait::Revalidate));
        assert!(receiver.try_recv().is_ok(), "the pending frame is retained");
    }

    #[test]
    fn established_stream_revalidates_every_visibility_boundary_and_audits_termination() {
        let events = SOURCE
            .split("pub async fn events")
            .nth(1)
            .unwrap()
            .split("#[utoipa::path(\n    get,\n    path = \"/api/v1/imports/{import_id}/errors\"")
            .next()
            .unwrap();
        for boundary in [
            "current_context",
            "session_id",
            "user_id",
            "workspace_id",
            "Permission::ReadImports",
            "get_import",
            "import.events_access_terminated",
        ] {
            assert!(events.contains(boundary), "missing SSE boundary {boundary}");
        }
        for forbidden in ["cookie", "token", "csrf", "idempotency_key", "record_data"] {
            assert!(
                !events.contains(&format!("\"{forbidden}\"")),
                "SSE audit must not persist {forbidden}"
            );
        }
    }
}
