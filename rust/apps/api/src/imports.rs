use crate::auth::{self, AuthError, AuthState, Permission};
use application::imports::{ImportParseError, UploadValidationError, UploadValidator};
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
use database::imports::{
    ImportRepositoryError, ImportUploadRecord, InspectionUpdate, NewImportUpload,
    PreviewInputSnapshot, PreviewSave,
};
use domain::import::{
    ImportBatchStatus, ImportConfirmRequest, ImportConflictPolicy, ImportDatasetDefinition,
    ImportErrorsResponse, ImportInspectRequest, ImportInspectResponse, ImportMappingRequest,
    ImportMappingResponse, ImportPreviewRequest, ImportTemplateCreateRequest,
    ImportTemplateSummary, ImportTemplateVersionResponse, import_dataset_definitions,
};
use infrastructure::object_storage::{ObjectStorage, ObjectStorageError};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::{collections::VecDeque, convert::Infallible, sync::Arc, time::Duration};
use time::OffsetDateTime;
use utoipa::ToSchema;
use uuid::Uuid;

#[derive(Clone)]
pub struct ImportState {
    pub auth: Arc<AuthState>,
    pub storage: Arc<infrastructure::object_storage::LocalObjectStorage>,
    pub upload_policy: application::imports::UploadPolicy,
    pub idempotency_pepper: String,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct ImportErrorBody {
    code: &'static str,
    message: &'static str,
}

#[derive(Debug, ToSchema)]
#[allow(dead_code)]
pub struct ImportUploadRequest {
    #[schema(value_type = String, format = Binary)]
    pub file: Vec<u8>,
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

    fn internal(request_id: Uuid) -> Self {
        Self {
            status: StatusCode::INTERNAL_SERVER_ERROR,
            code: "internal_error",
            message: "internal error",
            request_id,
        }
    }

    fn conflict(code: &'static str, request_id: Uuid) -> Self {
        Self {
            status: StatusCode::CONFLICT,
            code,
            message: "request conflicts with an existing confirmation",
            request_id,
        }
    }
}

#[derive(Debug, Deserialize, utoipa::IntoParams)]
pub struct ImportErrorsQuery {
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
    if let Err(error) = context.require_permission(Permission::ImportUpload) {
        return Err(audit_upload_denial(&state, &context, request_id, error).await);
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
        .begin_upload()
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
        let _ = state.storage.delete(&stored.object_key).await;
        return Err(ImportApiError::internal(request_id));
    }
    let size_bytes = match i64::try_from(stored.size_bytes) {
        Ok(size) => size,
        Err(_) => {
            let _ = state.storage.delete(&stored.object_key).await;
            return Err(ImportApiError::too_large(request_id));
        }
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
                let _ = state.storage.delete(&stored.object_key).await;
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
        .require_permission(Permission::ImportRead)
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
            request.conflict_policy,
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
    get,
    path = "/api/v1/imports/{import_id}/events",
    params(
        ("import_id" = Uuid, Path, description = "Import batch id"),
        ("Last-Event-ID" = Option<String>, Header, description = "Last processed decimal event sequence")
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 200, content_type = "text/event-stream"),
        (status = 400, body = ImportErrorBody),
        (status = 401, body = ImportErrorBody),
        (status = 403, body = ImportErrorBody),
        (status = 404, body = ImportErrorBody)
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
        .require_permission(Permission::ImportRead)
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    database::imports::get_import(&state.auth.pool, context.workspace_id(), import_id)
        .await
        .map_err(|error| map_repository_error(error, request_id))?;
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
    .map_err(|_| ImportApiError::bad_request("event_id_invalid", request_id))?;
    let (sender, receiver) = tokio::sync::mpsc::channel::<Result<Event, Infallible>>(32);
    let pool = state.auth.pool.clone();
    let workspace_id = context.workspace_id();
    tokio::spawn(async move {
        let mut cursor = after;
        let mut pending = VecDeque::from(initial);
        loop {
            if let Some(event) = pending.pop_front() {
                cursor = event.event_seq;
                let terminal = matches!(
                    event.event_type.as_str(),
                    "succeeded" | "failed" | "dead_letter"
                );
                let sse = Event::default()
                    .id(event.event_seq.to_string())
                    .event(event.event_type.clone())
                    .data(serde_json::to_string(&event).unwrap_or_else(|_| "{}".to_string()));
                if sender.send(Ok(sse)).await.is_err() || terminal {
                    break;
                }
                continue;
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
            match database::job_queue::list_events_after(&pool, workspace_id, import_id, cursor)
                .await
            {
                Ok(events) if !events.is_empty() => pending = VecDeque::from(events),
                Ok(_) => {}
                Err(_) => break,
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
        .require_permission(Permission::ImportRead)
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
        .require_permission(Permission::ImportRead)
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
        .require_permission(Permission::ImportRead)
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
        _ => ImportApiError::internal(request_id),
    }
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
    if let Err(error) = context.require_permission(Permission::ImportUpload) {
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
