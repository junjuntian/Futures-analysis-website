use crate::auth::{self, AuthError, AuthState, Permission};
use application::imports::{UploadValidationError, UploadValidator};
use axum::{
    Json,
    extract::{Multipart, Path, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use common::ApiResponse;
use database::imports::{ImportRepositoryError, ImportUploadRecord, NewImportUpload};
use domain::import::ImportBatchStatus;
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
        content = String,
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
