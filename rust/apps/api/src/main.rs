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
        imports::ImportUploadRequest,
        imports::ImportFileResponse,
        imports::ImportResponse,
        imports::ImportTemplatesResponse,
        imports::ImportDatasetsResponse,
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
        domain::import::ImportErrorsResponse
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
    });

    let app = router(state, import_state);
    let listener = tokio::net::TcpListener::bind(config.bind_addr).await?;
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
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
        .route("/api/v1/imports/{import_id}/errors", get(imports::errors))
        .route(
            "/api/v1/import-templates",
            get(imports::list_templates).post(imports::create_template),
        )
        .route("/api/v1/import-datasets", get(imports::list_datasets))
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
