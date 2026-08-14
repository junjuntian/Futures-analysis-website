mod auth;
mod spread_analytics;
mod spread_warm;

use application::{HealthStatus, VersionInfo};
use auth::{AuthConfig, AuthState, LoginLimiter};
use axum::{
    Json, Router,
    extract::State,
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
        auth::change_password,
        auth::sessions,
        auth::revoke_session,
        spread_analytics::list_varieties,
        spread_analytics::list_months,
        spread_analytics::list_own_varieties,
        spread_analytics::list_own_months,
        spread_analytics::query_free_spread,
        spread_analytics::query_seat_positions,
        spread_analytics::query_seat_building,
        spread_analytics::query_spread_monitor,
        spread_analytics::query_data_health,
        spread_analytics::query_overview_report,
        spread_analytics::save_overview_report_levels,
        spread_analytics::save_overview_report_seat_groups,
        spread_analytics::list_favorites,
        spread_analytics::create_favorite,
        spread_analytics::delete_favorite
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
        auth::ChangePasswordRequest,
        auth::SessionSummary,
        auth::SessionsQuery,
        auth::ErrorBody,
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
        ,spread_analytics::SpreadErrorBody
        ,spread_analytics::SourceMetadata
        ,spread_analytics::VarietiesResponse
        ,spread_analytics::MonthsResponse
        ,spread_analytics::FreeSpreadLeg
        ,spread_analytics::FreeSpreadQueryRequest
        ,spread_analytics::FreeSpreadQueryResponse
        ,spread_analytics::FreeSpreadQueryEcho
        ,spread_analytics::AlgorithmVersions
        ,spread_analytics::ContinuousSeriesResponse
        ,spread_analytics::AnalysisTrace
        ,spread_analytics::SeasonalSeriesResponse
        ,spread_analytics::MonthlyMatrixResponse
        ,spread_analytics::CreateFavoriteRequest
        ,spread_analytics::FavoriteResponse
        ,application::spread_analytics::ProviderVariety
        ,application::spread_analytics::ProviderResultKind
        ,domain::spread_analytics::ContinuousPoint
        ,domain::spread_analytics::SegmentBoundary
        ,domain::spread_analytics::WindowSegment
        ,domain::spread_analytics::WindowQuality
        ,domain::spread_analytics::SeasonalSeries
        ,domain::spread_analytics::SeasonalYearSeries
        ,domain::spread_analytics::MonthlyMatrix
        ,domain::spread_analytics::MonthlyYearRow
        ,domain::spread_analytics::MonthlyCell
        ,domain::spread_analytics::MonthlyUpRatio
        ,spread_analytics::SeatPositionsResponse
        ,spread_analytics::SeatPositionItem
        ,spread_analytics::SeatBuildingResponse
        ,spread_analytics::BuildingDayItem
        ,spread_analytics::VarietyLegs
        ,spread_analytics::OverviewReportResponse
        ,spread_analytics::ReportSeatRow
        ,spread_analytics::ReportSeatCell
        ,spread_analytics::ReportSeatGroup
        ,spread_analytics::SaveReportLevelsRequest
        ,spread_analytics::SaveReportSeatGroupsRequest
        ,spread_analytics::DataHealthDay
        ,spread_analytics::DataHealthResponse
        ,spread_analytics::SpreadMonitorResponse
        ,spread_analytics::SpreadMonitorItem
        ,spread_analytics::SpreadMonitorTrack
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
    if std::env::args().any(|arg| arg == "--provision-collector-account") {
        auth::provision_collector_account(&pool, &auth_config).await?;
        return Ok(());
    }
    let state = Arc::new(AuthState {
        pool,
        version: VersionInfo::new(config.app_name.clone(), config.app_version.clone()),
        config: auth_config,
        limiter: Arc::new(LoginLimiter::default()),
    });
    let spread_state = Arc::new(spread_analytics::SpreadAnalyticsState {
        auth: state.clone(),
        provider: Arc::new(infrastructure::sanhe_spread::SanheSpreadSeriesProvider::new()),
    });
    if std::env::args().any(|arg| arg == "--warm-spread-cache") {
        let summary = spread_warm::warm_spread_cache(spread_state).await?;
        // A run that could not refresh anything it tried is a failed run: the
        // exit code is what a scheduler sees.
        if summary.attempted > 0 && summary.succeeded == 0 {
            anyhow::bail!("every warm attempt failed");
        }
        return Ok(());
    }

    let app = router(state, spread_state);
    let listener = tokio::net::TcpListener::bind(config.bind_addr).await?;
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown_signal())
        .await?;
    Ok(())
}

