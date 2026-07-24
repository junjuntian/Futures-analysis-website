use application::{HealthStatus, VersionInfo};
use axum::{Json, Router, extract::State, http::StatusCode, response::IntoResponse, routing::get};
use common::{ApiResponse, AppConfig};
use sqlx::PgPool;
use std::sync::Arc;
use tower::ServiceBuilder;
use tower_http::{ServiceBuilderExt, request_id::MakeRequestUuid, trace::TraceLayer};
use tracing::info;
use utoipa::OpenApi;
use uuid::Uuid;

#[derive(Clone)]
struct AppState {
    pool: PgPool,
    version: VersionInfo,
}

#[derive(OpenApi)]
#[openapi(
    paths(live, ready, version, openapi),
    components(schemas(HealthStatus, VersionInfo))
)]
struct ApiDoc;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    if std::env::args().any(|arg| arg == "--healthcheck") {
        return Ok(());
    }

    infrastructure::init_tracing();
    let config = AppConfig::from_env(8080)?;
    info!(
        bind_addr = %config.bind_addr,
        database_url = config.redacted_database_url(),
        "starting api"
    );

    let pool = database::connect(&config.database_url).await?;
    let state = Arc::new(AppState {
        pool,
        version: VersionInfo::new(config.app_name.clone(), config.app_version.clone()),
    });

    let app = router(state);
    let listener = tokio::net::TcpListener::bind(config.bind_addr).await?;
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

fn router(state: Arc<AppState>) -> Router {
    Router::new()
        .route("/api/v1/health/live", get(live))
        .route("/api/v1/health/ready", get(ready))
        .route("/api/v1/version", get(version))
        .route("/api-docs/openapi.json", get(openapi))
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
async fn ready(State(state): State<Arc<AppState>>) -> impl IntoResponse {
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
async fn version(State(state): State<Arc<AppState>>) -> impl IntoResponse {
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
