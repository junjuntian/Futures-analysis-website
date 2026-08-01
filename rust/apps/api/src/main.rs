mod auth;
mod imports;

use application::{HealthStatus, VersionInfo};
use auth::{AuthConfig, AuthState, LoginLimiter};
use axum::{
    Json, Router,
    extract::{DefaultBodyLimit, State},
    http::StatusCode,
    response::IntoResponse,
    routing::{delete, get, post, put},
};
use common::{ApiResponse, AppConfig};
use std::sync::Arc;
use tower::ServiceBuilder;
use tower_http::{ServiceBuilderExt, request_id::MakeRequestUuid, trace::TraceLayer};
use tracing::info;
use utoipa::{
    OpenApi,
    openapi::security::{ApiKey, ApiKeyValue, SecurityScheme},
};
use uuid::Uuid;

#[derive(OpenApi)]
#[openapi(
    paths(
        live,
        ready,
        version,
        openapi,
        auth::bootstrap,
        auth::login,
        auth::logout,
        auth::me,
        auth::csrf,
        auth::workspace,
        auth::sessions,
        auth::revoke_session,
        imports::upload,
        imports::get,
        imports::inspect,
        imports::save_mapping,
        imports::preview,
        imports::validate,
        imports::confirm,
        imports::rollback_check,
        imports::rollback_conflicts,
        imports::rollback,
        imports::create_compensation,
        imports::lineage,
        imports::queue_object_scan,
        imports::object_scan_report,
        imports::queue_object_quarantine,
        imports::events,
        imports::errors,
        imports::list_templates,
        imports::list_datasets,
        imports::create_template
    ),
    components(schemas(
        HealthStatus,
        VersionInfo,
        auth::BootstrapRequest,
        auth::LoginRequest,
        auth::UserSummary,
        auth::WorkspaceSummary,
        auth::MeResponse,
        auth::CsrfResponse,
        auth::SessionSummary,
        auth::SessionsQuery,
        auth::ErrorBody,
        imports::ImportErrorBody,
        imports::ImportEventStreamErrorCode,
        imports::ImportEventStreamErrorBody,
        imports::ImportSseEventFrame,
        imports::ImportQueuedEventFrame,
        imports::ImportQueuedEventType,
        imports::ImportRunningEventFrame,
        imports::ImportRunningEventType,
        imports::ImportProgressEventFrame,
        imports::ImportProgressEventType,
        imports::ImportSucceededEventFrame,
        imports::ImportSucceededEventType,
        imports::ImportFailedEventFrame,
        imports::ImportFailedEventType,
        imports::ImportDeadLetterEventFrame,
        imports::ImportDeadLetterEventType,
        imports::ImportRollbackQueuedEventFrame,
        imports::ImportRollbackQueuedEventType,
        imports::ImportRollbackRunningEventFrame,
        imports::ImportRollbackRunningEventType,
        imports::ImportRollbackConflictEventFrame,
        imports::ImportRollbackConflictEventType,
        imports::ImportRolledBackEventFrame,
        imports::ImportRolledBackEventType,
        imports::ImportRollbackFailedEventFrame,
        imports::ImportRollbackFailedEventType,
        imports::ImportRollbackConflictApiResponse,
        imports::ImportUploadRequest,
        imports::ImportCompensationUploadRequest,
        imports::ImportFileResponse,
        imports::ImportResponse,
        imports::ImportTemplatesResponse,
        imports::ImportDatasetsResponse,
        imports::ImportValidateApiResponse,
        imports::ImportConfirmApiResponse,
        imports::ImportJobApiResponse,
        domain::import::ImportBatchStatus,
        domain::import::ImportErrorSeverity,
        domain::import::ImportInspectRequest,
        domain::import::ImportPreviewRequest,
        domain::import::ImportDetection,
        domain::import::ImportSheetInfo,
        domain::import::ImportColumnPreview,
        domain::import::ImportPreviewCell,
        domain::import::ImportPreviewRow,
        domain::import::ImportErrorPreview,
        domain::import::ImportInspectResponse,
        domain::import::ImportMappingField,
        domain::import::ImportDatasetDefinition,
        domain::import::ImportDatasetFieldDefinition,
        domain::import::ImportMappingRequest,
        domain::import::ImportMappingResponse,
        domain::import::ImportTemplateCreateRequest,
        domain::import::ImportTemplateSummary,
        domain::import::ImportTemplateVersionResponse,
        domain::import::ImportErrorsResponse,
        domain::import::ImportConflictPolicy,
        domain::import::ImportJobStatus,
        domain::import::ImportJobEventType,
        domain::import::ImportWorkflowErrorCode,
        domain::import::ImportValidationSummary,
        domain::import::ImportValidateResponse,
        domain::import::ImportConfirmRequest,
        domain::import::ImportConfirmResponse,
        domain::import::RollbackCapability,
        domain::import::ImportRollbackRequestStatus,
        domain::import::ImportRollbackConflictType,
        domain::import::ImportRollbackRequest,
        domain::import::ImportRollbackConflict,
        domain::import::ImportRollbackCheckResponse,
        domain::import::ImportRollbackConflictsResponse,
        domain::import::ImportRollbackResponse,
        domain::import::ImportCompensationFile,
        domain::import::ImportCompensationResponse,
        domain::import::ImportLineageFile,
        domain::import::ImportLineageJob,
        domain::import::ImportLineageRollback,
        domain::import::ImportLineageNode,
        domain::import::ImportLineageAudit,
        domain::import::ImportLineageResponse,
        domain::import::ImportProgress,
        domain::import::ImportJobSummary,
        domain::import::ImportJobEvent
        ,domain::object_governance::ObjectConsistencyRun
        ,domain::object_governance::ObjectConsistencyFinding
        ,domain::object_governance::ObjectConsistencyReport
        ,domain::object_governance::ObjectQuarantineResponse
    )),
    modifiers(&SecurityAddon)
)]
struct ApiDoc;