fn router(
    state: Arc<AuthState>,
    spread_state: Arc<spread_analytics::SpreadAnalyticsState>,
) -> Router {
    let auth_routes = Router::new()
        .route("/api/v1/auth/bootstrap", post(auth::bootstrap))
        .route("/api/v1/auth/login", post(auth::login))
        .route("/api/v1/auth/logout", post(auth::logout))
        .route("/api/v1/auth/me", get(auth::me))
        .route("/api/v1/auth/csrf", get(auth::csrf))
        .route("/api/v1/auth/password", post(auth::change_password))
        .route("/api/v1/workspace", get(auth::workspace))
        .route("/api/v1/sessions", get(auth::sessions))
        .route(
            "/api/v1/sessions/{session_id}",
            delete(auth::revoke_session),
        )
        .with_state(state.clone());
    let spread_routes = Router::new()
        .route(
            "/api/v1/spread-analytics/providers/sanhe/varieties",
            get(spread_analytics::list_varieties),
        )
        .route(
            "/api/v1/spread-analytics/providers/sanhe/varieties/{variety}/months",
            get(spread_analytics::list_months),
        )
        .route(
            "/api/v1/spread-analytics/providers/self/varieties",
            get(spread_analytics::list_own_varieties),
        )
        .route(
            "/api/v1/spread-analytics/providers/self/varieties/{variety}/months",
            get(spread_analytics::list_own_months),
        )
        .route(
            "/api/v1/spread-analytics/free-spread/query",
            post(spread_analytics::query_free_spread),
        )
        .route(
            "/api/v1/spread-analytics/seats/positions",
            get(spread_analytics::query_seat_positions),
        )
        .route(
            "/api/v1/spread-analytics/seats/building",
            get(spread_analytics::query_seat_building),
        )
        .route(
            "/api/v1/spread-analytics/monitor",
            get(spread_analytics::query_spread_monitor),
        )
        .route(
            "/api/v1/overview/data-health",
            get(spread_analytics::query_data_health),
        )
        .route(
            "/api/v1/overview/report",
            get(spread_analytics::query_overview_report),
        )
        .route(
            "/api/v1/overview/report/levels",
            put(spread_analytics::save_overview_report_levels),
        )
        .route(
            "/api/v1/overview/report/seat-groups",
            put(spread_analytics::save_overview_report_seat_groups),
        )
        .route(
            "/api/v1/spread-analytics/favorites",
            get(spread_analytics::list_favorites).post(spread_analytics::create_favorite),
        )
        .route(
            "/api/v1/spread-analytics/favorites/{favorite_id}",
            delete(spread_analytics::delete_favorite),
        )
        .with_state(spread_state);

    Router::new()
        .route("/api/v1/health/live", get(live))
        .route("/api/v1/health/ready", get(ready))
        .route("/api/v1/version", get(version))
        .route("/api-docs/openapi.json", get(openapi))
        .merge(auth_routes)
        .merge(spread_routes)
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
    fn openapi_exposes_only_the_phase_5a_spread_surface() {
        let document = serde_json::to_value(ApiDoc::openapi()).expect("serialize OpenAPI");
        let paths = document["paths"].as_object().expect("OpenAPI paths");
        for (path, method) in [
            ("/api/v1/spread-analytics/providers/sanhe/varieties", "get"),
            (
                "/api/v1/spread-analytics/providers/sanhe/varieties/{variety}/months",
                "get",
            ),
            ("/api/v1/spread-analytics/providers/self/varieties", "get"),
            (
                "/api/v1/spread-analytics/providers/self/varieties/{variety}/months",
                "get",
            ),
            ("/api/v1/spread-analytics/free-spread/query", "post"),
            ("/api/v1/spread-analytics/seats/positions", "get"),
            ("/api/v1/spread-analytics/seats/building", "get"),
            ("/api/v1/spread-analytics/monitor", "get"),
            ("/api/v1/spread-analytics/favorites", "get"),
            ("/api/v1/spread-analytics/favorites", "post"),
            ("/api/v1/spread-analytics/favorites/{favorite_id}", "delete"),
            ("/api/v1/overview/report/levels", "put"),
            ("/api/v1/overview/report/seat-groups", "put"),
        ] {
            let operation = &paths[path][method];
            assert_eq!(
                operation["security"][0]["session_cookie"],
                serde_json::json!([])
            );
            assert!(operation["responses"]["401"].is_object());
            assert!(operation["responses"]["403"].is_object());
        }

        // 首页的数据健康端点。它只读、没有 CSRF 分支，所以不断言 403；
        // 但「必须登录」这条与上面同等刚性——一个不需要登录就能读的端点，
        // 会把这个 workspace 有哪些交易所、数据到哪天全部说出去。
        let data_health = &paths["/api/v1/overview/data-health"]["get"];
        assert_eq!(
            data_health["security"][0]["session_cookie"],
            serde_json::json!([])
        );
        assert!(data_health["responses"]["401"].is_object());

        // 改密码：登录 + CSRF + Origin 三道都要在契约里写明。
        let password = &paths["/api/v1/auth/password"]["post"];
        assert_eq!(
            password["security"][0]["session_cookie"],
            serde_json::json!([])
        );
        let parameters = password["parameters"].as_array().expect("parameters");
        for header in ["x-csrf-token", "Origin"] {
            assert!(
                parameters
                    .iter()
                    .any(|parameter| parameter["name"] == header),
                "改密码必须声明 {header}"
            );
        }
        assert!(password["responses"]["403"].is_object());
        assert!(!paths.keys().any(|path| path.contains("broker")));
        assert!(!paths.keys().any(|path| path.contains("backtest")));
        // monitor 曾经也在这份「还不该存在」的名单里——那是 Phase 5A 时期的守卫，
        // 用来挡住把 5B 的表面提前暴露出去。5B 现在正式开工，端点已立项并注册，
        // 守卫随之退场。broker 与 backtest 仍未立项，继续挡着。

        let query = &paths["/api/v1/spread-analytics/free-spread/query"]["post"];
        let parameters = query["parameters"].as_array().expect("query headers");
        for header in ["x-csrf-token", "Origin"] {
            assert!(
                parameters
                    .iter()
                    .any(|parameter| parameter["name"] == header)
            );
        }
        for schema in [
            "AnalysisTrace",
            "ContinuousSeriesResponse",
            "SeasonalSeriesResponse",
            "MonthlyMatrixResponse",
        ] {
            assert!(document["components"]["schemas"][schema].is_object());
        }
    }
}
