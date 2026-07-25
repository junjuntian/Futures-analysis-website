use crate::auth::{self, AuthError, AuthState, Permission};
use application::imports::{ImportParseError, UploadValidationError, UploadValidator};
use axum::{
    Json,
    extract::{Multipart, Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use common::ApiResponse;
use database::imports::{
    ImportRepositoryError, ImportUploadRecord, InspectionUpdate, NewImportUpload,
    PreviewInputSnapshot, PreviewSave,
};
use domain::import::{
    ImportBatchStatus, ImportDatasetDefinition, ImportErrorsResponse, ImportInspectRequest,
    ImportInspectResponse, ImportMappingRequest, ImportMappingResponse, ImportPreviewRequest,
    ImportTemplateCreateRequest, ImportTemplateSummary, ImportTemplateVersionResponse,
    import_dataset_definitions,
};
use infrastructure::object_storage::{ObjectStorage, ObjectStorageError};
use serde::Serialize;
use std::sync::Arc;
use time::OffsetDateTime;
use utoipa::ToSchema;
use uuid::Uuid;

#[derive(Clone)]
pub struct ImportState {
    pub auth: Arc<AuthState>,
    pub storage: Arc<infrastructure::object_storage::LocalObjectStorage>,
    pub upload_policy: application::imports::UploadPolicy,
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
    get,
    path = "/api/v1/imports/{import_id}/errors",
    params(("import_id" = Uuid, Path, description = "Import batch id")),
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
    headers: HeaderMap,
) -> Result<Response, ImportApiError> {
    let request_id = Uuid::now_v7();
    let context = auth::current_context(&state.auth, &headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    context
        .require_permission(Permission::ImportRead)
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    let items = database::imports::list_errors(&state.auth.pool, context.workspace_id(), import_id)
        .await
        .map_err(|error| map_repository_error(error, request_id))?;
    Ok(Json(ApiResponse::new(
        ImportErrorsResponse { import_id, items },
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
    auth::ensure_allowed_origin(&state.auth.config, headers)
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    auth::ensure_csrf(&state.auth, headers)
        .await
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    context
        .require_permission(Permission::ImportUpload)
        .map_err(|error| ImportApiError::auth(error, request_id))?;
    Ok((request_id, context))
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