struct SecurityAddon;

impl utoipa::Modify for SecurityAddon {
    fn modify(&self, openapi: &mut utoipa::openapi::OpenApi) {
        if let Some(components) = openapi.components.as_mut() {
            components.add_security_scheme(
                "session_cookie",
                SecurityScheme::ApiKey(ApiKey::Cookie(ApiKeyValue::new("futures_session"))),
            );
        }
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    if std::env::args().any(|arg| arg == "--healthcheck") {
        return Ok(());
    }

    infrastructure::init_tracing();
    let config = AppConfig::from_env(8080)?;
    let auth_config = AuthConfig::from_env()?;
    info!(
        bind_addr = %config.bind_addr,
        database_url = config.redacted_database_url(),
        "starting api"
    );

    let pool = database::connect(&config.database_url).await?;
    let upload_policy = application::imports::UploadPolicy::from_env()
        .map_err(|_| anyhow::anyhow!("invalid IMPORT_MAX_BYTES"))?;
    let storage_root =
        std::env::var("OBJECT_STORAGE_ROOT").unwrap_or_else(|_| "./data/object-storage".into());
    let storage =
        Arc::new(infrastructure::object_storage::LocalObjectStorage::new(storage_root).await?);
    let state = Arc::new(AuthState {
        pool,
        version: VersionInfo::new(config.app_name.clone(), config.app_version.clone()),
        config: auth_config,
        limiter: Arc::new(LoginLimiter::default()),
    });
    let import_state = Arc::new(imports::ImportState {
        auth: state.clone(),
        storage,
        upload_policy,
        idempotency_pepper: load_idempotency_pepper().await?,
        sse_revalidate_seconds: load_sse_revalidate_seconds()?,
    });

    let app = router(state, import_state);
    let listener = tokio::net::TcpListener::bind(config.bind_addr).await?;
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

fn load_sse_revalidate_seconds() -> anyhow::Result<u64> {
    let seconds = std::env::var("IMPORT_SSE_REVALIDATE_SECONDS")
        .map(|value| value.parse::<u64>())
        .unwrap_or(Ok(15))
        .map_err(|_| anyhow::anyhow!("IMPORT_SSE_REVALIDATE_SECONDS must be an integer"))?;
    validate_sse_revalidate_seconds(seconds)?;
    Ok(seconds)
}

fn validate_sse_revalidate_seconds(seconds: u64) -> anyhow::Result<()> {
    if !(1..=60).contains(&seconds) {
        anyhow::bail!("IMPORT_SSE_REVALIDATE_SECONDS must be between 1 and 60");
    }
    Ok(())
}

async fn load_idempotency_pepper() -> anyhow::Result<String> {
    let path = std::env::var("IMPORT_IDEMPOTENCY_PEPPER_FILE")
        .unwrap_or_else(|_| "/run/secrets/import-idempotency-pepper".to_string());
    match tokio::fs::read_to_string(&path).await {
        Ok(value) => {
            let value = value.trim();
            if value.len() < 32 {
                anyhow::bail!("IMPORT_IDEMPOTENCY_PEPPER_FILE must contain at least 32 characters");
            }
            Ok(value.to_string())
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            let production = std::env::var("APP_ENV")
                .is_ok_and(|value| value.eq_ignore_ascii_case("production"));
            if production {
                anyhow::bail!("IMPORT_IDEMPOTENCY_PEPPER_FILE is required in production");
            }
            let value = std::env::var("IMPORT_IDEMPOTENCY_PEPPER").map_err(|_| {
                anyhow::anyhow!("development requires explicit IMPORT_IDEMPOTENCY_PEPPER")
            })?;
            if value.len() < 32 {
                anyhow::bail!("IMPORT_IDEMPOTENCY_PEPPER must contain at least 32 characters");
            }
            Ok(value)
        }
        Err(error) => Err(error.into()),
    }
}

fn router(state: Arc<AuthState>, import_state: Arc<imports::ImportState>) -> Router {
    let auth_routes = Router::new()
        .route("/api/v1/auth/bootstrap", post(auth::bootstrap))
        .route("/api/v1/auth/login", post(auth::login))
        .route("/api/v1/auth/logout", post(auth::logout))
        .route("/api/v1/auth/me", get(auth::me))
        .route("/api/v1/auth/csrf", get(auth::csrf))
        .route("/api/v1/workspace", get(auth::workspace))
        .route("/api/v1/sessions", get(auth::sessions))
        .route(
            "/api/v1/sessions/{session_id}",
            delete(auth::revoke_session),
        )
        .with_state(state.clone());
    let import_body_limit = usize::try_from(import_state.upload_policy.max_bytes)
        .unwrap_or(usize::MAX)
        .saturating_add(1024 * 1024);
    let import_routes = Router::new()
        .route("/api/v1/imports", post(imports::upload))
        .route("/api/v1/imports/{import_id}", get(imports::get))
        .route(
            "/api/v1/imports/{import_id}/inspect",
            post(imports::inspect),
        )
        .route(
            "/api/v1/imports/{import_id}/mapping",
            put(imports::save_mapping),
        )
        .route(
            "/api/v1/imports/{import_id}/preview",
            post(imports::preview),
        )
        .route(
            "/api/v1/imports/{import_id}/validate",
            post(imports::validate),
        )
        .route(
            "/api/v1/imports/{import_id}/confirm",
            post(imports::confirm),
        )
        .route(
            "/api/v1/imports/{import_id}/rollback-check",
            post(imports::rollback_check),
        )
        .route(
            "/api/v1/imports/{import_id}/rollback-conflicts",
            get(imports::rollback_conflicts),
        )
        .route(
            "/api/v1/imports/{import_id}/rollback",
            post(imports::rollback),
        )
        .route(
            "/api/v1/imports/{import_id}/compensations",
            post(imports::create_compensation),
        )
        .route("/api/v1/imports/{import_id}/lineage", get(imports::lineage))
        .route("/api/v1/imports/{import_id}/events", get(imports::events))
        .route("/api/v1/imports/{import_id}/errors", get(imports::errors))
        .route(
            "/api/v1/import-templates",
            get(imports::list_templates).post(imports::create_template),
        )
        .route("/api/v1/import-datasets", get(imports::list_datasets))
        .route(
            "/api/v1/object-consistency/scans",
            post(imports::queue_object_scan),
        )
        .route(
            "/api/v1/object-consistency/scans/{run_id}",
            get(imports::object_scan_report),
        )
        .route(
            "/api/v1/object-consistency/findings/{finding_id}/quarantine",
            post(imports::queue_object_quarantine),
        )
        .layer(DefaultBodyLimit::max(import_body_limit))
        .with_state(import_state);

    Router::new()
        .route("/api/v1/health/live", get(live))
        .route("/api/v1/health/ready", get(ready))
        .route("/api/v1/version", get(version))
        .route("/api-docs/openapi.json", get(openapi))
        .merge(auth_routes)
        .merge(import_routes)
        .with_state(state)
        .layer(
            ServiceBuilder::new()
                .set_x_request_id(MakeRequestUuid)
                .propagate_x_request_id()
                .layer(TraceLayer::new_for_http()),
        )
}

#[utoipa::path(get, path = "/api/v1/health/live", responses((status = 200, body = HealthStatus)))]
async fn live() -> impl IntoResponse {
    Json(ApiResponse::new(HealthStatus::live(), Uuid::now_v7()))
}

#[utoipa::path(get, path = "/api/v1/health/ready", responses((status = 200, body = HealthStatus), (status = 503)))]
async fn ready(State(state): State<Arc<AuthState>>) -> impl IntoResponse {
    let is_ready = database::check_ready(&state.pool).await;
    let status = if is_ready {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    };
    (
        status,
        Json(ApiResponse::new(
            HealthStatus::ready(is_ready),
            Uuid::now_v7(),
        )),
    )
}

#[utoipa::path(get, path = "/api/v1/version", responses((status = 200, body = VersionInfo)))]
async fn version(State(state): State<Arc<AuthState>>) -> impl IntoResponse {
    Json(ApiResponse::new(state.version.clone(), Uuid::now_v7()))
}

#[utoipa::path(get, path = "/api-docs/openapi.json", responses((status = 200)))]
async fn openapi() -> impl IntoResponse {
    Json(ApiDoc::openapi())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };

    #[cfg(unix)]
    let terminate = async {
        if let Ok(mut signal) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            signal.recv().await;
        }
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn openapi_exposes_secured_rollback_precheck_conflicts_and_enqueue() {
        let document = serde_json::to_value(ApiDoc::openapi()).expect("serialize OpenAPI");
        let paths = document["paths"].as_object().expect("OpenAPI paths");
        let check = &paths["/api/v1/imports/{import_id}/rollback-check"]["post"];
        let conflicts = &paths["/api/v1/imports/{import_id}/rollback-conflicts"]["get"];
        let rollback = &paths["/api/v1/imports/{import_id}/rollback"]["post"];
        for operation in [check, conflicts, rollback] {
            assert_eq!(
                operation["security"][0]["session_cookie"],
                serde_json::json!([])
            );
            assert!(operation["responses"]["401"].is_object());
            assert!(operation["responses"]["403"].is_object());
            assert!(operation["responses"]["404"].is_object());
        }
        assert!(
            check["parameters"]
                .as_array()
                .expect("check parameters")
                .iter()
                .any(|parameter| parameter["name"] == "x-csrf-token")
        );
        assert!(
            rollback["parameters"]
                .as_array()
                .expect("rollback parameters")
                .iter()
                .any(|parameter| parameter["name"] == "Idempotency-Key")
        );
        assert!(rollback["requestBody"].is_object());
        assert!(rollback["responses"]["202"].is_object());
        assert!(rollback["responses"]["409"].is_object());
        let rollback_conflict_schema =
            &document["components"]["schemas"]["ImportRollbackConflictApiResponse"];
        let variants = rollback_conflict_schema["oneOf"]
            .as_array()
            .expect("rollback 409 response is oneOf");
        assert_eq!(variants.len(), 2);
        let response_schema_ref =
            rollback["responses"]["409"]["content"]["application/json"]["schema"]["$ref"]
                .as_str()
                .expect("rollback 409 schema ref");
        assert_eq!(
            response_schema_ref,
            "#/components/schemas/ImportRollbackConflictApiResponse"
        );
    }

    #[test]
    fn openapi_exposes_compensation_upload_and_lineage() {
        let document = serde_json::to_value(ApiDoc::openapi()).expect("serialize OpenAPI");
        let paths = document["paths"].as_object().expect("OpenAPI paths");
        let compensation = &paths["/api/v1/imports/{import_id}/compensations"]["post"];
        let lineage = &paths["/api/v1/imports/{import_id}/lineage"]["get"];
        for operation in [compensation, lineage] {
            assert_eq!(
                operation["security"][0]["session_cookie"],
                serde_json::json!([])
            );
            assert!(operation["responses"]["401"].is_object());
            assert!(operation["responses"]["403"].is_object());
            assert!(operation["responses"]["404"].is_object());
        }
        let parameters = compensation["parameters"]
            .as_array()
            .expect("compensation parameters");
        for required_header in ["Idempotency-Key", "x-csrf-token", "Origin"] {
            assert!(
                parameters
                    .iter()
                    .any(|parameter| parameter["name"] == required_header),
                "missing {required_header}"
            );
        }
        assert!(
            compensation["requestBody"]["content"]["multipart/form-data"]["schema"].is_object()
        );
        assert!(compensation["responses"]["200"].is_object());
        assert!(compensation["responses"]["201"].is_object());
        assert!(compensation["responses"]["409"].is_object());
        assert!(lineage["responses"]["200"].is_object());
        assert!(document["components"]["schemas"]["ImportCompensationResponse"].is_object());
        assert!(document["components"]["schemas"]["ImportLineageResponse"].is_object());
    }

    #[test]
    fn openapi_exposes_explicit_object_scan_and_quarantine_without_delete() {
        let document = serde_json::to_value(ApiDoc::openapi()).expect("serialize OpenAPI");
        let paths = document["paths"].as_object().expect("OpenAPI paths");
        let scan = &paths["/api/v1/object-consistency/scans"]["post"];
        let report = &paths["/api/v1/object-consistency/scans/{run_id}"]["get"];
        let quarantine =
            &paths["/api/v1/object-consistency/findings/{finding_id}/quarantine"]["post"];
        for operation in [scan, report, quarantine] {
            assert_eq!(
                operation["security"][0]["session_cookie"],
                serde_json::json!([])
            );
            assert!(operation["responses"]["401"].is_object());
            assert!(operation["responses"]["403"].is_object());
        }
        for operation in [scan, quarantine] {
            let parameters = operation["parameters"].as_array().expect("parameters");
            for header in ["Idempotency-Key", "x-csrf-token", "Origin"] {
                assert!(
                    parameters
                        .iter()
                        .any(|parameter| parameter["name"] == header)
                );
            }
            assert!(operation["responses"]["202"].is_object());
            assert!(operation["responses"]["409"].is_object());
        }
        assert!(
            !paths
                .keys()
                .any(|path| path.contains("delete") || path.contains("purge"))
        );
    }

    #[test]
    fn openapi_snapshots_discriminated_sse_frames_and_stable_errors() {
        let document = serde_json::to_value(ApiDoc::openapi()).expect("serialize OpenAPI");
        let events = &document["paths"]["/api/v1/imports/{import_id}/events"]["get"];
        assert!(
            events["parameters"]
                .as_array()
                .expect("SSE parameters")
                .iter()
                .any(
                    |parameter| parameter["name"] == "Last-Event-ID" && parameter["in"] == "header"
                )
        );
        assert_eq!(
            events["responses"]["200"]["content"]["text/event-stream"]["schema"]["$ref"],
            "#/components/schemas/ImportSseEventFrame"
        );
        let frame = &document["components"]["schemas"]["ImportSseEventFrame"];
        assert_eq!(frame["discriminator"]["propertyName"], "event_type");
        let variants = frame["oneOf"].as_array().expect("SSE oneOf variants");
        assert_eq!(variants.len(), 11);
        let refs = variants
            .iter()
            .filter_map(|variant| variant["$ref"].as_str())
            .collect::<Vec<_>>();
        for expected in [
            "ImportQueuedEventFrame",
            "ImportRunningEventFrame",
            "ImportProgressEventFrame",
            "ImportSucceededEventFrame",
            "ImportFailedEventFrame",
            "ImportDeadLetterEventFrame",
            "ImportRollbackQueuedEventFrame",
            "ImportRollbackRunningEventFrame",
            "ImportRollbackConflictEventFrame",
            "ImportRolledBackEventFrame",
            "ImportRollbackFailedEventFrame",
        ] {
            assert!(
                refs.iter().any(|schema_ref| schema_ref.ends_with(expected)),
                "missing SSE frame {expected}"
            );
        }
        let snapshot = serde_json::json!({
            "400": "event_id_invalid",
            "401": "auth_required",
            "403": "permission_denied",
            "404": "event_not_visible",
            "500": "internal_error",
        });
        let actual = serde_json::json!({
            "400": events["responses"]["400"]["description"],
            "401": events["responses"]["401"]["description"],
            "403": events["responses"]["403"]["description"],
            "404": events["responses"]["404"]["description"],
            "500": events["responses"]["500"]["description"],
        });
        assert_eq!(actual, snapshot);
        let schemas = document["components"]["schemas"].to_string();
        for forbidden in [
            "record_data",
            "cookie",
            "token",
            "csrf",
            "idempotency_key",
            "original_row",
        ] {
            assert!(
                !frame.to_string().contains(forbidden),
                "SSE frame schema leaks {forbidden}"
            );
        }
        assert!(schemas.contains("ImportEventStreamErrorBody"));
    }

    #[test]
    fn sse_revalidation_interval_is_configurable_but_bounded() {
        assert!(validate_sse_revalidate_seconds(1).is_ok());
        assert!(validate_sse_revalidate_seconds(15).is_ok());
        assert!(validate_sse_revalidate_seconds(60).is_ok());
        assert!(validate_sse_revalidate_seconds(0).is_err());
        assert!(validate_sse_revalidate_seconds(61).is_err());
    }
}
