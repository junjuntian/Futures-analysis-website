use crate::auth::{self, AuthError, AuthState, Permission};
use application::spread_analytics::{
    ProviderContractMonths, ProviderEndpoint, ProviderResultKind, ProviderSeries, ProviderVariety,
    SANHE_PROVIDER_ALGORITHM_VERSION, SANHE_PROVIDER_CODE, SANHE_SOURCE_CODE,
    SANHE_SOURCE_DISPLAY_NAME, SpreadProviderError, SpreadProviderErrorKind, SpreadSeriesProvider,
};
use axum::{
    Json,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode},
    response::{IntoResponse, Response},
};
use common::ApiResponse;
use database::spread_analytics::{
    FavoriteLeg, NewFavorite, NewProviderCache, SeriesPersistence, SpreadRepositoryError,
};
use domain::seat_cost::{DailyPosition, build_cost_series, build_variety_series};
use domain::seat_net_position::{SeatContractDay, build_net_position_series};
use domain::spread_analytics::{
    ContinuousPoint, DEFAULT_RULE_VERSION, RawSpreadPoint, STATISTICS_ALGORITHM_VERSION,
    SegmentBoundary, WINDOW_ALGORITHM_VERSION, WindowQuality, WindowSegment,
    WindowedSpreadAnalytics, calculate_windowed_analytics,
};
use infrastructure::sanhe_spread::SanheSpreadSeriesProvider;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    sync::Arc,
    time::{Duration, Instant},
};
use time::{Date, OffsetDateTime, UtcOffset};
use tracing::warn;
use utoipa::ToSchema;
use uuid::Uuid;

#[derive(Clone)]
pub struct SpreadAnalyticsState {
    pub auth: Arc<AuthState>,
    pub provider: Arc<dyn SpreadSeriesProvider>,
    /// 总览报告表响应缓存,键=(工作区, 交易日),值=(**数据版本**, 已序列化响应)。
    ///
    /// 报告表下半的筹码列每次要对七家席位×金银跑全历史成本推算(实测十几秒,
    /// 这是"与席位页同引擎、不落表"设计的物理成本)。
    ///
    /// **失效由数据变化驱动,不由时间驱动**(2026-08-18 运营者纠正):
    /// 原先 15 分钟 TTL,意味着隔一会儿再打开就必然撞上重算,而空闲时段又在
    /// 反复重算一模一样的东西。现在缓存里存下算这份时的
    /// `report_data_version`(该交易日席位与行情的最近装载时刻),命中判断只比
    /// 版本:**数据没变就一直用这份,不管过了多久**;采集写了新数,版本一变,
    /// 后台预热(3 分钟内)就把它算好,用户命中的仍是算好的。
    ///
    /// 手工改的东西(压力位、席位组)不体现在数据版本里,由写端点保存后
    /// `.clear()` 整体失效兜住。缓存的是 serde_json::Value 而不是响应结构体,
    /// 免去给整棵响应类型树加 Clone。
    pub report_cache: ReportCache,
}

/// 报告表缓存的类型拆名(clippy type-complexity):键=(工作区, 交易日),
/// 值=(算这份时的数据版本, 已序列化响应)。版本见 `report_data_version`。
type ReportCache =
    Arc<tokio::sync::RwLock<HashMap<(Uuid, Date), (Option<OffsetDateTime>, serde_json::Value)>>>;

/// 后台预热的巡检间隔。每次只做一条 `report_data_version` 轻查询,
/// **版本没变就什么都不做**——所以间隔短不等于费 CPU。
const REPORT_WARM_INTERVAL: Duration = Duration::from_secs(180);

/// 报告表后台预热:让「打开就是算好的」成为常态。
///
/// 它做的事很窄:每 3 分钟查一次数据版本,**变了才算**。所以采集落库之后
/// 最多 3 分钟,页面上就是算好的新数;数据没动的时段它一次重算都不做。
///
/// 为什么放在 API 进程里而不是像价差预热那样做成一条 cron:**报告表的缓存在
/// 进程内存里**,外部进程(compose run 起的临时容器)算完写进的是它自己的内存,
/// 对正在服务的这个进程毫无影响。要么把缓存落库,要么就由这个进程自己算——
/// 单人面板选后者,省一张表和一次失效设计。
///
/// 失败只记日志不重试:下一轮巡检自然会再试,而报告表算不出来时页面仍能按
/// 原路径实时算(只是慢),不该因为预热失败就让服务起不来。
pub fn spawn_report_warmer(state: Arc<SpreadAnalyticsState>) {
    tokio::spawn(async move {
        // 启动后先等一会:让迁移、健康检查、首批请求先过去,别一上来就占十几秒 CPU。
        tokio::time::sleep(Duration::from_secs(45)).await;
        loop {
            if let Err(error) = warm_latest_report(&state).await {
                tracing::warn!(%error, "report warm failed; will retry next tick");
            }
            tokio::time::sleep(REPORT_WARM_INTERVAL).await;
        }
    });
}

async fn warm_latest_report(state: &Arc<SpreadAnalyticsState>) -> anyhow::Result<()> {
    // 单人面板只有一个工作区;真有多个也该每个都预热,所以照实遍历。
    let workspaces: Vec<Uuid> = sqlx::query_scalar("select id from workspaces")
        .fetch_all(&state.auth.pool)
        .await?;
    for workspace in workspaces {
        let Some(trade_date) =
            database::spread_analytics::seat_trade_dates(&state.auth.pool, workspace, None, 1)
                .await?
                .into_iter()
                .next()
        else {
            continue; // 这个工作区还没有席位数据,没什么可预热的
        };
        let version = database::spread_analytics::report_data_version(
            &state.auth.pool,
            workspace,
            trade_date,
        )
        .await?;
        let current = state
            .report_cache
            .read()
            .await
            .get(&(workspace, trade_date))
            .is_some_and(|(cached_version, _)| *cached_version == version);
        if current {
            continue; // 数据没动过,这份缓存还是对的
        }
        let started = Instant::now();
        compute_overview_report(state, workspace, trade_date, version, Uuid::now_v7())
            .await
            .map_err(|_| anyhow::anyhow!("compute_overview_report failed"))?;
        tracing::info!(
            %workspace, %trade_date, elapsed_ms = started.elapsed().as_millis(),
            "report cache warmed"
        );
    }
    Ok(())
}

#[derive(Debug, Serialize, ToSchema)]
pub struct SpreadErrorBody {
    pub code: String,
    pub message: &'static str,
    pub retry_after_seconds: Option<u64>,
}

#[derive(Debug)]
pub enum SpreadApiError {
    Validation(&'static str, Uuid),
    Auth(AuthError, Uuid),
    Provider(SpreadProviderErrorKind, Option<u64>, Uuid),
    Conflict(&'static str, Uuid),
    NotFound(&'static str, Uuid),
    Internal(Uuid),
}

impl SpreadApiError {
    fn request_id(&self) -> Uuid {
        match self {
            Self::Validation(_, id)
            | Self::Auth(_, id)
            | Self::Provider(_, _, id)
            | Self::Conflict(_, id)
            | Self::NotFound(_, id)
            | Self::Internal(id) => *id,
        }
    }

    fn code(&self) -> String {
        match self {
            Self::Validation(code, _) | Self::Conflict(code, _) | Self::NotFound(code, _) => {
                (*code).to_string()
            }
            Self::Auth(error, _) => error.code().to_string(),
            Self::Provider(kind, _, _) => kind.stable_code().to_string(),
            Self::Internal(_) => "internal_error".to_string(),
        }
    }

    fn status(&self) -> StatusCode {
        match self {
            Self::Validation(_, _) => StatusCode::BAD_REQUEST,
            Self::Auth(error, _) => error.status(),
            Self::Provider(SpreadProviderErrorKind::ContractChanged, _, _) => {
                StatusCode::BAD_GATEWAY
            }
            Self::Provider(_, _, _) => StatusCode::SERVICE_UNAVAILABLE,
            Self::Conflict(_, _) => StatusCode::CONFLICT,
            Self::NotFound(_, _) => StatusCode::NOT_FOUND,
            Self::Internal(_) => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    fn message(&self) -> &'static str {
        match self {
            Self::Validation(_, _) => "request is invalid",
            Self::Auth(error, _) => error.message(),
            Self::Provider(_, _, _) => "spread data provider is unavailable",
            Self::Conflict(_, _) => "request conflicts with current state",
            Self::NotFound(_, _) => "resource is not visible",
            Self::Internal(_) => "internal error",
        }
    }

    fn retry_after_seconds(&self) -> Option<u64> {
        match self {
            Self::Provider(_, retry_after, _) => *retry_after,
            _ => None,
        }
    }
}

impl IntoResponse for SpreadApiError {
    fn into_response(self) -> Response {
        let request_id = self.request_id();
        let status = self.status();
        let body = SpreadErrorBody {
            code: self.code(),
            message: self.message(),
            retry_after_seconds: self.retry_after_seconds(),
        };
        (status, Json(ApiResponse::new(body, request_id))).into_response()
    }
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct SourceMetadata {
    pub provider: String,
    pub source_code: String,
    pub source_display_name: String,
    pub source_type: String,
    #[serde(with = "time::serde::rfc3339")]
    #[schema(value_type = String, format = DateTime)]
    pub fetched_at: OffsetDateTime,
    #[serde(serialize_with = "domain::spread_analytics::date_serde::option::serialize")]
    #[schema(value_type = Option<String>, format = Date)]
    pub data_cutoff_at: Option<Date>,
    pub price_basis: String,
    pub raw_leg_prices_available: bool,
    pub provider_algorithm_version: String,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct VarietiesResponse {
    pub source: SourceMetadata,
    pub items: Vec<ProviderVariety>,
    pub result_kind: ProviderResultKind,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct MonthsResponse {
    pub source: SourceMetadata,
    pub variety: String,
    pub months: Vec<String>,
    pub basis: Option<i64>,
    pub basis_semantics_confirmed: bool,
    pub result_kind: ProviderResultKind,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct FreeSpreadLeg {
    pub variety: String,
    pub symbol: String,
    pub month: String,
}

#[derive(Debug, Deserialize, ToSchema)]
pub struct FreeSpreadQueryRequest {
    pub provider: String,
    pub leg1: FreeSpreadLeg,
    pub leg2: FreeSpreadLeg,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct AlgorithmVersions {
    pub provider: String,
    pub window: String,
    pub statistics: String,
    pub rule: String,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct ContinuousSeriesResponse {
    pub trace: AnalysisTrace,
    pub points: Vec<ContinuousPoint>,
    pub segment_boundaries: Vec<SegmentBoundary>,
    #[schema(value_type = Option<f64>)]
    pub current_value: Option<Decimal>,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct AnalysisTrace {
    pub provider: String,
    pub source_code: String,
    #[serde(serialize_with = "domain::spread_analytics::date_serde::option::serialize")]
    #[schema(value_type = Option<String>, format = Date)]
    pub data_cutoff_at: Option<Date>,
    pub price_basis: String,
    #[serde(serialize_with = "domain::spread_analytics::date_serde::option::serialize")]
    #[schema(value_type = Option<String>, format = Date)]
    pub sample_start: Option<Date>,
    #[serde(serialize_with = "domain::spread_analytics::date_serde::option::serialize")]
    #[schema(value_type = Option<String>, format = Date)]
    pub sample_end: Option<Date>,
    pub sample_count: u32,
    pub excluded_point_count: u32,
    pub calendar_version_ids: Vec<Uuid>,
    pub window_algorithm_version: String,
    pub statistics_algorithm_version: String,
    pub rule_version: String,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct SeasonalSeriesResponse {
    pub trace: AnalysisTrace,
    pub axis: Vec<String>,
    pub years: Vec<domain::spread_analytics::SeasonalYearSeries>,
    pub current_year: Option<i32>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct MonthlyMatrixResponse {
    pub trace: AnalysisTrace,
    pub years: Vec<domain::spread_analytics::MonthlyYearRow>,
    pub up_ratios: Vec<domain::spread_analytics::MonthlyUpRatio>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct FreeSpreadQueryResponse {
    pub series_id: Uuid,
    pub source: SourceMetadata,
    pub query: FreeSpreadQueryEcho,
    pub quality: WindowQuality,
    pub algorithm_versions: AlgorithmVersions,
    pub continuous_series: ContinuousSeriesResponse,
    pub seasonal_series: SeasonalSeriesResponse,
    pub monthly_matrix: MonthlyMatrixResponse,
    pub segments: Vec<WindowSegment>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct FreeSpreadQueryEcho {
    pub provider: String,
    pub leg1: FreeSpreadLeg,
    pub leg2: FreeSpreadLeg,
}

#[derive(Debug, Deserialize, ToSchema)]
pub struct CreateFavoriteRequest {
    pub name: String,
    pub provider: String,
    pub leg1: FreeSpreadLeg,
    pub leg2: FreeSpreadLeg,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct FavoriteResponse {
    pub id: Uuid,
    pub name: String,
    pub provider: String,
    pub leg1: FreeSpreadLeg,
    pub leg2: FreeSpreadLeg,
    #[serde(with = "time::serde::rfc3339")]
    #[schema(value_type = String, format = DateTime)]
    pub created_at: OffsetDateTime,
}

#[derive(Debug)]
struct CachedFetch<T> {
    data: T,
    fetched_at: OffsetDateTime,
    result_kind: ProviderResultKind,
    payload_hash: String,
}

#[utoipa::path(
    get,
    path = "/api/v1/spread-analytics/providers/sanhe/varieties",
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = VarietiesResponse),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody),
        (status = 502, body = SpreadErrorBody),
        (status = 503, body = SpreadErrorBody)
    )
)]
pub async fn list_varieties(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let _context = read_context(&state, &headers, request_id).await?;
    let fetched = load_varieties(&state, request_id).await?;
    Ok(Json(ApiResponse::new(
        VarietiesResponse {
            source: source_metadata(fetched.fetched_at, None, false),
            items: fetched.data,
            result_kind: fetched.result_kind,
        },
        request_id,
    ))
    .into_response())
}

#[utoipa::path(
    get,
    path = "/api/v1/spread-analytics/providers/sanhe/varieties/{variety}/months",
    params(("variety" = String, Path)),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = MonthsResponse),
        (status = 400, body = SpreadErrorBody),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody),
        (status = 502, body = SpreadErrorBody),
        (status = 503, body = SpreadErrorBody)
    )
)]
pub async fn list_months(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Path(variety): Path<String>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let _context = read_context(&state, &headers, request_id).await?;
    validate_text(&variety, 1, 40).map_err(|code| SpreadApiError::Validation(code, request_id))?;
    let known = load_varieties(&state, request_id).await?.data;
    if !known.iter().any(|item| item.name == variety.trim()) {
        return Err(SpreadApiError::Validation(
            "provider_selection_invalid",
            request_id,
        ));
    }
    let fetched = load_months(&state, variety.trim(), request_id).await?;
    Ok(Json(ApiResponse::new(
        MonthsResponse {
            source: source_metadata(fetched.fetched_at, None, false),
            variety: fetched.data.variety,
            months: fetched.data.months,
            basis: fetched.data.basis,
            basis_semantics_confirmed: false,
            result_kind: fetched.result_kind,
        },
        request_id,
    ))
    .into_response())
}

#[utoipa::path(
    get,
    path = "/api/v1/spread-analytics/providers/self/varieties",
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = VarietiesResponse),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody)
    )
)]
pub async fn list_own_varieties(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = read_context(&state, &headers, request_id).await?;
    let items = database::spread_analytics::own_varieties(&state.auth.pool, context.workspace_id())
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?;
    // 这条读的是我们自己的库，没有上游可以不可用，所以不存在 502/503。
    let result_kind = if items.is_empty() {
        ProviderResultKind::Empty
    } else {
        ProviderResultKind::Ok
    };
    Ok(Json(ApiResponse::new(
        VarietiesResponse {
            source: source_metadata(OffsetDateTime::now_utc(), None, true),
            items: items
                .into_iter()
                .map(|item| ProviderVariety {
                    market: item.market,
                    name: item.name,
                    symbol: item.symbol,
                })
                .collect(),
            result_kind,
        },
        request_id,
    ))
    .into_response())
}

#[utoipa::path(
    get,
    path = "/api/v1/spread-analytics/providers/self/varieties/{variety}/months",
    params(("variety" = String, Path)),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = MonthsResponse),
        (status = 400, body = SpreadErrorBody),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody)
    )
)]
pub async fn list_own_months(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Path(variety): Path<String>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = read_context(&state, &headers, request_id).await?;
    validate_text(&variety, 1, 40).map_err(|code| SpreadApiError::Validation(code, request_id))?;
    let wanted = variety.trim();
    // 路径上给的是中文名，库里按代码存，所以先在自己的品种表里认一遍——
    // 顺带挡掉不认识的名字，免得把任意字符串当成代码去查。
    let known = database::spread_analytics::own_varieties(&state.auth.pool, context.workspace_id())
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?;
    let matched = known
        .into_iter()
        .find(|item| item.name == wanted || item.symbol == wanted.to_ascii_uppercase())
        .ok_or(SpreadApiError::Validation(
            "provider_selection_invalid",
            request_id,
        ))?;
    let months = database::spread_analytics::own_contract_months(
        &state.auth.pool,
        context.workspace_id(),
        &matched.symbol,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    let result_kind = if months.is_empty() {
        ProviderResultKind::Empty
    } else {
        ProviderResultKind::Ok
    };
    Ok(Json(ApiResponse::new(
        MonthsResponse {
            source: source_metadata(OffsetDateTime::now_utc(), None, true),
            variety: matched.name,
            months,
            // 基差是三禾自己算的一个数，我们这条没有对应物，不编一个填进去。
            basis: None,
            basis_semantics_confirmed: false,
            result_kind,
        },
        request_id,
    ))
    .into_response())
}

/// 自研来源下，一条腿是否成立：品种要在我们自己有数据的列表里，
/// 月份要是该品种当前挂牌的月份之一。
async fn validate_own_selection(
    state: &SpreadAnalyticsState,
    workspace_id: Uuid,
    leg: &FreeSpreadLeg,
    request_id: Uuid,
) -> Result<(), SpreadApiError> {
    let known = database::spread_analytics::own_varieties(&state.auth.pool, workspace_id)
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?;
    let wanted = leg.variety.trim();
    let matched = known
        .into_iter()
        .find(|item| item.name == wanted || item.symbol == leg.symbol.trim().to_ascii_uppercase())
        .ok_or(SpreadApiError::Validation(
            "provider_selection_invalid",
            request_id,
        ))?;
    let months = database::spread_analytics::own_contract_months(
        &state.auth.pool,
        workspace_id,
        &matched.symbol,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    if !months.iter().any(|month| month == leg.month.trim()) {
        return Err(SpreadApiError::Validation(
            "provider_selection_invalid",
            request_id,
        ));
    }
    Ok(())
}

/// 自建价差引擎的来源标识。与三禾并存而不是取代：切换只是请求里的一个字符串，
/// 出了问题可以立刻切回去，也便于把两边的数字摆在一起比。
/// 席位日期选择器能列出的交易日上限。这不是分页，是防跑飞的兜底——
/// 全库 2008 年至今也只有 4516 个交易日，6000 留了约六年的余量。
///
/// 原值是 400。它在会员更名合并之前不显眼（高盛期货本来就只有更名后的 55 天），
/// 合并出 691 天之后立刻成了新的天花板：运营者仍然翻不到 2024-12 以前。
///
/// 生产实测（必须用 futures_runtime + RLS，超级用户测出来的数字不作数，
/// 见 database 侧 MEMBER_KEY 上的教训）：带会员过滤 691 天 8.5 毫秒；
/// 不带会员过滤（首次加载、还没选席位）4516 天 827 毫秒。
/// 试过把会员那套递归跳跃扫描搬来做日期，反而更慢（1397 毫秒）——
/// `trade_date` 没有可用的前导索引，普通 distinct 才是这里的最优解，别再搬。
const SEAT_DATE_LIMIT: i64 = 6000;

const SELF_PROVIDER_CODE: &str = "self";
const SELF_SOURCE_CODE: &str = "own_price_history";
const SELF_SOURCE_NAME: &str = "自建价差引擎（交易所行情）";

/// 席位每日持仓：选品种、选日期，看当天谁持了多少。
#[derive(Debug, Deserialize, ToSchema)]
pub struct SeatPositionsQuery {
    /// 会员简称。这一页以席位为主轴：选一个会员，看它在**全部品种**上的持仓，
    /// 而不是先选品种——先选品种就得为每个品种各看一遍同一个会员。
    pub member: Option<String>,
    /// 只看某个品种，可选。不给就是全部品种。
    pub instrument: Option<String>,
    /// 交易日；不给就用最近一个有数据的交易日。
    pub trade_date: Option<String>,
}

/// OpenAPI 用的形状，与 `database::spread_analytics::SeatPositionRow` 一一对应。
/// 数据库结构不直接暴给接口契约，免得改一处存储就动一次对外承诺。
#[derive(Debug, Serialize, ToSchema)]
pub struct SeatPositionItem {
    pub exchange: String,
    pub instrument: String,
    pub contract: Option<String>,
    pub is_variety_total: bool,
    pub variety_total_is_computed: bool,
    pub rank_type: String,
    pub rank: Option<i32>,
    pub member: String,
    pub quantity: String,
    pub change: Option<String>,
    pub source: String,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct SeatPositionsResponse {
    pub member: Option<String>,
    pub instrument: Option<String>,
    /// 有过持仓的会员名录，供顶部选择器使用。
    pub members: Vec<String>,
    #[serde(serialize_with = "domain::spread_analytics::date_serde::option::serialize")]
    #[schema(value_type = Option<String>, format = Date)]
    pub trade_date: Option<Date>,
    /// 该品种有数据的交易日，最新在前，供界面做日期选择。
    #[schema(value_type = Vec<String>)]
    pub available_dates: Vec<String>,
    /// 这个品种的席位数据最早从哪天起。五个品种有十几年、大商所三个只有三年，
    /// 不说清楚的话，一张空图看起来像"这个席位没持仓"，而不是"这段没有数据"。
    #[serde(serialize_with = "domain::spread_analytics::date_serde::option::serialize")]
    #[schema(value_type = Option<String>, format = Date)]
    pub coverage_start: Option<Date>,
    #[schema(value_type = Vec<SeatPositionItem>)]
    pub rows: Vec<database::spread_analytics::SeatPositionRow>,
}

#[utoipa::path(
    get,
    path = "/api/v1/spread-analytics/seats/positions",
    params(
        ("instrument" = String, Query),
        ("trade_date" = Option<String>, Query)
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = SeatPositionsResponse),
        (status = 400, body = SpreadErrorBody),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody)
    )
)]
pub async fn query_seat_positions(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Query(query): Query<SeatPositionsQuery>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = read_context(&state, &headers, request_id).await?;
    let instrument = query
        .instrument
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_uppercase);
    if instrument
        .as_deref()
        .is_some_and(|value| !value.chars().all(|c| c.is_ascii_uppercase()))
    {
        return Err(SpreadApiError::Validation("invalid_instrument", request_id));
    }
    let member = query
        .member
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string);
    let members = database::spread_analytics::seat_members(
        &state.auth.pool,
        context.workspace_id(),
        instrument.as_deref(),
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    let dates = database::spread_analytics::seat_trade_dates(
        &state.auth.pool,
        context.workspace_id(),
        member.as_deref(),
        SEAT_DATE_LIMIT,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    // 明确要哪天就用哪天；没说就用最近有数据的一天，而不是今天——
    // 今天很可能还没收盘，给一张空表不如给最近一张真表。
    let trade_date = match query.trade_date.as_deref().map(str::trim) {
        Some(raw) if !raw.is_empty() => Some(
            Date::parse(raw, &time::format_description::well_known::Iso8601::DATE)
                .map_err(|_| SpreadApiError::Validation("invalid_trade_date", request_id))?,
        ),
        _ => dates.first().copied(),
    };
    // 没选会员就不出行：全市场一天上万行，一次全给既慢又没人看得完。
    let rows = match (trade_date, member.as_deref()) {
        (Some(day), Some(_)) => database::spread_analytics::load_seat_positions(
            &state.auth.pool,
            context.workspace_id(),
            member.as_deref(),
            instrument.as_deref(),
            day,
        )
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?,
        _ => Vec::new(),
    };
    Ok(Json(ApiResponse::new(
        SeatPositionsResponse {
            member,
            instrument,
            members,
            trade_date,
            available_dates: dates.iter().map(ToString::to_string).collect(),
            coverage_start: dates.last().copied(),
            rows,
        },
        request_id,
    ))
    .into_response())
}

/// 建仓过程：一个席位在一个合约（或整个品种）上，逐日的持仓、成本与盈亏。
#[derive(Debug, Deserialize, ToSchema)]
pub struct SeatBuildingQuery {
    pub instrument: String,
    pub member: String,
    /// 不给就是品种汇总。运营者要求两个视图都有：单合约会因掉出前 20 而中断，
    /// 品种汇总几乎不会。
    pub contract: Option<String>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct BuildingDayItem {
    pub trade_date: String,
    pub open_price: Option<String>,
    pub high_price: Option<String>,
    pub low_price: Option<String>,
    pub close_price: Option<String>,
    pub settlement_price: Option<String>,
    /// 三者皆可为 `None`：那天该席位掉出了前二十，持仓未知——**不是零**。
    /// 界面据此断开曲线并标注，而不是画一条穿过零的线。
    pub long_position: Option<String>,
    pub short_position: Option<String>,
    pub net_position: Option<String>,
    /// 净持仓成本（推算），不是成交均价——我们看不到成交明细。
    pub cost: Option<String>,
    pub daily_pnl: Option<String>,
    /// 自序列开头至今的当日盈亏累计。不可知的天按 0 计入，累计线不断开。
    pub cumulative_pnl: String,
    pub open_pnl: Option<String>,
    pub cost_unknown_reason: Option<String>,
    /// 品种汇总档才有：当日的多空两腿。单合约档为空。
    pub legs: Option<VarietyLegs>,
    /// 该日持仓含**回榜反推**成分:他实际未上榜,数字由回榜日的增减倒推
    /// (运营者 2026-08-17:所有席位的推算持仓都要打标)。
    pub inferred: bool,
}

/// 品种汇总当日的两腿。**这里的多空是按合约的净方向分的组**——净多的那些合约算
/// 「多单」，净空的算「空单」，两者相减才是净持仓。与多头榜/空头榜不是一回事：
/// 同一个合约上他既可能在多头榜也可能在空头榜，那两个数相减之后才进这里。
#[derive(Debug, Serialize, ToSchema)]
pub struct VarietyLegs {
    /// 净多的那些合约，手数相加。
    pub long_lots: String,
    /// 上面那些合约的净持仓成本，按手数加权。
    pub long_cost: Option<String>,
    /// `long_cost` 实际覆盖到的手数。小于 `long_lots` 说明有合约成本不可知，
    /// 那个均价只是已知部分的均价——界面要如实说明，不能让人当成全部持仓的成本。
    pub long_cost_lots: String,
    pub short_lots: String,
    pub short_cost: Option<String>,
    pub short_cost_lots: String,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct SeatBuildingResponse {
    pub instrument: String,
    pub member: String,
    pub contract: Option<String>,
    pub is_variety_total: bool,
    /// 该品种的点值。鸡蛋是 10 而不是合约单位 5，用错盈亏正好差一倍。
    pub price_multiplier: Option<String>,
    /// 该品种有过持仓的会员，供界面选择。
    pub members: Vec<String>,
    /// 该会员在该品种上**历史持有过的全部合约**，新月份在前。
    /// 不随所选交易日变化：某个合约今天不在榜，不代表它的建仓过程不值得看。
    pub contracts: Vec<String>,
    /// 汇总档 K 线的口径：`open_interest_weighted` 或 `dominant_unadjusted`。
    /// 单合约档为 `None`——那是合约自己的真实行情，没有「口径」可言。
    ///
    /// 必须透出：图上那根 K 线是算出来的，不是任何一个合约的真实成交。界面不写明
    /// 是哪一种，看的人会当成真实价位去定止损。
    pub price_series_kind: Option<String>,
    pub days: Vec<BuildingDayItem>,
}

#[derive(Debug, Deserialize, utoipa::ToSchema)]
pub struct SaveTemplateNoteRequest {
    pub instrument_1: String,
    pub month_1: i32,
    pub instrument_2: String,
    pub month_2: i32,
    /// 空串 = 删除该模板的备注。
    pub note: String,
}

#[utoipa::path(
    put,
    path = "/api/v1/spread-analytics/monitor/template-note",
    security(("session_cookie" = [])),
    responses((status = 204), (status = 400), (status = 401), (status = 403))
)]
pub async fn save_template_note(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Json(request): Json<SaveTemplateNoteRequest>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = write_context(
        &state,
        &headers,
        Permission::ManageOverviewReport,
        request_id,
    )
    .await?;
    let ok_inst =
        |s: &str| !s.is_empty() && s.len() <= 2 && s.chars().all(|c| c.is_ascii_uppercase());
    let note = request.note.trim();
    if !ok_inst(&request.instrument_1)
        || !ok_inst(&request.instrument_2)
        || !(1..=12).contains(&request.month_1)
        || !(1..=12).contains(&request.month_2)
        || note.chars().count() > 2000
    {
        return Err(SpreadApiError::Validation("invalid_note", request_id));
    }
    database::spread_analytics::save_template_note(
        &state.auth.pool,
        context.workspace_id(),
        &request.instrument_1,
        request.month_1,
        &request.instrument_2,
        request.month_2,
        note,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    Ok(axum::http::StatusCode::NO_CONTENT.into_response())
}

#[derive(Debug, Deserialize)]
pub struct MemberInstrumentsQuery {
    pub member: String,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct MemberInstrumentsResponse {
    pub member: String,
    /// 该席位历史上持有过的全部品种。建仓过程的品种下拉用它,不随所选日期变化。
    pub instruments: Vec<String>,
}

#[utoipa::path(
    get,
    path = "/api/v1/spread-analytics/seats/member-instruments",
    params(("member" = String, Query)),
    security(("session_cookie" = [])),
    responses((status = 200, body = MemberInstrumentsResponse), (status = 401), (status = 403))
)]
pub async fn query_member_instruments(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Query(query): Query<MemberInstrumentsQuery>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = read_context(&state, &headers, request_id).await?;
    let member = query.member.trim().to_string();
    if member.is_empty() || member.chars().count() > 40 {
        return Err(SpreadApiError::Validation("invalid_member", request_id));
    }
    let instruments = database::spread_analytics::load_member_instruments(
        &state.auth.pool,
        context.workspace_id(),
        &member,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    Ok(Json(ApiResponse::new(
        MemberInstrumentsResponse {
            member,
            instruments,
        },
        request_id,
    ))
    .into_response())
}

#[utoipa::path(
    get,
    path = "/api/v1/spread-analytics/seats/building",
    params(
        ("instrument" = String, Query),
        ("member" = String, Query),
        ("contract" = Option<String>, Query)
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = SeatBuildingResponse),
        (status = 400, body = SpreadErrorBody),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody)
    )
)]
pub async fn query_seat_building(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Query(query): Query<SeatBuildingQuery>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = read_context(&state, &headers, request_id).await?;
    let instrument = query.instrument.trim().to_ascii_uppercase();
    if instrument.is_empty() || !instrument.chars().all(|c| c.is_ascii_uppercase()) {
        return Err(SpreadApiError::Validation("invalid_instrument", request_id));
    }
    let member = query.member.trim().to_string();
    let contract = query
        .contract
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_uppercase);

    let members = database::spread_analytics::seat_members(
        &state.auth.pool,
        context.workspace_id(),
        Some(instrument.as_str()),
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    let multiplier = database::spread_analytics::instrument_price_multiplier(
        &state.auth.pool,
        context.workspace_id(),
        &instrument,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;

    // 合约列表按会员取全历史，与所选交易日无关——见 seat_member_contracts 的注释。
    // 没选会员时留空：合约选择器本来就要先有会员才有意义。
    let contracts = if member.is_empty() {
        Vec::new()
    } else {
        database::spread_analytics::seat_member_contracts(
            &state.auth.pool,
            context.workspace_id(),
            &member,
            &instrument,
        )
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?
    };

    // 没有点值就不算盈亏：宁可少一条曲线，也不要一条乘错倍数的曲线。
    let factor: Decimal = multiplier
        .as_deref()
        .and_then(|value| value.parse().ok())
        .unwrap_or(Decimal::ZERO);

    let mut days = Vec::new();
    if member.is_empty() {
        // 没选会员，下面两条路都没有意义。
    } else if contract.is_none() {
        days = variety_building_days(&state, &context, &instrument, &member, factor, request_id)
            .await?;
    } else {
        let raw = database::spread_analytics::load_building_days(
            &state.auth.pool,
            context.workspace_id(),
            &instrument,
            &member,
            contract.as_deref(),
        )
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?;
        let positions: Vec<_> = raw
            .iter()
            .map(|row| DailyPosition {
                trade_date: row.trade_date,
                // 两边都没有行 = 那天他不在榜上，持仓未知。
                // 只缺一边（例如只上了多头榜）沿用旧口径按 0 计：那是「不在空头
                // 前二十」，对这些主力席位而言与无空仓接近，且历来如此。
                net_position: match (&row.long_position, &row.short_position) {
                    (None, None) => None,
                    (long, short) => Some(
                        long.as_deref().map(parse_decimal).unwrap_or(Decimal::ZERO)
                            - short.as_deref().map(parse_decimal).unwrap_or(Decimal::ZERO),
                    ),
                },
                settlement: row.settlement_price.as_deref().and_then(|v| v.parse().ok()),
            })
            .collect();
        let costs = build_cost_series(&positions, factor);
        days = raw
            .into_iter()
            .zip(costs)
            .map(|(row, cost)| BuildingDayItem {
                trade_date: row.trade_date.to_string(),
                open_price: row.open_price,
                high_price: row.high_price,
                low_price: row.low_price,
                close_price: row.close_price,
                settlement_price: row.settlement_price,
                long_position: row.long_position,
                short_position: row.short_position,
                net_position: cost.net_position.map(|value| value.to_string()),
                cost: cost.cost.map(|value| value.round_dp(4).to_string()),
                daily_pnl: cost.daily_pnl.map(|value| value.round_dp(2).to_string()),
                cumulative_pnl: cost.cumulative_pnl.round_dp(2).to_string(),
                open_pnl: cost.open_pnl.map(|value| value.round_dp(2).to_string()),
                cost_unknown_reason: cost.cost_unknown_reason.map(str::to_string),
                legs: None,
                inferred: row.inferred,
            })
            .collect();
    }

    let is_variety_total = contract.is_none();
    // 口径跟着 load_variety_candles 用的同一个判定走，不在这里另写一遍。
    let price_series_kind = is_variety_total.then(|| {
        database::spread_analytics::VarietyCandleMode::for_instrument(&instrument)
            .as_str()
            .to_string()
    });

    Ok(Json(ApiResponse::new(
        SeatBuildingResponse {
            instrument,
            member,
            is_variety_total,
            contract,
            price_multiplier: multiplier,
            members,
            contracts,
            price_series_kind,
            days,
        },
        request_id,
    ))
    .into_response())
}

/// 品种汇总档的逐日序列：先把每个合约各自的成本序列算出来，再按交易日合并。
///
/// 为什么不走交易所的品种汇总榜：那一行只有一个总手数，推不出成本，也分不出净多的
/// 合约与净空的合约。原来正是走的那条路，连结算价都没取（取数 SQL 里行情被
/// `contract` 限死），于是品种汇总下成本线、当日盈亏、累计盈亏三张图全是空的。
async fn variety_building_days(
    state: &SpreadAnalyticsState,
    context: &auth::AuthContext,
    instrument: &str,
    member: &str,
    factor: Decimal,
    request_id: Uuid,
) -> Result<Vec<BuildingDayItem>, SpreadApiError> {
    let rows = database::spread_analytics::load_variety_building_days(
        &state.auth.pool,
        context.workspace_id(),
        instrument,
        member,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;

    // SQL 已按 (contract, trade_date) 排序，顺序扫一遍切段即可。
    let mut per_contract: Vec<Vec<DailyPosition>> = Vec::new();
    let mut inferred_dates: std::collections::HashSet<Date> = std::collections::HashSet::new();
    let mut current: Option<String> = None;
    for row in rows {
        if row.inferred {
            inferred_dates.insert(row.trade_date);
        }
        if current.as_deref() != Some(row.contract.as_str()) {
            current = Some(row.contract.clone());
            per_contract.push(Vec::new());
        }
        per_contract
            .last_mut()
            .expect("上面刚推过一段")
            .push(DailyPosition {
                trade_date: row.trade_date,
                // 汇总口径下没有「掉榜」这一档：某合约某天没有行就不会出现在这里。
                // 只上了一边榜（例如只进了多头前二十）沿用单合约页的口径按 0 计。
                net_position: Some(
                    row.long_position
                        .as_deref()
                        .map(parse_decimal)
                        .unwrap_or(Decimal::ZERO)
                        - row
                            .short_position
                            .as_deref()
                            .map(parse_decimal)
                            .unwrap_or(Decimal::ZERO),
                ),
                settlement: row.settlement_price.as_deref().and_then(|v| v.parse().ok()),
            });
    }

    // 合成行情：金银按持仓量加权，其余取当日主力合约（不复权）。**只画图**——
    // 下面 cost / daily_pnl / cumulative_pnl 全部仍由 build_variety_series 从各合约
    // 自己的结算价算出，没有一个数经过这里。他持的是具体合约，不是加权指数。
    let candles = database::spread_analytics::load_variety_candles(
        &state.auth.pool,
        context.workspace_id(),
        instrument,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    let candle_dates: Vec<Date> = candles.iter().map(|candle| candle.trade_date).collect();
    let candle_by_date: HashMap<Date, _> = candles
        .into_iter()
        .map(|candle| (candle.trade_date, candle))
        .collect();

    // 掉榜日补轴(2026-08-16 运营者拍板,高盛 2026-01-29 实例):席位行驱动的
    // 日期轴在「全部合约同日掉榜」时整天消失,K 线与三张折线一起断。以合成
    // 行情的完整交易日历为外轴,把席位序列首尾之间缺失的交易日补回来:
    // 持仓留 None(前端按掉榜口径画 0 并标注)、当日盈亏留 None、累计盈亏
    // 沿用前值(与「不可知的天按 0 计入、累计线不断开」同一条既有口径)、
    // legs/成本一概留空——**这些补行绝不进成本引擎**(0 进成本链的假盈亏
    // 事故见 building_days_sql 内注释)。
    let series = build_variety_series(&per_contract, factor);
    let series_by_date: HashMap<Date, usize> = series
        .iter()
        .enumerate()
        .map(|(index, day)| (day.trade_date, index))
        .collect();
    let (first, last) = match (series.first(), series.last()) {
        (Some(first), Some(last)) => (first.trade_date, last.trade_date),
        _ => return Ok(Vec::new()),
    };
    let mut merged: Vec<(Date, Option<usize>)> = Vec::new();
    for date in candle_dates {
        if date < first || date > last {
            continue;
        }
        merged.push((date, series_by_date.get(&date).copied()));
    }
    // 行情缺席位有的日子(理论上不该有,但两表来源不同)不能反过来丢席位行。
    let merged_dates: BTreeSet<Date> = merged.iter().map(|(date, _)| *date).collect();
    for (index, day) in series.iter().enumerate() {
        if !merged_dates.contains(&day.trade_date) {
            merged.push((day.trade_date, Some(index)));
        }
    }
    merged.sort_by_key(|(date, _)| *date);

    let mut last_cumulative = String::from("0");
    Ok(merged
        .into_iter()
        .map(|(date, series_index)| {
            let candle = candle_by_date.get(&date);
            let Some(index) = series_index else {
                // 补回来的掉榜日:只有行情,没有任何席位与盈亏数字。
                return BuildingDayItem {
                    trade_date: date.to_string(),
                    open_price: candle.map(|c| c.open_price.clone()),
                    high_price: candle.map(|c| c.high_price.clone()),
                    low_price: candle.map(|c| c.low_price.clone()),
                    close_price: candle.map(|c| c.close_price.clone()),
                    settlement_price: None,
                    long_position: None,
                    short_position: None,
                    net_position: None,
                    cost: None,
                    daily_pnl: None,
                    cumulative_pnl: last_cumulative.clone(),
                    open_pnl: None,
                    cost_unknown_reason: Some("seat_off_the_board".to_string()),
                    legs: None,
                    inferred: false,
                };
            };
            let day = &series[index];
            last_cumulative = day.cumulative_pnl.round_dp(2).to_string();
            BuildingDayItem {
                trade_date: day.trade_date.to_string(),
                open_price: candle.map(|c| c.open_price.clone()),
                high_price: candle.map(|c| c.high_price.clone()),
                low_price: candle.map(|c| c.low_price.clone()),
                close_price: candle.map(|c| c.close_price.clone()),
                // 结算价留空：汇总档的结算价没有单一取值，成本引擎用的是各合约自己的那个。
                settlement_price: None,
                // 汇总档不给多头榜/空头榜的原始手数：`legs` 里的多空是按**合约净方向**
                // 分的组，与那两张榜不是一回事，共用字段名只会让人读错。
                long_position: None,
                short_position: None,
                net_position: Some(day.net_position.to_string()),
                // 多空混持时没有一个有意义的「净持仓成本」——净多两千手与净空一千手
                // 不是同一笔仓位的两部分。两腿各自的均价在 `legs` 里。
                cost: None,
                daily_pnl: day.daily_pnl.map(|value| value.round_dp(2).to_string()),
                cumulative_pnl: day.cumulative_pnl.round_dp(2).to_string(),
                open_pnl: None,
                cost_unknown_reason: None,
                legs: Some(VarietyLegs {
                    long_lots: day.long_lots.to_string(),
                    long_cost: day.long_cost.map(|value| value.round_dp(4).to_string()),
                    long_cost_lots: day.long_cost_lots.to_string(),
                    short_lots: day.short_lots.to_string(),
                    short_cost: day.short_cost.map(|value| value.round_dp(4).to_string()),
                    short_cost_lots: day.short_cost_lots.to_string(),
                }),
                inferred: inferred_dates.contains(&date),
            }
        })
        .collect())
}

// ---------------------------------------------------------------------------
// 席位净持仓：几家席位合起来看
// ---------------------------------------------------------------------------
//
// 与建仓过程那条路的区别只有一处：一次看好几家，把它们的持仓加到一起。**不算成本、
// 不算盈亏**——五家机构的仓不是同一笔仓，给它们算一个「平均成本」会得出一个不对应
// 任何真实仓位的数字。

/// 一次最多合并多少家席位。
///
/// 十家是运营者定的。这个上限不只是为了界面好看：每多一家就多一次变体展开，而合起来
/// 看二十家的意义本来也不大——那已经接近「全市场」，该看的是另一张图。
const MAX_NET_POSITION_MEMBERS: usize = 10;

#[derive(Debug, Deserialize, ToSchema)]
pub struct SeatNetPositionQuery {
    pub instrument: String,
    /// 逗号分隔的会员名。会员名里不会有逗号（归一后是「中信期货」这种写法）。
    pub members: Option<String>,
    pub contract: Option<String>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct NetPositionDayItem {
    pub trade_date: String,
    pub open_price: Option<String>,
    pub high_price: Option<String>,
    pub low_price: Option<String>,
    pub close_price: Option<String>,
    /// 所选席位当天的合计净持仓。**只含当天在榜的那几家**，见 `missing_members`。
    pub net_position: String,
    /// 当天净多的那些「席位×合约」，手数相加。分腿口径同建仓过程的合约汇总。
    pub long_lots: String,
    pub short_lots: String,
    pub counted_members: Vec<String>,
    /// 当天掉出前二十的席位：持仓**未知**，没有计进上面的合计。
    /// 界面必须把这件事说出来——按零计会画出一根假的大幅减仓。
    pub missing_members: Vec<String>,
    /// 当天按**回榜反推值**计入合计的席位:他们实际未上榜,数字是倒推的。
    pub inferred_members: Vec<String>,
    /// 当日盈亏 =(今结算 − 昨结算)× 昨净持仓 × 点值,**逐「席位×合约」各算各的
    /// 再相加**(与建仓过程同一套 `build_variety_series`,不另起一套口径)。
    /// 掉榜或无结算价的那天为空:那天赚了多少不知道,不是零。
    pub daily_pnl: Option<String>,
    /// 自序列开头至今的当日盈亏累计。不可知的天按 0 计入,累计线不断开。
    pub cumulative_pnl: String,
    /// 净多那几条腿的加权成本(推算),覆盖手数见 `long_cost_lots`。
    pub long_cost: Option<String>,
    pub long_cost_lots: String,
    pub short_cost: Option<String>,
    pub short_cost_lots: String,
}

/// 最新一天里**某一家**的多空手数与均价。
///
/// 合计那一行说不出「这几家里谁在多、谁在空、各自成本多少」——五家合起来净空
/// 一万四千手，可能是五家都在空，也可能是一家重仓空、四家轻仓多。摘要下面那排
/// 就是拆给人看的（2026-08-19 运营者要求）。
///
/// **与合计同源**：按 member 分组各跑一遍 `build_variety_series`，算法与合计
/// 一字不差。合计的均价本就是各家加权还原，所以这排数加起来必然对得上合计，
/// 不会出现「分项与总数打架」。
#[derive(Debug, Serialize, ToSchema)]
pub struct MemberLegItem {
    pub member: String,
    pub long_lots: String,
    pub long_cost: Option<String>,
    pub long_cost_lots: String,
    pub short_lots: String,
    pub short_cost: Option<String>,
    pub short_cost_lots: String,
    /// 这家当天不在榜：持仓**未知**，不是零，也没计进合计。
    pub missing: bool,
    /// 这家当天的持仓含回榜反推成分：实际未上榜，数字由回榜日增减倒推。
    pub inferred: bool,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct SeatNetPositionResponse {
    pub instrument: String,
    pub contract: Option<String>,
    pub is_variety_total: bool,
    /// 去重后的所选席位，回显给界面对齐。
    pub members: Vec<String>,
    /// 该品种上有过持仓的全部会员，供选择器使用。
    pub all_members: Vec<String>,
    /// 所选席位在该品种上持有过的合约并集，新月份在前。
    pub contracts: Vec<String>,
    /// 汇总档 K 线的口径；选定单合约时为 `None`（那是真实行情）。
    pub price_series_kind: Option<String>,
    /// 合约点值。盈亏由它乘出来，界面要把它写在明面上——看的人有权知道那条
    /// 曲线是按什么倍数算的。库里没有配置时为 `None`，此时盈亏整条不算。
    pub price_multiplier: Option<String>,
    pub days: Vec<NetPositionDayItem>,
    /// 最新一天逐家的多空手数与均价，摘要那排用。日期就是 `days` 最后一天，
    /// 一并回出来免得两边各自判断「哪天算最新」。没有数据时为空。
    pub latest_trade_date: Option<String>,
    pub latest_members: Vec<MemberLegItem>,
}

#[utoipa::path(
    get,
    path = "/api/v1/spread-analytics/seats/net-position",
    params(
        ("instrument" = String, Query),
        ("members" = Option<String>, Query),
        ("contract" = Option<String>, Query)
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = SeatNetPositionResponse),
        (status = 400, body = SpreadErrorBody),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody)
    )
)]
pub async fn query_seat_net_position(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Query(query): Query<SeatNetPositionQuery>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = read_context(&state, &headers, request_id).await?;
    let instrument = query.instrument.trim().to_ascii_uppercase();
    if instrument.is_empty() || !instrument.chars().all(|c| c.is_ascii_uppercase()) {
        return Err(SpreadApiError::Validation("invalid_instrument", request_id));
    }
    let members = parse_member_list(query.members.as_deref(), request_id)?;
    let contract = query
        .contract
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_ascii_uppercase);

    let all_members = database::spread_analytics::seat_members(
        &state.auth.pool,
        context.workspace_id(),
        Some(instrument.as_str()),
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;

    // 合约选项取所选席位的并集：有的合约只有其中一家持有过，只列交集会让它消失。
    let mut contracts: Vec<String> = Vec::new();
    for member in &members {
        let owned = database::spread_analytics::seat_member_contracts(
            &state.auth.pool,
            context.workspace_id(),
            member,
            &instrument,
        )
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?;
        for code in owned {
            if !contracts.contains(&code) {
                contracts.push(code);
            }
        }
    }
    // 新月份在前，与建仓过程的合约选择器一致。
    contracts.sort_by(|a, b| b.cmp(a));

    // 点值在 members 为空时也要回：界面顶部那句「合约汇总 · 点值 1000」不该因为
    // 一时没勾席位就消失。
    let price_multiplier = database::spread_analytics::instrument_price_multiplier(
        &state.auth.pool,
        context.workspace_id(),
        &instrument,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;

    let mut days = Vec::new();
    let mut latest_trade_date: Option<String> = None;
    let mut latest_members: Vec<MemberLegItem> = Vec::new();
    if !members.is_empty() {
        let rows = database::spread_analytics::load_seat_net_positions(
            &state.auth.pool,
            context.workspace_id(),
            &instrument,
            &members,
            contract.as_deref(),
        )
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?;

        // 行情：汇总档用合成价（金银加权、其余主连），选定单合约时用那个合约的真实行情。
        let candles = match contract.as_deref() {
            Some(code) => {
                database::spread_analytics::load_contract_candles(
                    &state.auth.pool,
                    context.workspace_id(),
                    &instrument,
                    code,
                )
                .await
            }
            None => {
                database::spread_analytics::load_variety_candles(
                    &state.auth.pool,
                    context.workspace_id(),
                    &instrument,
                )
                .await
            }
        }
        .map_err(|_| SpreadApiError::Internal(request_id))?;
        let candle_by_date: HashMap<Date, _> = candles
            .into_iter()
            .map(|candle| (candle.trade_date, candle))
            .collect();

        let mut inferred_by_date: HashMap<Date, std::collections::BTreeSet<String>> =
            HashMap::new();
        for row in &rows {
            if row.inferred {
                inferred_by_date
                    .entry(row.trade_date)
                    .or_default()
                    .insert(row.member_key.clone());
            }
        }
        // 盈亏与成本:**每个「席位 × 合约」各自走一遍成本引擎再按日归并**。
        //
        // 分组维度是这里唯一与建仓过程不同的地方(那边是「合约」),算法本身
        // 一行不改地复用 `build_variety_series`——掉榜那天不贡献手数、成本不可知
        // 的合约照样计手数但不进均价、累计线不因不可知而断开,这些口径都已经在
        // 那套逻辑里验证过。另起一套只会让两个页面的盈亏迟早对不上。
        // 没有点值就不算盈亏:宁可少一条曲线,也不要一条乘错倍数的曲线
        // (与建仓过程同一条纪律)。
        let pnl_factor: Decimal = price_multiplier
            .as_deref()
            .and_then(|value| value.parse().ok())
            .unwrap_or(Decimal::ZERO);

        let mut by_seat_contract: BTreeMap<(String, String), Vec<DailyPosition>> = BTreeMap::new();
        for observation in &rows {
            let long = parse_decimal(&observation.long_position);
            let short = parse_decimal(&observation.short_position);
            by_seat_contract
                .entry((observation.member_key.clone(), observation.contract.clone()))
                .or_default()
                .push(DailyPosition {
                    trade_date: observation.trade_date,
                    net_position: Some(long - short),
                    settlement: observation.settlement.as_deref().map(parse_decimal),
                });
        }
        let per_series: Vec<Vec<DailyPosition>> = by_seat_contract.values().cloned().collect();
        let pnl_by_date: HashMap<Date, domain::seat_cost::VarietyDay> =
            build_variety_series(&per_series, pnl_factor)
                .into_iter()
                .map(|day| (day.trade_date, day))
                .collect();

        // 逐家再算一遍，供摘要下面那排「各家各自多少手、成本多少」。
        //
        // 分组维度换成「席位」，喂进去的还是同一批 (席位×合约) 序列、同一个
        // `build_variety_series`——合计与分项必须同源，各写一套迟早对不上。
        let mut per_member: BTreeMap<String, Vec<Vec<DailyPosition>>> = BTreeMap::new();
        for ((member, _contract), series) in by_seat_contract {
            per_member.entry(member).or_default().push(series);
        }
        let member_days: BTreeMap<String, HashMap<Date, domain::seat_cost::VarietyDay>> =
            per_member
                .into_iter()
                .map(|(member, series)| {
                    let by_date = build_variety_series(&series, pnl_factor)
                        .into_iter()
                        .map(|day| (day.trade_date, day))
                        .collect();
                    (member, by_date)
                })
                .collect();

        let observations: Vec<SeatContractDay> = rows
            .into_iter()
            .map(|row| SeatContractDay {
                member: row.member_key,
                trade_date: row.trade_date,
                long: parse_decimal(&row.long_position),
                short: parse_decimal(&row.short_position),
            })
            .collect();

        let series = build_net_position_series(&observations, &members);
        // 「最新一天」在这里定一次就够：`days` 出去之后 trade_date 已经是字符串，
        // 让前端再判一次哪天算最新，两边就有了各自的口径。
        let latest_date = series.last().map(|day| day.trade_date);
        if let Some(date) = latest_date {
            latest_trade_date = Some(date.to_string());
            latest_members = members
                .iter()
                .map(|member| {
                    let day = member_days.get(member).and_then(|days| days.get(&date));
                    MemberLegItem {
                        member: member.clone(),
                        // 掉榜那天这家一行都没有：手数留 0 而由 `missing` 说明
                        // 「未知」——界面据此写「当日掉榜」，不是写「0 手」。
                        long_lots: day
                            .map(|d| d.long_lots.to_string())
                            .unwrap_or_else(|| "0".to_string()),
                        long_cost: day
                            .and_then(|d| d.long_cost)
                            .map(|v| v.round_dp(2).to_string()),
                        long_cost_lots: day
                            .map(|d| d.long_cost_lots.to_string())
                            .unwrap_or_else(|| "0".to_string()),
                        short_lots: day
                            .map(|d| d.short_lots.to_string())
                            .unwrap_or_else(|| "0".to_string()),
                        short_cost: day
                            .and_then(|d| d.short_cost)
                            .map(|v| v.round_dp(2).to_string()),
                        short_cost_lots: day
                            .map(|d| d.short_cost_lots.to_string())
                            .unwrap_or_else(|| "0".to_string()),
                        missing: day.is_none(),
                        inferred: inferred_by_date
                            .get(&date)
                            .is_some_and(|set| set.contains(member)),
                    }
                })
                .collect();
        }

        days = series
            .into_iter()
            .map(|day| {
                let candle = candle_by_date.get(&day.trade_date);
                let pnl = pnl_by_date.get(&day.trade_date);
                NetPositionDayItem {
                    trade_date: day.trade_date.to_string(),
                    open_price: candle.map(|c| c.open_price.clone()),
                    high_price: candle.map(|c| c.high_price.clone()),
                    low_price: candle.map(|c| c.low_price.clone()),
                    close_price: candle.map(|c| c.close_price.clone()),
                    net_position: day.net_position.to_string(),
                    long_lots: day.long_lots.to_string(),
                    short_lots: day.short_lots.to_string(),
                    counted_members: day.counted_members,
                    missing_members: day.missing_members,
                    inferred_members: inferred_by_date
                        .get(&day.trade_date)
                        .map(|set| set.iter().cloned().collect())
                        .unwrap_or_default(),
                    daily_pnl: pnl
                        .and_then(|p| p.daily_pnl)
                        .map(|v| v.round_dp(2).to_string()),
                    cumulative_pnl: pnl
                        .map(|p| p.cumulative_pnl.round_dp(2).to_string())
                        .unwrap_or_else(|| "0".to_string()),
                    long_cost: pnl
                        .and_then(|p| p.long_cost)
                        .map(|v| v.round_dp(2).to_string()),
                    long_cost_lots: pnl
                        .map(|p| p.long_cost_lots.to_string())
                        .unwrap_or_else(|| "0".to_string()),
                    short_cost: pnl
                        .and_then(|p| p.short_cost)
                        .map(|v| v.round_dp(2).to_string()),
                    short_cost_lots: pnl
                        .map(|p| p.short_cost_lots.to_string())
                        .unwrap_or_else(|| "0".to_string()),
                }
            })
            .collect();
    }

    let is_variety_total = contract.is_none();
    let price_series_kind = is_variety_total.then(|| {
        database::spread_analytics::VarietyCandleMode::for_instrument(&instrument)
            .as_str()
            .to_string()
    });

    Ok(Json(ApiResponse::new(
        SeatNetPositionResponse {
            instrument,
            contract,
            is_variety_total,
            members,
            all_members,
            contracts,
            price_series_kind,
            price_multiplier,
            days,
            latest_trade_date,
            latest_members,
        },
        request_id,
    ))
    .into_response())
}

/// 解析逗号分隔的会员名，顺带去重。
///
/// **去重不是整洁癖**：同一家选两次会被逐日加两遍，合计直接翻倍，而图上看不出任何
/// 异常。这是这条路最该防的一件事，所以查询与收藏两处都走这里。
fn parse_member_list(raw: Option<&str>, request_id: Uuid) -> Result<Vec<String>, SpreadApiError> {
    let mut members: Vec<String> = Vec::new();
    for name in raw.unwrap_or_default().split(',') {
        let name = name.trim();
        if name.is_empty() || members.iter().any(|kept| kept == name) {
            continue;
        }
        members.push(name.to_string());
    }
    if members.len() > MAX_NET_POSITION_MEMBERS {
        return Err(SpreadApiError::Validation("too_many_members", request_id));
    }
    Ok(members)
}

// ---------------------------------------------------------------------------
// 席位组合收藏
// ---------------------------------------------------------------------------

#[derive(Debug, Serialize, ToSchema)]
pub struct SeatFavoriteResponse {
    pub id: String,
    pub name: String,
    pub members: Vec<String>,
}

#[derive(Debug, Deserialize, ToSchema)]
pub struct SaveSeatFavoriteRequest {
    pub name: String,
    pub members: Vec<String>,
}

#[utoipa::path(
    get,
    path = "/api/v1/spread-analytics/seats/member-favorites",
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = Vec<SeatFavoriteResponse>),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody)
    )
)]
pub async fn list_seat_member_favorites(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = read_context(&state, &headers, request_id).await?;
    let items: Vec<SeatFavoriteResponse> = database::spread_analytics::list_seat_member_favorites(
        &state.auth.pool,
        context.workspace_id(),
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?
    .into_iter()
    .map(|favorite| SeatFavoriteResponse {
        id: favorite.id.to_string(),
        name: favorite.name,
        members: favorite.members,
    })
    .collect();
    Ok(Json(ApiResponse::new(items, request_id)).into_response())
}

#[utoipa::path(
    post,
    path = "/api/v1/spread-analytics/seats/member-favorites",
    params(("x-csrf-token" = String, Header)),
    request_body = SaveSeatFavoriteRequest,
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = SeatFavoriteResponse),
        (status = 400, body = SpreadErrorBody),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody),
        (status = 409, body = SpreadErrorBody)
    )
)]
pub async fn create_seat_member_favorite(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Json(request): Json<SaveSeatFavoriteRequest>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = write_context(
        &state,
        &headers,
        Permission::ManageSeatFavorites,
        request_id,
    )
    .await?;
    let name = request.name.trim().to_string();
    if name.is_empty() || name.chars().count() > 40 {
        return Err(SpreadApiError::Validation("invalid_name", request_id));
    }
    // 走同一个去重：收藏里混进重复的一家，之后每次用它都会把合计算翻倍。
    let members = parse_member_list(Some(&request.members.join(",")), request_id)?;
    if members.is_empty() {
        return Err(SpreadApiError::Validation("empty_members", request_id));
    }

    let id = Uuid::now_v7();
    database::spread_analytics::create_seat_member_favorite(
        &state.auth.pool,
        context.workspace_id(),
        id,
        &name,
        &members,
    )
    .await
    .map_err(|error| match error {
        // 同名撞车是运营者能自己处理的事（换个名字），不是内部错误。
        sqlx::Error::Database(ref db) if db.is_unique_violation() => {
            SpreadApiError::Conflict("favorite_name_taken", request_id)
        }
        _ => SpreadApiError::Internal(request_id),
    })?;

    Ok(Json(ApiResponse::new(
        SeatFavoriteResponse {
            id: id.to_string(),
            name,
            members,
        },
        request_id,
    ))
    .into_response())
}

#[utoipa::path(
    delete,
    path = "/api/v1/spread-analytics/seats/member-favorites/{favorite_id}",
    params(
        ("favorite_id" = String, Path),
        ("x-csrf-token" = String, Header)
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 204),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody),
        (status = 404, body = SpreadErrorBody)
    )
)]
pub async fn delete_seat_member_favorite(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Path(favorite_id): Path<Uuid>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = write_context(
        &state,
        &headers,
        Permission::ManageSeatFavorites,
        request_id,
    )
    .await?;
    let removed = database::spread_analytics::delete_seat_member_favorite(
        &state.auth.pool,
        context.workspace_id(),
        favorite_id,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    if !removed {
        // 删一个不存在的东西该是 404。回 204 会让界面以为删掉了，刷新后它还在。
        return Err(SpreadApiError::NotFound("favorite_not_found", request_id));
    }
    Ok(StatusCode::NO_CONTENT.into_response())
}

// ---------------------------------------------------------------------------
// 总览页「黄金白银报告表」
// ---------------------------------------------------------------------------
//
// 一张表两个来源：上半（压力位/支撑位）是运营者手填的盘面判断，平台无从计算；
// 下半（席位净持仓与筹码）全自动，从已有事实表现算。两半分开存取，别混。

/// 报告表下半部分的一行。
#[derive(Debug, Serialize, ToSchema)]
pub struct ReportSeatRow {
    /// 「国泰席位」「机构持仓」这种显示名。
    pub label: String,
    /// 这一行由哪几家席位合成。单席位行就一个。
    pub members: Vec<String>,
    /// 合计行（机构持仓 / 外资持仓 / 散户席位），界面要与单席位行区分开。
    pub is_total: bool,
    pub gold: ReportSeatCell,
    pub silver: ReportSeatCell,
}

/// 一个品种上的昨 / 今净持仓与筹码。
#[derive(Debug, Serialize, ToSchema)]
pub struct ReportSeatCell {
    /// 前一交易日净仓。掉榜且反推不出时为空(三态口径,显示横杠)。
    pub previous_net: Option<String>,
    /// previous_net 里含有反推成分(该席位前一日掉榜,值由当日「持仓−增减」反推;
    /// 合计行只要有一家是反推的就为 true)。界面据此打「推」字标。
    pub previous_net_inferred: bool,
    pub net: Option<String>,
    pub cost: Option<String>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct OverviewReportResponse {
    /// 报告的交易日，标题里那个日期。
    pub trade_date: String,
    /// 压力位网格。运营者没填过就是 `None`，界面显示空表让他填。
    pub levels: Option<Value>,
    /// `levels` 实际来自哪一天。早于 `trade_date` 说明是沿用上一次填的，
    /// 界面要标出来——不标就等于把上周的判断冒充成今天的。
    pub levels_source_date: Option<String>,
    pub seat_groups: Vec<ReportSeatGroup>,
    pub rows: Vec<ReportSeatRow>,
}

#[derive(Debug, Serialize, Deserialize, ToSchema)]
pub struct ReportSeatGroup {
    /// `institution` / `watch` / `foreign` / `retail`
    pub group_key: String,
    pub members: Vec<String>,
}

#[derive(Debug, Deserialize, ToSchema)]
pub struct SaveReportLevelsRequest {
    pub trade_date: String,
    pub cells: Value,
}

#[derive(Debug, Deserialize, ToSchema)]
pub struct SaveReportSeatGroupsRequest {
    pub groups: Vec<ReportSeatGroup>,
}

/// 报告表只看金银两个品种——运营者明确说过其他品种是以后的事。
const REPORT_INSTRUMENTS: [&str; 2] = ["AU", "AG"];
/// 四组的默认名单。运营者没配过时用它，配过就以库里为准。
/// 与他 2026-08-15 给的那张报告表逐字对应。
const DEFAULT_SEAT_GROUPS: [(&str, &[&str]); 4] = [
    (
        "institution",
        &["国泰君安", "中信期货", "东证期货", "永安期货", "海通期货"],
    ),
    ("watch", &["中财期货"]),
    ("foreign", &["高盛期货"]),
    (
        "retail",
        &["东方财富", "方正中期", "徽商期货", "平安期货", "中信建投"],
    ),
];

fn parse_trade_date(value: &str, request_id: Uuid) -> Result<Date, SpreadApiError> {
    Date::parse(value, &time::format_description::well_known::Iso8601::DATE)
        .map_err(|_| SpreadApiError::Validation("invalid_trade_date", request_id))
}

/// 把配置的四组名单摊平成表上的行。
///
/// 行序与运营者那张报告表一致：机构逐行 → 其他关注逐行 → 外资逐行 →
/// 机构持仓 → 外资持仓 → 散户席位。**散户只出合计行**，不逐行显示——
/// 那五家是用来定义「散户」这个合计的，不是要一家家看。
fn report_row_plan(groups: &[(String, Vec<String>)]) -> Vec<(String, Vec<String>, bool)> {
    let pick = |key: &str| -> Vec<String> {
        groups
            .iter()
            .find(|(group_key, _)| group_key == key)
            .map(|(_, members)| members.clone())
            .unwrap_or_default()
    };
    let institution = pick("institution");
    let watch = pick("watch");
    let foreign = pick("foreign");
    let retail = pick("retail");

    let mut plan: Vec<(String, Vec<String>, bool)> = Vec::new();
    for member in institution.iter().chain(&watch).chain(&foreign) {
        // 「国泰君安」→「国泰席位」。取前两个字是运营者那张表的写法；
        // 名字短于两个字就整名照用，别切出半个字来。
        let short: String = member.chars().take(2).collect();
        plan.push((format!("{short}席位"), vec![member.clone()], false));
    }
    if !institution.is_empty() {
        plan.push(("机构持仓".to_string(), institution, true));
    }
    if !foreign.is_empty() {
        plan.push(("外资持仓".to_string(), foreign, true));
    }
    if !retail.is_empty() {
        plan.push(("散户席位".to_string(), retail, true));
    }
    plan
}

#[utoipa::path(
    get,
    path = "/api/v1/overview/report",
    params(("trade_date" = Option<String>, Query)),
    security(("session_cookie" = [])),
    responses((status = 200, body = OverviewReportResponse), (status = 401), (status = 500))
)]
pub async fn query_overview_report(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Query(query): Query<std::collections::HashMap<String, String>>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = read_context(&state, &headers, request_id).await?;
    let workspace = context.workspace_id();

    // 没指定日子就用最新有席位数据的那天。让人自己去猜今天有没有数据是多余的。
    let trade_date = match query.get("trade_date").map(String::as_str) {
        Some(value) if !value.trim().is_empty() => parse_trade_date(value.trim(), request_id)?,
        _ => database::spread_analytics::seat_trade_dates(&state.auth.pool, workspace, None, 1)
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?
            .into_iter()
            .next()
            .ok_or(SpreadApiError::Internal(request_id))?,
    };

    // **数据没变就一直用这份**(设计见 report_cache 字段注释与 DEC-078)。
    // 版本查询是一条按 (workspace, trade_date) 走索引的 max,毫秒级;
    // 真正贵的是它保护的那十几秒成本推算。
    let version =
        database::spread_analytics::report_data_version(&state.auth.pool, workspace, trade_date)
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?;
    if let Some((cached_version, value)) = state
        .report_cache
        .read()
        .await
        .get(&(workspace, trade_date))
        && *cached_version == version
    {
        return Ok(Json(ApiResponse::new(value.clone(), request_id)).into_response());
    }

    let value = compute_overview_report(&state, workspace, trade_date, version, request_id).await?;
    Ok(Json(ApiResponse::new(value, request_id)).into_response())
}

/// 算一份报告表并写进缓存。**handler 与后台预热共用同一条路径**——
/// 预热要是走另一套代码,两边迟早算出不一样的东西。
///
/// 十几秒的成本推算就发生在这里(七家席位 × 金银 × 全历史),
/// 所以它必须只被缓存未命中的那一次调用。
async fn compute_overview_report(
    state: &Arc<SpreadAnalyticsState>,
    workspace: Uuid,
    trade_date: Date,
    version: Option<OffsetDateTime>,
    request_id: Uuid,
) -> Result<serde_json::Value, SpreadApiError> {
    let stored = database::spread_analytics::load_report_seat_groups(&state.auth.pool, workspace)
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?;
    let groups: Vec<(String, Vec<String>)> = if stored.is_empty() {
        DEFAULT_SEAT_GROUPS
            .iter()
            .map(|(key, members)| {
                (
                    (*key).to_string(),
                    members.iter().map(|name| (*name).to_string()).collect(),
                )
            })
            .collect()
    } else {
        stored
    };

    let mut members: Vec<String> = groups
        .iter()
        .flat_map(|(_, list)| list.iter().cloned())
        .collect();
    members.sort();
    members.dedup();

    let instruments: Vec<String> = REPORT_INSTRUMENTS
        .iter()
        .map(|code| (*code).to_string())
        .collect();

    let (today, yesterday) = database::spread_analytics::load_report_nets(
        &state.auth.pool,
        workspace,
        &instruments,
        &members,
        trade_date,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;

    let cost_rows = database::spread_analytics::load_report_cost_rows(
        &state.auth.pool,
        workspace,
        &instruments,
        &members,
        trade_date,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;

    // 点值只影响盈亏，报告表用不到；但传个假的会让 `build_variety_series` 输出一份
    // 乘错倍数的盈亏，日后有人顺手读它就中招。取真的，两次查询而已。
    let mut multipliers: std::collections::HashMap<String, Decimal> =
        std::collections::HashMap::new();
    for code in &instruments {
        let value = database::spread_analytics::instrument_price_multiplier(
            &state.auth.pool,
            workspace,
            code,
        )
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?
        .and_then(|raw| raw.parse().ok())
        .unwrap_or(Decimal::ZERO);
        multipliers.insert(code.clone(), value);
    }

    // 按 (席位, 品种, 合约) 切段。SQL 已按这个顺序排好，顺序扫一遍即可。
    let mut per_pair: std::collections::HashMap<(String, String), Vec<Vec<DailyPosition>>> =
        std::collections::HashMap::new();
    let mut current: Option<(String, String, String)> = None;
    for (member, instrument, row) in cost_rows {
        let key = (member.clone(), instrument.clone(), row.contract.clone());
        let bucket = per_pair.entry((member, instrument)).or_default();
        if current.as_ref() != Some(&key) {
            current = Some(key);
            bucket.push(Vec::new());
        }
        bucket
            .last_mut()
            .expect("上面刚推过一段")
            .push(DailyPosition {
                trade_date: row.trade_date,
                net_position: Some(
                    row.long_position
                        .as_deref()
                        .map(parse_decimal)
                        .unwrap_or(Decimal::ZERO)
                        - row
                            .short_position
                            .as_deref()
                            .map(parse_decimal)
                            .unwrap_or(Decimal::ZERO),
                ),
                settlement: row.settlement_price.as_deref().and_then(|v| v.parse().ok()),
            });
    }

    // 每个 (席位, 品种) 当日的筹码：净多看多头腿的加权成本，净空看空头腿的。
    let mut costs: std::collections::HashMap<(String, String), Decimal> =
        std::collections::HashMap::new();
    for ((member, instrument), contracts) in per_pair {
        let factor = multipliers
            .get(&instrument)
            .copied()
            .unwrap_or(Decimal::ZERO);
        let series = build_variety_series(&contracts, factor);
        let Some(day) = series.into_iter().find(|day| day.trade_date == trade_date) else {
            continue;
        };
        let picked = if day.net_position > Decimal::ZERO {
            day.long_cost
        } else if day.net_position < Decimal::ZERO {
            day.short_cost
        } else {
            None
        };
        if let Some(cost) = picked {
            costs.insert((member, instrument), cost);
        }
    }

    let net_of = |rows: &[database::spread_analytics::ReportNetRow],
                  members: &[String],
                  instrument: &str|
     -> Option<String> {
        let mut total = Decimal::ZERO;
        let mut seen = false;
        for row in rows {
            if row.instrument == instrument && members.iter().any(|name| name == &row.member) {
                total += parse_decimal(&row.net_position);
                seen = true;
            }
        }
        // 一家都没在榜上：报未知，不是零。合计行只要有一家在榜就给和——
        // 那是「已知部分的合计」，与席位页累计盈亏同一条纪律。
        seen.then(|| total.normalize().to_string())
    };

    let rows: Vec<ReportSeatRow> = report_row_plan(&groups)
        .into_iter()
        .map(|(label, members, is_total)| {
            let cell = |instrument: &str| {
                let (previous_net, previous_net_inferred) =
                    report_prev_net(&yesterday, &today, &members, instrument);
                ReportSeatCell {
                    previous_net,
                    previous_net_inferred,
                    net: net_of(&today, &members, instrument),
                    // 合计行不给筹码：把几家成本不同的仓位平均成一个数没有意义，
                    // 运营者那张表在合计行放的也是「加/减」方向而不是价。
                    cost: (!is_total)
                        .then(|| {
                            members.first().and_then(|member| {
                                costs
                                    .get(&(member.clone(), instrument.to_string()))
                                    .map(|value| value.round_dp(4).to_string())
                            })
                        })
                        .flatten(),
                }
            };
            ReportSeatRow {
                gold: cell("AU"),
                silver: cell("AG"),
                label,
                members,
                is_total,
            }
        })
        .collect();

    let levels =
        database::spread_analytics::load_report_levels(&state.auth.pool, workspace, trade_date)
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?;

    let response = OverviewReportResponse {
        trade_date: trade_date.to_string(),
        levels: levels.as_ref().map(|(_, cells)| cells.clone()),
        levels_source_date: levels.as_ref().map(|(date, _)| date.to_string()),
        seat_groups: groups
            .into_iter()
            .map(|(group_key, members)| ReportSeatGroup { group_key, members })
            .collect(),
        rows,
    };
    let value =
        serde_json::to_value(&response).map_err(|_| SpreadApiError::Internal(request_id))?;
    state
        .report_cache
        .write()
        .await
        .insert((workspace, trade_date), (version, value.clone()));
    Ok(value)
}

#[utoipa::path(
    put,
    path = "/api/v1/overview/report/levels",
    security(("session_cookie" = [])),
    responses((status = 204), (status = 400), (status = 401), (status = 403))
)]
pub async fn save_overview_report_levels(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Json(request): Json<SaveReportLevelsRequest>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = write_context(
        &state,
        &headers,
        Permission::ManageOverviewReport,
        request_id,
    )
    .await?;
    let trade_date = parse_trade_date(request.trade_date.trim(), request_id)?;
    // 形状校验挡在这里，不指望数据库的 check——那条只守住「是个带 rows 的对象」。
    // 存进去一个畸形网格，界面会渲染成一张残表，而看上去像是数据没到。
    let rows = request
        .cells
        .get("rows")
        .and_then(Value::as_array)
        .ok_or(SpreadApiError::Validation("invalid_levels", request_id))?;
    if rows.len() > 32 {
        return Err(SpreadApiError::Validation("invalid_levels", request_id));
    }
    for row in rows {
        let key = row.get("key").and_then(Value::as_str).unwrap_or("");
        if key.is_empty() || key.len() > 40 {
            return Err(SpreadApiError::Validation("invalid_levels", request_id));
        }
        let values = row
            .get("values")
            .and_then(Value::as_array)
            .ok_or(SpreadApiError::Validation("invalid_levels", request_id))?;
        if values.len() > 8 || values.iter().any(|value| !value.is_string()) {
            return Err(SpreadApiError::Validation("invalid_levels", request_id));
        }
        if values
            .iter()
            .any(|value| value.as_str().map(str::len).unwrap_or(0) > 40)
        {
            return Err(SpreadApiError::Validation("invalid_levels", request_id));
        }
    }
    database::spread_analytics::save_report_levels(
        &state.auth.pool,
        context.workspace_id(),
        trade_date,
        &request.cells,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    // 压力位进了响应上半,保存后整体失效缓存,立即可见。
    state.report_cache.write().await.clear();
    Ok(StatusCode::NO_CONTENT.into_response())
}

#[utoipa::path(
    put,
    path = "/api/v1/overview/report/seat-groups",
    security(("session_cookie" = [])),
    responses((status = 204), (status = 400), (status = 401), (status = 403))
)]
pub async fn save_overview_report_seat_groups(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Json(request): Json<SaveReportSeatGroupsRequest>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = write_context(
        &state,
        &headers,
        Permission::ManageOverviewReport,
        request_id,
    )
    .await?;
    let mut groups = Vec::new();
    for group in request.groups {
        if !DEFAULT_SEAT_GROUPS
            .iter()
            .any(|(key, _)| *key == group.group_key)
        {
            return Err(SpreadApiError::Validation("invalid_group_key", request_id));
        }
        // 名单是拿去当 SQL 参数比对的，不会注入；这里挡的是「一组塞进几百家」
        // 把报告表撑成一屏滚不完的东西，以及空名字（匹配不到却在表上占一行）。
        if group.members.len() > 20 {
            return Err(SpreadApiError::Validation("too_many_members", request_id));
        }
        let mut members = Vec::new();
        for member in group.members {
            let name = member.trim().to_string();
            validate_text(&name, 1, 40)
                .map_err(|code| SpreadApiError::Validation(code, request_id))?;
            if !members.contains(&name) {
                members.push(name);
            }
        }
        groups.push((group.group_key, members));
    }
    database::spread_analytics::save_report_seat_groups(
        &state.auth.pool,
        context.workspace_id(),
        &groups,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    // 席位组决定响应下半的全部行,保存后整体失效缓存。
    state.report_cache.write().await.clear();
    Ok(StatusCode::NO_CONTENT.into_response())
}

/// 一个交易日的到齐情况。
#[derive(Debug, Serialize, ToSchema)]
pub struct DataHealthDay {
    pub trade_date: String,
    pub exchanges: Vec<String>,
    /// 各所数据首次入库时刻(北京时间 HH:MM),键=交易所代码。装载侧自
    /// 2026-08-16 起 upsert 不再刷新 loaded_at,此值即采集源到达时刻画像。
    pub arrivals: std::collections::BTreeMap<String, String>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct DataHealthResponse {
    /// 近期出现过的交易所全集，按代码排序。界面拿它当「该有几家」的基准——
    /// 从数据里推，不写死名单。
    pub expected_exchanges: Vec<String>,
    /// 最近若干个交易日的席位数据，最新的在前。
    pub seats: Vec<DataHealthDay>,
    /// 同上，行情数据。
    pub prices: Vec<DataHealthDay>,
}

fn to_health_days(rows: Vec<database::spread_analytics::DataFreshnessDay>) -> Vec<DataHealthDay> {
    rows.into_iter()
        .map(|row| DataHealthDay {
            trade_date: row.trade_date.to_string(),
            exchanges: row
                .exchanges
                .split(',')
                .filter(|item| !item.is_empty())
                .map(str::to_string)
                .collect(),
            arrivals: row
                .arrivals
                .split(',')
                .filter_map(|item| {
                    item.split_once('@')
                        .map(|(code, at)| (code.to_string(), at.to_string()))
                })
                .collect(),
        })
        .collect()
}

#[utoipa::path(
    get,
    path = "/api/v1/overview/data-health",
    security(("session_cookie" = [])),
    responses((status = 200, body = DataHealthResponse), (status = 401), (status = 500))
)]
pub async fn query_data_health(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = read_context(&state, &headers, request_id).await?;

    let (seats, prices) =
        database::spread_analytics::data_freshness(&state.auth.pool, context.workspace_id())
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?;

    let seats = to_health_days(seats);
    let prices = to_health_days(prices);
    let mut expected: Vec<String> = seats
        .iter()
        .chain(prices.iter())
        .flat_map(|day| day.exchanges.iter().cloned())
        .collect();
    expected.sort();
    expected.dedup();

    Ok(Json(ApiResponse::new(
        DataHealthResponse {
            expected_exchanges: expected,
            seats,
            prices,
        },
        request_id,
    ))
    .into_response())
}

fn parse_decimal(value: &str) -> Decimal {
    value.parse().unwrap_or(Decimal::ZERO)
}

/// 把请求里的两条腿变成品种与前后月。
///
/// 前腿是先到期的那条，价差就是它减去后腿——运营者定的口径。组合的命名本身
/// 已经表达了这个顺序（09-01 是九月腿在前），所以这里按 leg1/leg2 取，
/// 真正的腿序校验仍由 `calculate_windowed_analytics` 做，不在这里重复一遍。
fn own_engine_legs(request: &FreeSpreadQueryRequest) -> Option<(String, u8, u8)> {
    let instrument = request.leg1.symbol.trim().to_ascii_uppercase();
    if instrument.is_empty() || instrument != request.leg2.symbol.trim().to_ascii_uppercase() {
        // 两条腿必须同品种：跨品种价差不是这个引擎在算的东西。
        return None;
    }
    let front = request.leg1.month.trim().parse::<u8>().ok()?;
    let back = request.leg2.month.trim().parse::<u8>().ok()?;
    if !(1..=12).contains(&front) || !(1..=12).contains(&back) || front == back {
        return None;
    }
    Some((instrument, front, back))
}

#[utoipa::path(
    post,
    path = "/api/v1/spread-analytics/free-spread/query",
    params(
        ("x-csrf-token" = String, Header),
        ("Origin" = String, Header)
    ),
    request_body = FreeSpreadQueryRequest,
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = FreeSpreadQueryResponse),
        (status = 400, body = SpreadErrorBody),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody),
        (status = 502, body = SpreadErrorBody),
        (status = 503, body = SpreadErrorBody)
    )
)]
pub async fn query_free_spread(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Json(request): Json<FreeSpreadQueryRequest>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = write_context(&state, &headers, Permission::ReadSpreads, request_id).await?;
    validate_query(&request).map_err(|code| SpreadApiError::Validation(code, request_id))?;
    let use_own_engine = request.provider.trim() == SELF_PROVIDER_CODE;
    if !use_own_engine {
        validate_provider_selection(&state, &request.leg1, &request.leg2, request_id).await?;
        database::spread_analytics::ensure_sanhe_source(&state.auth.pool, context.workspace_id())
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?;
    }
    let query_json = canonical_query(&request);
    let query_hash = sha256_json(&query_json);
    let fetched = if use_own_engine {
        load_own_series(&state, &request, context.workspace_id(), request_id).await?
    } else {
        load_series(&state, &request, request_id).await?
    };
    let mut codes = BTreeSet::new();
    for point in &fetched.data.points {
        codes.insert(point.from_code.to_ascii_uppercase());
        codes.insert(point.to_code.to_ascii_uppercase());
    }
    let codes: Vec<_> = codes.into_iter().collect();
    let contracts = database::spread_analytics::resolve_contract_windows(
        &state.auth.pool,
        context.workspace_id(),
        &codes,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    let data_cutoff_at = fetched.data.points.last().map(|point| point.trade_date);
    let analytics = calculate_windowed_analytics(&fetched.data.points, &contracts, data_cutoff_at)
        .map_err(|_| {
            SpreadApiError::Provider(SpreadProviderErrorKind::ContractChanged, None, request_id)
        })?;
    let derivation_hash = sha256_json(&json!({
        "payload_hash": &fetched.payload_hash,
        "window_algorithm_version": WINDOW_ALGORITHM_VERSION,
        "statistics_algorithm_version": STATISTICS_ALGORITHM_VERSION,
        "rule_version": DEFAULT_RULE_VERSION,
        "segments": &analytics.segments,
    }));
    let series_id = database::spread_analytics::save_series(
        &state.auth.pool,
        &SeriesPersistence {
            workspace_id: context.workspace_id(),
            actor_user_id: context.user_id(),
            request_id,
            query_hash: &query_hash,
            business_date: business_date(),
            query_json: &query_json,
            fetched_at: fetched.fetched_at,
            data_cutoff_at,
            payload_hash: &fetched.payload_hash,
            derivation_hash: &derivation_hash,
            analytics: &analytics,
            own_engine: use_own_engine,
        },
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    Ok(Json(ApiResponse::new(
        response_from_analytics(
            series_id,
            &request,
            fetched.fetched_at,
            data_cutoff_at,
            analytics,
            use_own_engine,
        ),
        request_id,
    ))
    .into_response())
}

/// 用我们自己的行情算价差，形状与三禾那条完全一样，好让下游一个字都不用改。
async fn load_own_series(
    state: &SpreadAnalyticsState,
    request: &FreeSpreadQueryRequest,
    workspace_id: Uuid,
    request_id: Uuid,
) -> Result<CachedFetch<ProviderSeries>, SpreadApiError> {
    let (instrument, front, back) = own_engine_legs(request).ok_or(SpreadApiError::Validation(
        "invalid_leg_selection",
        request_id,
    ))?;
    let rows = database::spread_analytics::load_own_spread_points(
        &state.auth.pool,
        workspace_id,
        &instrument,
        front,
        back,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;
    let points: Vec<_> = rows
        .into_iter()
        .filter_map(|row| {
            // 文本转精确小数；转不动的一行宁可丢掉，也不要把一个近似值
            // 混进以后要拿来算钱的序列里。
            row.value.parse().ok().map(|value| RawSpreadPoint {
                trade_date: row.trade_date,
                value,
                from_code: row.front,
                to_code: row.back,
            })
        })
        .collect();
    let fetched_at = OffsetDateTime::now_utc();
    let payload_hash = sha256_json(&json!({
        "engine": SELF_SOURCE_CODE,
        "instrument": instrument,
        "front_month": front,
        "back_month": back,
        "point_count": points.len(),
        "last_trade_date": points.last().map(|point| point.trade_date.to_string()),
    }));
    Ok(CachedFetch {
        data: ProviderSeries { points },
        fetched_at,
        payload_hash,
        result_kind: ProviderResultKind::Ok,
    })
}

#[utoipa::path(
    get,
    path = "/api/v1/spread-analytics/favorites",
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = Vec<FavoriteResponse>),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody)
    )
)]
pub async fn list_favorites(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = read_context(&state, &headers, request_id).await?;
    let items: Vec<FavoriteResponse> =
        database::spread_analytics::list_favorites(&state.auth.pool, context.workspace_id())
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?
            .into_iter()
            .map(favorite_response)
            .collect();
    Ok(Json(ApiResponse::new(items, request_id)).into_response())
}

#[utoipa::path(
    post,
    path = "/api/v1/spread-analytics/favorites",
    params(
        ("x-csrf-token" = String, Header),
        ("Origin" = String, Header)
    ),
    request_body = CreateFavoriteRequest,
    security(("session_cookie" = [])),
    responses(
        (status = 201, body = FavoriteResponse),
        (status = 400, body = SpreadErrorBody),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody),
        (status = 409, body = SpreadErrorBody)
    )
)]
pub async fn create_favorite(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Json(request): Json<CreateFavoriteRequest>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = write_context(
        &state,
        &headers,
        Permission::ManageSpreadFavorites,
        request_id,
    )
    .await?;
    validate_favorite(&request).map_err(|code| SpreadApiError::Validation(code, request_id))?;
    let provider = request.provider.trim().to_string();
    // 自研那条不去问三禾：腿合不合法要拿我们自己的品种和月份来判，
    // 而不是让一个已经不用的上游来决定能不能收藏。
    if provider == SELF_PROVIDER_CODE {
        validate_own_selection(&state, context.workspace_id(), &request.leg1, request_id).await?;
        validate_own_selection(&state, context.workspace_id(), &request.leg2, request_id).await?;
    } else {
        validate_provider_selection(&state, &request.leg1, &request.leg2, request_id).await?;
    }
    let normalized = json!({
        "provider": &provider,
        "leg1": canonical_leg(&request.leg1),
        "leg2": canonical_leg(&request.leg2),
    });
    let favorite = database::spread_analytics::create_favorite(
        &state.auth.pool,
        &NewFavorite {
            workspace_id: context.workspace_id(),
            provider_code: &provider,
            actor_user_id: context.user_id(),
            request_id,
            name: request.name.trim(),
            leg1: &favorite_leg(&request.leg1),
            leg2: &favorite_leg(&request.leg2),
            normalized_hash: &sha256_json(&normalized),
        },
    )
    .await
    .map_err(|error| match error {
        SpreadRepositoryError::FavoriteConflict => {
            SpreadApiError::Conflict("favorite_exists", request_id)
        }
        _ => SpreadApiError::Internal(request_id),
    })?;
    Ok((
        StatusCode::CREATED,
        Json(ApiResponse::new(favorite_response(favorite), request_id)),
    )
        .into_response())
}

#[utoipa::path(
    delete,
    path = "/api/v1/spread-analytics/favorites/{favorite_id}",
    params(
        ("favorite_id" = Uuid, Path),
        ("x-csrf-token" = String, Header),
        ("Origin" = String, Header)
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 204),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody),
        (status = 404, body = SpreadErrorBody)
    )
)]
pub async fn delete_favorite(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Path(favorite_id): Path<Uuid>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = write_context(
        &state,
        &headers,
        Permission::ManageSpreadFavorites,
        request_id,
    )
    .await?;
    database::spread_analytics::delete_favorite(
        &state.auth.pool,
        context.workspace_id(),
        context.user_id(),
        request_id,
        favorite_id,
    )
    .await
    .map_err(|error| match error {
        SpreadRepositoryError::FavoriteNotFound => {
            SpreadApiError::NotFound("favorite_not_found", request_id)
        }
        _ => SpreadApiError::Internal(request_id),
    })?;
    Ok(StatusCode::NO_CONTENT.into_response())
}

async fn read_context(
    state: &SpreadAnalyticsState,
    headers: &HeaderMap,
    request_id: Uuid,
) -> Result<auth::AuthContext, SpreadApiError> {
    let context = auth::current_context(&state.auth, headers)
        .await
        .map_err(|error| SpreadApiError::Auth(error, request_id))?;
    context
        .require_permission(Permission::ReadSpreads)
        .map_err(|error| SpreadApiError::Auth(error, request_id))?;
    Ok(context)
}

async fn write_context(
    state: &SpreadAnalyticsState,
    headers: &HeaderMap,
    permission: Permission,
    request_id: Uuid,
) -> Result<auth::AuthContext, SpreadApiError> {
    let context = auth::current_context(&state.auth, headers)
        .await
        .map_err(|error| SpreadApiError::Auth(error, request_id))?;
    auth::ensure_allowed_origin(&state.auth.config, headers)
        .map_err(|error| SpreadApiError::Auth(error, request_id))?;
    auth::ensure_csrf(&state.auth, headers)
        .await
        .map_err(|error| SpreadApiError::Auth(error, request_id))?;
    context
        .require_permission(permission)
        .map_err(|error| SpreadApiError::Auth(error, request_id))?;
    Ok(context)
}

async fn load_varieties(
    state: &SpreadAnalyticsState,
    request_id: Uuid,
) -> Result<CachedFetch<Vec<ProviderVariety>>, SpreadApiError> {
    let endpoint = ProviderEndpoint::AllVarieties;
    let parameters = json!({});
    let parameter_hash = sha256_json(&parameters);
    if let Some(cache) = database::spread_analytics::get_cache(
        &state.auth.pool,
        endpoint,
        &parameter_hash,
        business_date(),
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?
    {
        let (data, result_kind) = SanheSpreadSeriesProvider::parse_varieties(&cache.payload)
            .map_err(|error| provider_error(error, request_id))?;
        return Ok(CachedFetch {
            data,
            fetched_at: cache.fetched_at,
            result_kind,
            payload_hash: cache.payload_hash,
        });
    }
    let mut fill_guard =
        database::spread_analytics::begin_cache_fill(&state.auth.pool, endpoint, &parameter_hash)
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?;
    if let Some(cache) = database::spread_analytics::get_cache_in_transaction(
        &mut fill_guard,
        endpoint,
        &parameter_hash,
        business_date(),
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?
    {
        let (data, result_kind) = SanheSpreadSeriesProvider::parse_varieties(&cache.payload)
            .map_err(|error| provider_error(error, request_id))?;
        fill_guard
            .commit()
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?;
        return Ok(CachedFetch {
            data,
            fetched_at: cache.fetched_at,
            result_kind,
            payload_hash: cache.payload_hash,
        });
    }
    guard_and_wait(state, endpoint, &parameter_hash, request_id).await?;
    let fetched = match state.provider.list_varieties().await {
        Ok(fetched) => fetched,
        Err(error) => {
            return serve_stored_after_provider_error(
                state,
                endpoint,
                &parameter_hash,
                error,
                request_id,
                SanheSpreadSeriesProvider::parse_varieties,
            )
            .await;
        }
    };
    let cache = store_fetch(
        state,
        endpoint,
        &parameter_hash,
        &parameters,
        &fetched.raw_payload,
        fetched.fetched_at,
        fetched.http_status,
        fetched.business_code,
        fetched.result_kind,
        request_id,
    )
    .await?;
    fill_guard
        .commit()
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?;
    let (data, result_kind) = SanheSpreadSeriesProvider::parse_varieties(&cache.payload)
        .map_err(|error| provider_error(error, request_id))?;
    Ok(CachedFetch {
        data,
        fetched_at: cache.fetched_at,
        result_kind,
        payload_hash: cache.payload_hash,
    })
}

async fn validate_provider_selection(
    state: &SpreadAnalyticsState,
    leg1: &FreeSpreadLeg,
    leg2: &FreeSpreadLeg,
    request_id: Uuid,
) -> Result<(), SpreadApiError> {
    let varieties = load_varieties(state, request_id).await?.data;
    for leg in [leg1, leg2] {
        let valid = varieties.iter().any(|item| {
            item.name == leg.variety.trim() && item.symbol.eq_ignore_ascii_case(leg.symbol.trim())
        });
        if !valid {
            return Err(SpreadApiError::Validation(
                "provider_selection_invalid",
                request_id,
            ));
        }
        let available = load_months(state, leg.variety.trim(), request_id)
            .await?
            .data
            .months;
        if !available.iter().any(|month| month == &leg.month) {
            return Err(SpreadApiError::Validation(
                "provider_selection_invalid",
                request_id,
            ));
        }
    }
    Ok(())
}

async fn load_months(
    state: &SpreadAnalyticsState,
    variety: &str,
    request_id: Uuid,
) -> Result<CachedFetch<ProviderContractMonths>, SpreadApiError> {
    let endpoint = ProviderEndpoint::VarietyContracts;
    let parameters = json!({"variety": variety});
    let parameter_hash = sha256_json(&parameters);
    if let Some(cache) = database::spread_analytics::get_cache(
        &state.auth.pool,
        endpoint,
        &parameter_hash,
        business_date(),
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?
    {
        let (data, result_kind) =
            SanheSpreadSeriesProvider::parse_contract_months(variety, &cache.payload)
                .map_err(|error| provider_error(error, request_id))?;
        return Ok(CachedFetch {
            data,
            fetched_at: cache.fetched_at,
            result_kind,
            payload_hash: cache.payload_hash,
        });
    }
    let mut fill_guard =
        database::spread_analytics::begin_cache_fill(&state.auth.pool, endpoint, &parameter_hash)
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?;
    if let Some(cache) = database::spread_analytics::get_cache_in_transaction(
        &mut fill_guard,
        endpoint,
        &parameter_hash,
        business_date(),
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?
    {
        let (data, result_kind) =
            SanheSpreadSeriesProvider::parse_contract_months(variety, &cache.payload)
                .map_err(|error| provider_error(error, request_id))?;
        fill_guard
            .commit()
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?;
        return Ok(CachedFetch {
            data,
            fetched_at: cache.fetched_at,
            result_kind,
            payload_hash: cache.payload_hash,
        });
    }
    guard_and_wait(state, endpoint, &parameter_hash, request_id).await?;
    let fetched = match state.provider.list_contract_months(variety).await {
        Ok(fetched) => fetched,
        Err(error) => {
            return serve_stored_after_provider_error(
                state,
                endpoint,
                &parameter_hash,
                error,
                request_id,
                |payload| SanheSpreadSeriesProvider::parse_contract_months(variety, payload),
            )
            .await;
        }
    };
    let cache = store_fetch(
        state,
        endpoint,
        &parameter_hash,
        &parameters,
        &fetched.raw_payload,
        fetched.fetched_at,
        fetched.http_status,
        fetched.business_code,
        fetched.result_kind,
        request_id,
    )
    .await?;
    fill_guard
        .commit()
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?;
    let (data, result_kind) =
        SanheSpreadSeriesProvider::parse_contract_months(variety, &cache.payload)
            .map_err(|error| provider_error(error, request_id))?;
    Ok(CachedFetch {
        data,
        fetched_at: cache.fetched_at,
        result_kind,
        payload_hash: cache.payload_hash,
    })
}

async fn load_series(
    state: &SpreadAnalyticsState,
    request: &FreeSpreadQueryRequest,
    request_id: Uuid,
) -> Result<CachedFetch<ProviderSeries>, SpreadApiError> {
    let endpoint = ProviderEndpoint::ArbitrageVarieties;
    let parameters = canonical_query(request);
    let parameter_hash = sha256_json(&parameters);
    if let Some(cache) = database::spread_analytics::get_cache(
        &state.auth.pool,
        endpoint,
        &parameter_hash,
        business_date(),
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?
    {
        let (data, result_kind) = SanheSpreadSeriesProvider::parse_series(&cache.payload)
            .map_err(|error| provider_error(error, request_id))?;
        return Ok(CachedFetch {
            data,
            fetched_at: cache.fetched_at,
            result_kind,
            payload_hash: cache.payload_hash,
        });
    }
    let mut fill_guard =
        database::spread_analytics::begin_cache_fill(&state.auth.pool, endpoint, &parameter_hash)
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?;
    if let Some(cache) = database::spread_analytics::get_cache_in_transaction(
        &mut fill_guard,
        endpoint,
        &parameter_hash,
        business_date(),
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?
    {
        let (data, result_kind) = SanheSpreadSeriesProvider::parse_series(&cache.payload)
            .map_err(|error| provider_error(error, request_id))?;
        fill_guard
            .commit()
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?;
        return Ok(CachedFetch {
            data,
            fetched_at: cache.fetched_at,
            result_kind,
            payload_hash: cache.payload_hash,
        });
    }
    guard_and_wait(state, endpoint, &parameter_hash, request_id).await?;
    let fetched = match state
        .provider
        .load_series(
            &request.leg1.variety,
            &request.leg1.month,
            &request.leg2.variety,
            &request.leg2.month,
        )
        .await
    {
        Ok(fetched) => fetched,
        Err(error) => {
            return serve_stored_after_provider_error(
                state,
                endpoint,
                &parameter_hash,
                error,
                request_id,
                SanheSpreadSeriesProvider::parse_series,
            )
            .await;
        }
    };
    let cache = store_fetch(
        state,
        endpoint,
        &parameter_hash,
        &parameters,
        &fetched.raw_payload,
        fetched.fetched_at,
        fetched.http_status,
        fetched.business_code,
        fetched.result_kind,
        request_id,
    )
    .await?;
    fill_guard
        .commit()
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?;
    let (data, result_kind) = SanheSpreadSeriesProvider::parse_series(&cache.payload)
        .map_err(|error| provider_error(error, request_id))?;
    Ok(CachedFetch {
        data,
        fetched_at: cache.fetched_at,
        result_kind,
        payload_hash: cache.payload_hash,
    })
}

async fn guard_and_wait(
    state: &SpreadAnalyticsState,
    endpoint: ProviderEndpoint,
    parameter_hash: &str,
    request_id: Uuid,
) -> Result<(), SpreadApiError> {
    if let Some(failure) =
        database::spread_analytics::active_failure(&state.auth.pool, endpoint, parameter_hash)
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?
    {
        return Err(SpreadApiError::Provider(
            stable_kind(&failure.stable_error_code),
            Some(failure.retry_after_seconds),
            request_id,
        ));
    }
    let wait = database::spread_analytics::reserve_request_slot(&state.auth.pool)
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?;
    if !wait.is_zero() {
        tokio::time::sleep(wait).await;
    }
    Ok(())
}

fn provider_error(error: SpreadProviderError, request_id: Uuid) -> SpreadApiError {
    SpreadApiError::Provider(error.kind, error.retry_after_seconds, request_id)
}

/// Serve a previously stored payload when the upstream refuses.
///
/// Applied to every sanhe endpoint, not only the series one: the varieties list
/// is fetched first, so a page that could fall back on a stored series but not
/// on a stored variety list would still be unusable in exactly the outage the
/// fallback exists for.
///
/// The failure is recorded either way. What is served is whatever was last
/// stored, carrying its own fetch time, which the page prints — so the answer
/// may be old but is never presented as current. If nothing was ever stored
/// there is nothing to serve and the original error stands.
async fn serve_stored_after_provider_error<T, F>(
    state: &SpreadAnalyticsState,
    endpoint: ProviderEndpoint,
    parameter_hash: &str,
    error: SpreadProviderError,
    request_id: Uuid,
    parse: F,
) -> Result<CachedFetch<T>, SpreadApiError>
where
    F: Fn(&serde_json::Value) -> Result<(T, ProviderResultKind), SpreadProviderError>,
{
    let recorded = record_provider_error(state, endpoint, parameter_hash, error, request_id).await;
    let stored =
        database::spread_analytics::get_latest_cache(&state.auth.pool, endpoint, parameter_hash)
            .await
            .map_err(|error| {
                warn!(
                    request_id = %request_id,
                    endpoint = endpoint.code(),
                    parameter_hash,
                    %error,
                    "could not read a stored payload to serve after the provider refused"
                );
                SpreadApiError::Internal(request_id)
            })?;
    let Some(stored) = stored else {
        return Err(recorded);
    };
    let (data, result_kind) =
        parse(&stored.payload).map_err(|error| provider_error(error, request_id))?;
    warn!(
        request_id = %request_id,
        provider = SANHE_PROVIDER_CODE,
        endpoint = endpoint.code(),
        parameter_hash,
        served_fetched_at = %stored.fetched_at,
        "serving a stored payload because the provider refused"
    );
    Ok(CachedFetch {
        data,
        fetched_at: stored.fetched_at,
        result_kind,
        payload_hash: stored.payload_hash,
    })
}

async fn record_provider_error(
    state: &SpreadAnalyticsState,
    endpoint: ProviderEndpoint,
    parameter_hash: &str,
    error: SpreadProviderError,
    request_id: Uuid,
) -> SpreadApiError {
    let retry_after = error.retry_after_seconds.unwrap_or(60).max(60);
    warn!(
        request_id = %request_id,
        provider = SANHE_PROVIDER_CODE,
        endpoint = endpoint.code(),
        parameter_hash,
        stable_error_code = error.kind.stable_code(),
        retry_after_seconds = retry_after,
        "spread provider request failed"
    );
    if database::spread_analytics::record_failure(
        &state.auth.pool,
        endpoint,
        parameter_hash,
        error.kind.stable_code(),
        retry_after,
    )
    .await
    .is_err()
    {
        return SpreadApiError::Internal(request_id);
    }
    SpreadApiError::Provider(error.kind, Some(retry_after), request_id)
}

#[allow(clippy::too_many_arguments)]
async fn store_fetch(
    state: &SpreadAnalyticsState,
    endpoint: ProviderEndpoint,
    parameter_hash: &str,
    parameters: &Value,
    payload: &Value,
    fetched_at: OffsetDateTime,
    http_status: u16,
    business_code: i64,
    result_kind: ProviderResultKind,
    request_id: Uuid,
) -> Result<database::spread_analytics::CachedProviderPayload, SpreadApiError> {
    let payload_hash = sha256_json(payload);
    database::spread_analytics::store_cache(
        &state.auth.pool,
        &NewProviderCache {
            endpoint,
            parameter_hash,
            parameters,
            business_date: business_date(),
            fetched_at,
            http_status,
            business_code,
            payload,
            result_kind: result_kind.as_str(),
            payload_hash: &payload_hash,
        },
    )
    .await
    .map_err(|error| {
        // A 500 whose cause is discarded cannot be diagnosed from production.
        // That is not hypothetical: an acceptance run failed here and the only
        // evidence left was the words "internal error".
        warn!(
            request_id = %request_id,
            provider = SANHE_PROVIDER_CODE,
            endpoint = endpoint.code(),
            parameter_hash,
            %error,
            "could not store the provider payload"
        );
        SpreadApiError::Internal(request_id)
    })
}

fn response_from_analytics(
    series_id: Uuid,
    request: &FreeSpreadQueryRequest,
    fetched_at: OffsetDateTime,
    data_cutoff_at: Option<Date>,
    analytics: WindowedSpreadAnalytics,
    own_engine: bool,
) -> FreeSpreadQueryResponse {
    let WindowedSpreadAnalytics {
        continuous_points,
        segment_boundaries,
        segments,
        seasonal,
        monthly,
        current_value,
        quality,
        ..
    } = analytics;
    let (provider_code, source_code, price_basis) = if own_engine {
        // 自建口径：当天两腿收盘价相减，先到期的减后到期的。
        (SELF_PROVIDER_CODE, SELF_SOURCE_CODE, "own_close_difference")
    } else {
        (SANHE_PROVIDER_CODE, SANHE_SOURCE_CODE, "upstream_spread")
    };
    let trace = AnalysisTrace {
        provider: provider_code.to_string(),
        source_code: source_code.to_string(),
        data_cutoff_at,
        price_basis: price_basis.to_string(),
        sample_start: continuous_points.first().map(|point| point.trade_date),
        sample_end: continuous_points.last().map(|point| point.trade_date),
        sample_count: u32::try_from(continuous_points.len()).unwrap_or(u32::MAX),
        excluded_point_count: quality.excluded_point_count,
        calendar_version_ids: segments
            .iter()
            .flat_map(|segment| segment.calendar_version_ids.iter().copied())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect(),
        window_algorithm_version: WINDOW_ALGORITHM_VERSION.to_string(),
        statistics_algorithm_version: STATISTICS_ALGORITHM_VERSION.to_string(),
        rule_version: DEFAULT_RULE_VERSION.to_string(),
    };
    FreeSpreadQueryResponse {
        series_id,
        source: source_metadata(fetched_at, data_cutoff_at, own_engine),
        query: FreeSpreadQueryEcho {
            provider: SANHE_PROVIDER_CODE.to_string(),
            leg1: request.leg1.clone(),
            leg2: request.leg2.clone(),
        },
        quality,
        algorithm_versions: AlgorithmVersions {
            provider: SANHE_PROVIDER_ALGORITHM_VERSION.to_string(),
            window: WINDOW_ALGORITHM_VERSION.to_string(),
            statistics: STATISTICS_ALGORITHM_VERSION.to_string(),
            rule: DEFAULT_RULE_VERSION.to_string(),
        },
        continuous_series: ContinuousSeriesResponse {
            trace: trace.clone(),
            points: continuous_points,
            segment_boundaries,
            current_value,
        },
        seasonal_series: SeasonalSeriesResponse {
            trace: trace.clone(),
            axis: seasonal.axis,
            years: seasonal.years,
            current_year: seasonal.current_year,
        },
        monthly_matrix: MonthlyMatrixResponse {
            trace,
            years: monthly.years,
            up_ratios: monthly.up_ratios,
        },
        segments,
    }
}

fn source_metadata(
    fetched_at: OffsetDateTime,
    data_cutoff_at: Option<Date>,
    own_engine: bool,
) -> SourceMetadata {
    if own_engine {
        return SourceMetadata {
            provider: SELF_PROVIDER_CODE.to_string(),
            source_code: SELF_SOURCE_CODE.to_string(),
            source_display_name: SELF_SOURCE_NAME.to_string(),
            source_type: "derived".to_string(),
            fetched_at,
            data_cutoff_at,
            price_basis: "own_close_difference".to_string(),
            // 两条腿的收盘价就在我们自己的表里，与三禾只给算好的价差不同。
            raw_leg_prices_available: true,
            provider_algorithm_version: WINDOW_ALGORITHM_VERSION.to_string(),
        };
    }
    SourceMetadata {
        provider: SANHE_PROVIDER_CODE.to_string(),
        source_code: SANHE_SOURCE_CODE.to_string(),
        source_display_name: SANHE_SOURCE_DISPLAY_NAME.to_string(),
        source_type: "aggregator".to_string(),
        fetched_at,
        data_cutoff_at,
        price_basis: "upstream_spread".to_string(),
        raw_leg_prices_available: false,
        provider_algorithm_version: SANHE_PROVIDER_ALGORITHM_VERSION.to_string(),
    }
}

/// Thin seams for the warming job, so it drives the same cache, throttle and
/// failure-recording path a real request does instead of a parallel one.
pub async fn warm_contract_months(
    state: &SpreadAnalyticsState,
    variety: &str,
    request_id: Uuid,
) -> Result<Vec<String>, SpreadApiError> {
    Ok(load_months(state, variety, request_id).await?.data.months)
}

pub fn warm_parameter_hash(request: &FreeSpreadQueryRequest) -> String {
    sha256_json(&canonical_query(request))
}

/// Fetch and store one combination's series, discarding the payload.
///
/// The point is the write into the provider cache, which is what a refused
/// request later falls back on. Nothing derived is computed: the page rebuilds
/// all of that from the cached payload when someone actually looks.
pub async fn warm_one_combination(
    state: &SpreadAnalyticsState,
    request: &FreeSpreadQueryRequest,
    request_id: Uuid,
) -> Result<(), SpreadApiError> {
    load_series(state, request, request_id).await?;
    Ok(())
}

fn canonical_query(request: &FreeSpreadQueryRequest) -> Value {
    json!({
        "variety1": request.leg1.variety.trim(),
        "code1": request.leg1.month,
        "variety2": request.leg2.variety.trim(),
        "code2": request.leg2.month,
    })
}

fn canonical_leg(leg: &FreeSpreadLeg) -> Value {
    json!({
        "variety": leg.variety.trim(),
        "symbol": leg.symbol.trim().to_ascii_uppercase(),
        "month": leg.month,
    })
}

fn favorite_leg(leg: &FreeSpreadLeg) -> FavoriteLeg {
    FavoriteLeg {
        variety: leg.variety.trim().to_string(),
        symbol: leg.symbol.trim().to_ascii_uppercase(),
        month: leg.month.clone(),
    }
}

fn favorite_response(record: database::spread_analytics::FavoriteRecord) -> FavoriteResponse {
    FavoriteResponse {
        id: record.id,
        name: record.name,
        provider: record.provider,
        leg1: FreeSpreadLeg {
            variety: record.leg1.variety,
            symbol: record.leg1.symbol,
            month: record.leg1.month,
        },
        leg2: FreeSpreadLeg {
            variety: record.leg2.variety,
            symbol: record.leg2.symbol,
            month: record.leg2.month,
        },
        created_at: record.created_at,
    }
}

/// 请求里允许出现的来源。三禾保留是为了能随时切回去比对，见 `DEC-046`。
fn is_known_provider(provider: &str) -> bool {
    matches!(provider, SANHE_PROVIDER_CODE | SELF_PROVIDER_CODE)
}

fn validate_query(request: &FreeSpreadQueryRequest) -> Result<(), &'static str> {
    if !is_known_provider(request.provider.trim()) {
        return Err("provider_invalid");
    }
    validate_leg(&request.leg1)?;
    validate_leg(&request.leg2)?;
    Ok(())
}

fn validate_favorite(request: &CreateFavoriteRequest) -> Result<(), &'static str> {
    if !is_known_provider(request.provider.trim()) {
        return Err("provider_invalid");
    }
    validate_text(&request.name, 1, 80)?;
    validate_leg(&request.leg1)?;
    validate_leg(&request.leg2)
}

fn validate_leg(leg: &FreeSpreadLeg) -> Result<(), &'static str> {
    validate_text(&leg.variety, 1, 40)?;
    validate_text(&leg.symbol, 1, 12)?;
    if !leg
        .symbol
        .chars()
        .all(|character| character.is_ascii_alphanumeric())
    {
        return Err("symbol_invalid");
    }
    if leg.month.len() != 2
        || !leg
            .month
            .chars()
            .all(|character| character.is_ascii_digit())
        || !("01"..="12").contains(&leg.month.as_str())
    {
        return Err("month_invalid");
    }
    Ok(())
}

fn validate_text(value: &str, min: usize, max: usize) -> Result<(), &'static str> {
    let length = value.trim().chars().count();
    if length < min || length > max || value.chars().any(char::is_control) {
        Err("text_invalid")
    } else {
        Ok(())
    }
}

fn sha256_json(value: &Value) -> String {
    let bytes = serde_json::to_vec(value).unwrap_or_default();
    format!("{:x}", Sha256::digest(bytes))
}

fn business_date() -> Date {
    OffsetDateTime::now_utc()
        .to_offset(UtcOffset::from_hms(8, 0, 0).expect("valid CST offset"))
        .date()
}

fn stable_kind(code: &str) -> SpreadProviderErrorKind {
    match code {
        "spread_provider_rate_limited" => SpreadProviderErrorKind::RateLimited,
        "spread_provider_forbidden" => SpreadProviderErrorKind::Forbidden,
        "spread_provider_contract_changed" => SpreadProviderErrorKind::ContractChanged,
        _ => SpreadProviderErrorKind::Unavailable,
    }
}

#[cfg(test)]
mod monitor_tests {
    use super::*;

    fn track(position: f64, threshold: f64) -> SpreadMonitorTrack {
        SpreadMonitorTrack {
            low: "0".into(),
            high: "1".into(),
            position: Some(position.to_string()),
            days: Some(200),
            alert: monitor_alert(Some(position), threshold).map(str::to_string),
        }
    }

    #[test]
    fn the_threshold_is_applied_at_read_time_not_baked_in() {
        // 同一个位置，阈值不同结论不同——这正是「存位置不存结论」换来的自由度。
        assert_eq!(monitor_alert(Some(0.85), 0.10), None);
        assert_eq!(monitor_alert(Some(0.85), 0.20), Some("high"));
        assert_eq!(monitor_alert(Some(0.15), 0.10), None);
        assert_eq!(monitor_alert(Some(0.15), 0.20), Some("low"));
    }

    #[test]
    fn a_position_outside_the_band_is_a_stronger_alert_not_a_missing_one() {
        // 历年轨用的是第 2.5 / 97.5 百分位，当前价差可以落在区间之外：位置为负或
        // 大于 1。那比「贴着边」更极端，绝不能因为不在 [0,1] 里就漏报。
        assert_eq!(monitor_alert(Some(-0.014), 0.10), Some("low"));
        assert_eq!(monitor_alert(Some(1.332), 0.10), Some("high"));
    }

    #[test]
    fn when_the_two_tracks_disagree_the_more_extreme_one_wins() {
        // 生产实例：焦煤 JM2609−JM2701 在 2026-08-11 是当年 95.1%、历年 16.1%。
        // 在 20% 阈值下两条都触发且方向相反；离中线的距离 0.451 对 0.339，
        // 所以报「当年高位」。
        //
        // 这条测试存在的理由：原来那版用 max_by 配了个恒等比较器，等于随便挑第一条，
        // 有一半机会把方向说反——而页面上看不出它挑错了。
        let pair = track(0.951, 0.20);
        let years = track(0.161, 0.20);
        assert_eq!(pair.alert.as_deref(), Some("high"));
        assert_eq!(years.alert.as_deref(), Some("low"));
        assert_eq!(combined_alert(&pair, Some(&years), 0.20), Some("high"));

        // 同一组数据在默认的 10% 阈值下只有当年那条触发——16.1% 够不着 10% 的低位带。
        // 写在这里是为了记住：设计图里那些「历年低位」的说法用的是 20% 阈值。
        let pair_10 = track(0.951, 0.10);
        let years_10 = track(0.161, 0.10);
        assert_eq!(years_10.alert, None);
        assert_eq!(
            combined_alert(&pair_10, Some(&years_10), 0.10),
            Some("high")
        );

        // 反过来也要对：历年更极端时报历年那条。
        let pair_mild = track(0.88, 0.15);
        let years_wild = track(-0.20, 0.15);
        assert_eq!(
            combined_alert(&pair_mild, Some(&years_wild), 0.15),
            Some("low")
        );
    }

    #[test]
    fn the_shelf_ladder_marks_the_stop_on_the_far_side() {
        // LH2611−LH2705 @2026-08-19 的真实档位(库里跑出来的),现价差 −935。
        // 做空价差(high):下方全是目标,**上方最近的一档 −885 是止损**。
        let raw = r#"[
            {"level":-885,"lo":-885,"hi":-885,"touches":2},
            {"level":-1117,"lo":-1155,"hi":-1080,"touches":3},
            {"level":-1355,"lo":-1355,"hi":-1355,"touches":3},
            {"level":-1640,"lo":-1640,"hi":-1640,"touches":1}
        ]"#;
        let out = build_shelves(Some(raw), Some(-935.0), Some(94.4), Some(52), Some("high"));
        assert_eq!(out.len(), 4);
        assert_eq!(out[0].level, "-885");
        assert_eq!(out[0].offset, "50");
        assert_eq!(out[0].role, "stop");
        // 下方三档都是卖点侧,且离得越远概率越低。
        assert_eq!(out[1].role, "target");
        assert_eq!(out[3].role, "target");
        let p1 = out[1].reach_pct.expect("有 σ 与剩余天数就该有概率");
        let p3 = out[3].reach_pct.expect("同上");
        assert!(p1 > p3, "越远的档概率必须越低:{p1} vs {p3}");
        // 做多价差时角色整个翻过来:上方是目标,下方最近的一档是止损。
        let up = build_shelves(Some(raw), Some(-935.0), Some(94.4), Some(52), Some("low"));
        assert_eq!(up[0].role, "target");
        assert_eq!(up[1].role, "stop");
        assert_eq!(up[2].role, "");
    }

    #[test]
    fn shelves_survive_a_missing_sigma_and_an_old_row() {
        // 没有 σ 就没有 z 和概率,但档位与触碰次数照给——它们是库里的事实。
        let raw = r#"[{"level":-1355,"lo":-1355,"hi":-1355,"touches":3}]"#;
        let out = build_shelves(Some(raw), Some(-935.0), None, Some(52), Some("high"));
        assert_eq!(out.len(), 1);
        assert_eq!(out[0].touches, 3);
        assert!(out[0].z.is_none() && out[0].reach_pct.is_none());
        // 旧行(这一列还没算过)与坏 JSON 都给空数组,不 panic。
        assert!(build_shelves(None, Some(-935.0), Some(94.4), Some(52), Some("high")).is_empty());
        assert!(build_shelves(Some("坏的"), Some(-935.0), Some(94.4), Some(52), None).is_empty());
        // 没有交易侧就不派角色——那种行没有一笔要做的交易。
        let no_side = build_shelves(Some(raw), Some(-935.0), Some(94.4), Some(52), None);
        assert_eq!(no_side[0].role, "");
    }

    #[test]
    fn the_reach_curve_falls_with_distance_and_never_extrapolates() {
        for days in [10, 30, 60, 120] {
            let mut prev = 101.0;
            for step in 0..=40 {
                let z = f64::from(step) * 0.1;
                let p = reach_pct(z, days).expect("z ≥ 0 一定有值");
                assert!(p <= prev, "剩余 {days} 日、z={z} 处概率不单调:{p} > {prev}");
                prev = p;
            }
            // 超出格子取末端,不外推——外推没有依据。
            assert_eq!(reach_pct(3.0, days), reach_pct(9.9, days));
        }
        assert!(reach_pct(-0.1, 30).is_none());
    }

    #[test]
    fn the_traded_side_is_the_turn_side_not_the_alert_side() {
        // JM2612−JM2705 @2026-08-06 的真实形态:历年轨 3.6% 报**低位**,
        // 当年轨自 100% 退到 70.8% 拐**高位**。⚡ 是拐头给的,统计就得给高位侧
        // ——高位侧持到期 −45,判不合格,⚡ 该灭。原实现给低位侧(持到期 +45)
        // 直接放行了一笔做空,而其后两周价差涨了 83.5 点。
        assert_eq!(trade_side(Some("low"), Some("high")), Some("high"));
        assert_eq!(trade_side(Some("high"), Some("low")), Some("low"));
        // 两侧一致时谁优先都一样
        assert_eq!(trade_side(Some("high"), Some("high")), Some("high"));
        // 只报警没拐头:机会出现但没到上车点,按报警侧给数字,不会有 ⚡
        assert_eq!(trade_side(Some("low"), None), Some("low"));
        // 只拐头没报警:拐头行多半已退出报警带,这正是该看数字的时候
        assert_eq!(trade_side(None, Some("high")), Some("high"));
        assert_eq!(trade_side(None, None), None);
    }

    #[test]
    fn no_alert_anywhere_means_no_alert() {
        let pair = track(0.5, 0.10);
        let years = track(0.42, 0.10);
        assert_eq!(combined_alert(&pair, Some(&years), 0.10), None);
        assert_eq!(combined_alert(&pair, None, 0.10), None);
    }

    #[test]
    fn a_combination_without_a_years_track_still_works() {
        // 历年轨可能缺席：跨品种组合的历史年份不够，或该月份组合是头一年出现。
        // 缺席不该让整行消失。
        let pair = track(0.97, 0.10);
        assert_eq!(combined_alert(&pair, None, 0.10), Some("high"));
    }

    /// 段首日判定抽出来复算一遍：与 handler 里那段是同一条规则。
    fn new_alert(today: (f64, Option<f64>), prev: (Option<f64>, Option<f64>), thr: f64) -> bool {
        let alert = combined_alert_at(Some(today.0), today.1, thr);
        let has_prev = prev.0.is_some() || prev.1.is_some();
        alert.is_some() && has_prev && combined_alert_at(prev.0, prev.1, thr).is_none()
    }

    #[test]
    fn a_new_alert_is_one_that_was_not_there_yesterday() {
        // 昨天在区间中部、今天贴到下沿 —— 这才是「新出现的机会」。
        assert!(new_alert((0.01, None), (Some(0.30), None), 0.03));
        // 昨天已经在极值里，今天还在 —— 持续触发，不是新的。焦煤 2026 年 64% 的
        // 交易日都在触发，全靠这一条把长段压下去。
        assert!(!new_alert((0.01, None), (Some(0.02), None), 0.03));
        // 今天没触发，昨天触没触发都无所谓。
        assert!(!new_alert((0.50, None), (Some(0.01), None), 0.03));
    }

    #[test]
    fn without_yesterdays_position_nothing_is_marked_new() {
        // 该组合的第一天、或前一日没有快照。判不了就不打标记：把「不知道」当成
        // 「刚触发」，页面上会天天冒出假的新触发，而且看不出是假的。
        assert!(!new_alert((0.01, None), (None, None), 0.03));
    }

    #[test]
    fn the_segment_start_follows_the_same_two_track_rule() {
        // 前一日两轨方向相反时，也要按「更极端那条」判，否则会出现今天用合成轨、
        // 昨天用当年轨的错配。昨天当年轨 0.951(高位触发)、历年 0.161(低位触发)，
        // 20% 阈值下昨天已经触发，所以今天不是段首日。
        assert!(!new_alert(
            (0.97, Some(0.5)),
            (Some(0.951), Some(0.161)),
            0.20
        ));
        // 同一组前值放到 3% 阈值下，两条都够不着 —— 今天才算新触发。
        assert!(new_alert(
            (0.01, Some(0.5)),
            (Some(0.951), Some(0.161)),
            0.03
        ));
    }

    #[test]
    fn the_revert_stats_carry_all_three_numbers() {
        // 生产实例：JD2609/JD2701 在 2026-08-14 的高位统计。
        let s = revert_stats(
            "high",
            Some(11),
            Some(12),
            Some("101.5".into()),
            Some("81".into()),
            Some("62".into()),
            Some("247".into()),
            Some(12),
        )
        .expect("有样本");
        assert_eq!((s.hit, s.n), (11, 12));
        assert_eq!(s.rate, "0.9167");
        assert_eq!(s.move_points.as_deref(), Some("101.5"));
        assert_eq!(s.drift_points.as_deref(), Some("81"));
        assert_eq!(s.mae_points.as_deref(), Some("62"));
        assert_eq!(s.mae_max_points.as_deref(), Some("247"));
        assert_eq!(s.days, Some(12));
    }

    #[test]
    fn without_samples_nothing_is_shown_rather_than_zero_percent() {
        // 样本为 0 显示成「0% 回归率」是最坏的一种错：看着像结论，其实是没有数据。
        assert!(revert_stats("high", None, None, None, None, None, None, None).is_none());
        assert!(revert_stats("high", Some(0), Some(0), None, None, None, None, None).is_none());
        // 计数在、点数缺（中位算不出来）时仍然给比率，只是点数留空。
        let s = revert_stats("low", Some(4), Some(5), None, None, None, None, Some(30))
            .expect("有样本");
        assert_eq!(s.rate, "0.8000");
        assert!(s.move_points.is_none());
    }

    #[test]
    fn a_turn_needs_a_recent_alert_and_a_real_retreat() {
        // 近 20 日进过高位带(hi20=1.0),当前退到 0.88 —— 已拐头(默认档 10%)。
        assert_eq!(
            monitor_turn(Some(0.88), Some(1.0), Some(0.40), 0.10),
            Some("high")
        );
        // 还贴在带里(0.98):机会在,但还没拐头。
        assert_eq!(monitor_turn(Some(0.98), Some(1.0), Some(0.40), 0.10), None);
        // 退了但不够(0.93 > 0.90):不算。
        assert_eq!(monitor_turn(Some(0.93), Some(1.0), Some(0.40), 0.10), None);
        // 报警是 20 多天前的事(hi20 已滑出带外):状态自动过期。
        assert_eq!(monitor_turn(Some(0.85), Some(0.94), Some(0.40), 0.10), None);
        // 低位对称。
        assert_eq!(
            monitor_turn(Some(0.12), Some(0.60), Some(0.01), 0.10),
            Some("low")
        );
        // 位置缺失判不了。
        assert_eq!(monitor_turn(None, Some(1.0), Some(0.0), 0.10), None);
    }

    #[test]
    fn the_retreat_line_is_per_variety() {
        // DEC-070:JM 抖动全场最高要深线,JD 早进不受罚要浅线。
        assert_eq!(turn_retreat("JM", "JM"), 0.20);
        assert_eq!(turn_retreat("JD", "JD"), 0.05);
        assert_eq!(turn_retreat("FG", "SA"), 0.08);
        assert_eq!(turn_retreat("LH", "LH"), 0.10);
        assert_eq!(turn_retreat("FG", "FG"), 0.10);
        // DEC-075:AP 曾按脏数据判成 20%,清洗后 8/10/12 三档同值、逐年一致性
        // 掉到 5/8,且 AP 抖动全场最低没有机制支撑——已退回默认档。别再调深。
        assert_eq!(turn_retreat("AP", "AP"), 0.10);
        // JM 退到 0.88 在默认档算拐头,在自家 20% 档还不算——同样的位置,
        // 不同品种结论不同,这正是分档的意义。
        assert_eq!(
            monitor_turn(Some(0.88), Some(1.0), None, turn_retreat("JM", "JM")),
            None
        );
        assert_eq!(
            monitor_turn(Some(0.79), Some(1.0), None, turn_retreat("JM", "JM")),
            Some("high")
        );
        // JD 5% 档:退过 0.95 就算。
        assert_eq!(
            monitor_turn(Some(0.94), Some(1.0), None, turn_retreat("JD", "JD")),
            Some("high")
        );
    }

    fn net_row(
        member: &str,
        instrument: &str,
        net: &str,
        inferred: Option<&str>,
        inferable: bool,
    ) -> database::spread_analytics::ReportNetRow {
        database::spread_analytics::ReportNetRow {
            member: member.to_string(),
            instrument: instrument.to_string(),
            net_position: net.to_string(),
            inferred_prev: inferred.map(str::to_string),
            inferable,
        }
    }

    #[test]
    fn a_reboard_day_backfills_yesterday_from_todays_change() {
        // 高盛 2026-08-17 实例:前一日白银掉榜(昨无行),今天回榜空 2364、增 14
        // → 反推昨净仓 −2350,并打「推」标。
        let today = vec![net_row("高盛期货", "AG", "-2364", Some("-2350"), true)];
        let (value, inferred) = report_prev_net(&[], &today, &["高盛期货".to_string()], "AG");
        assert_eq!(value.as_deref(), Some("-2350"));
        assert!(inferred);
    }

    #[test]
    fn a_missing_change_poisons_the_inference() {
        // 任何一条腿的增减缺失,反推作废 —— 显示横杠,不显示半截和。
        let today = vec![net_row("高盛期货", "AG", "-2364", Some("-2350"), false)];
        let (value, inferred) = report_prev_net(&[], &today, &["高盛期货".to_string()], "AG");
        assert_eq!(value, None);
        assert!(!inferred);
    }

    #[test]
    fn an_actual_yesterday_row_beats_the_inference() {
        // 昨天真在榜上就用真数,反推只补缺口,不覆盖事实。
        let yesterday = vec![net_row("中信期货", "AG", "-26944", None, false)];
        let today = vec![net_row("中信期货", "AG", "-25222", Some("-99999"), true)];
        let (value, inferred) =
            report_prev_net(&yesterday, &today, &["中信期货".to_string()], "AG");
        assert_eq!(value.as_deref(), Some("-26944"));
        assert!(!inferred);
    }

    #[test]
    fn a_group_mixes_actual_and_inferred_and_flags_it() {
        // 机构合计行:六家有真数、高盛靠反推 —— 合计给和,并因含反推成分打标。
        let yesterday = vec![net_row("中财期货", "AG", "11234", None, false)];
        let today = vec![
            net_row("中财期货", "AG", "11330", Some("11200"), true),
            net_row("高盛期货", "AG", "-2364", Some("-2350"), true),
        ];
        let members = vec!["中财期货".to_string(), "高盛期货".to_string()];
        let (value, inferred) = report_prev_net(&yesterday, &today, &members, "AG");
        assert_eq!(value.as_deref(), Some("8884")); // 11234 + (−2350)
        assert!(inferred);
        // 完全没数的品种照旧未知。
        let (none, flag) = report_prev_net(&yesterday, &today, &members, "AU");
        assert_eq!(none, None);
        assert!(!flag);
    }

    #[test]
    fn the_delivery_red_line_counts_weekdays_to_the_window_end() {
        fn day(y: i32, m: u8, d: u8) -> Date {
            Date::from_calendar_date(y, time::Month::try_from(m).unwrap(), d).unwrap()
        }
        // JD2609/JD2701:先到期腿 09 月,止点 = 2026-08-31(周一)。
        // 从 08-17(周一)数,剩 8/18~8/31 的十个周内日。
        assert_eq!(
            days_to_window_end("JD2609", "JD2701", day(2026, 8, 17)),
            Some(10)
        );
        // FG2701/SA2701:止点 = 2026-12-31(周四),远月组合不在红线附近。
        let left = days_to_window_end("FG2701", "SA2701", day(2026, 8, 17)).unwrap();
        assert!(left > 90, "{left}");
        // 已过止点:0,不给负数。
        assert_eq!(
            days_to_window_end("JD2608", "JD2702", day(2026, 8, 17)),
            Some(0)
        );
        // 解析不了的代码判不了,不硬编。
        assert_eq!(days_to_window_end("JD26", "JD2701", day(2026, 8, 17)), None);
    }

    #[test]
    fn the_entry_day_is_the_day_the_position_crosses_the_line() {
        // FG2701/SA2701 生产序列:08-04 位置 1.000(带内,未拐头)→ 08-05 退到
        // 0.884(拐头,前一日 1.000 在线上方)= 进场日。
        assert!(monitor_turn_is_new(Some("high"), Some(1.0), 0.10));
        // 08-10:前一日 0.855 已在线下 —— 拐头持续中,不再是进场日。
        assert!(!monitor_turn_is_new(Some("high"), Some(0.855), 0.10));
        // 08-07 的抖动:前一日 0.906 弹回线上,再穿线 —— is_new_turn 会再真,
        // 但 ⚡ 由前端按 turn_crosses==1 只认首次(DEC-070,运营者拍板)。
        assert!(monitor_turn_is_new(Some("high"), Some(0.906), 0.10));
        // 没拐头就谈不上进场日;前一日缺失判不了,宁可漏标。
        assert!(!monitor_turn_is_new(None, Some(1.0), 0.10));
        assert!(!monitor_turn_is_new(Some("high"), None, 0.10));
        // 低位对称:前一日 0.05 在线下方,今天 ≥0.10 穿上来。
        assert!(monitor_turn_is_new(Some("low"), Some(0.05), 0.10));
        assert!(!monitor_turn_is_new(Some("low"), Some(0.15), 0.10));
    }

    #[test]
    fn a_crash_through_both_bands_picks_the_side_that_just_crossed() {
        // 20 日内从上带砸到 0.05:低位侧还没退够(0.05 < 0.10),只有高位侧成立。
        assert_eq!(
            monitor_turn(Some(0.05), Some(1.0), Some(0.05), 0.10),
            Some("high")
        );
        // 停在正中央,两侧余量相等 —— 也要有确定的答案,不许随机。
        assert_eq!(
            monitor_turn(Some(0.50), Some(1.0), Some(0.0), 0.10),
            Some("high")
        );
        // LH2611−LH2705 @2026-08-19 的真实形态:位置 0.865,20 日内两带都摸过。
        // 离高位线 0.035(昨天还在 1.0,今天刚穿下来)、离低位线 0.765(那波做多
        // 已经走完 715 点)—— 报**高位**。取「更远」会报低位,把走完的机会当信号。
        assert_eq!(
            monitor_turn(Some(0.865), Some(1.0), Some(0.0), 0.10),
            Some("high")
        );
        // 镜像:刚从底部弹上来一点,该报低位。
        assert_eq!(
            monitor_turn(Some(0.135), Some(1.0), Some(0.0), 0.10),
            Some("low")
        );
    }

    #[test]
    fn a_high_hit_rate_with_a_negative_drift_is_the_trap_worth_showing() {
        // JD2612/JD2701：12 年全都曾跌破起点（rate=100%），但一直持到窗口止点的净
        // 变化中位是 −166 点 —— 方向是反的。只显示 rate 会把这种组合读成安全机会，
        // 所以 drift 必须和 rate 一起出现在响应里。
        let s = revert_stats(
            "high",
            Some(12),
            Some(12),
            Some("88".into()),
            Some("-166".into()),
            None,
            None,
            Some(70),
        )
        .expect("有样本");
        assert_eq!(s.rate, "1.0000");
        assert_eq!(s.drift_points.as_deref(), Some("-166"));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn leg(variety: &str, symbol: &str, month: &str) -> FreeSpreadLeg {
        FreeSpreadLeg {
            variety: variety.to_string(),
            symbol: symbol.to_string(),
            month: month.to_string(),
        }
    }

    #[test]
    fn query_accepts_only_fixed_provider_and_two_digit_months() {
        let valid = FreeSpreadQueryRequest {
            provider: "sanhe".to_string(),
            leg1: leg("焦煤", "JM", "09"),
            leg2: leg("焦煤", "JM", "01"),
        };
        assert!(validate_query(&valid).is_ok());
        let invalid = FreeSpreadQueryRequest {
            provider: "https://evil.example".to_string(),
            leg1: leg("焦煤", "JM", "9"),
            leg2: leg("焦煤", "JM", "01"),
        };
        assert_eq!(validate_query(&invalid), Err("provider_invalid"));
    }

    /// 自研的请求必须能过这道校验。
    ///
    /// 页面切成 self 之后第一次点「查看」就报 provider_invalid：校验在分支之前跑，
    /// 只认 sanhe。当时那四个自研测试全是直接测 `own_engine_legs`，从入口进来的这段
    /// 一行都没走到——测了引擎，没测「请求能不能进得来」。
    #[test]
    fn the_own_engine_request_gets_past_the_front_door() {
        for provider in [SELF_PROVIDER_CODE, SANHE_PROVIDER_CODE] {
            let request = FreeSpreadQueryRequest {
                provider: provider.to_string(),
                leg1: leg("焦煤", "JM", "09"),
                leg2: leg("焦煤", "JM", "01"),
            };
            assert!(validate_query(&request).is_ok(), "{provider} 应当被放行");

            let favorite = CreateFavoriteRequest {
                name: "焦煤jm09-焦煤jm01".to_string(),
                provider: provider.to_string(),
                leg1: leg("焦煤", "JM", "09"),
                leg2: leg("焦煤", "JM", "01"),
            };
            assert!(
                validate_favorite(&favorite).is_ok(),
                "{provider} 收藏应当被放行"
            );
        }
    }

    /// 收藏要记住它是在哪条来源下存的。
    ///
    /// 原来这里把 provider 写死成 sanhe——从自研页面存下来的收藏，记录上写着是三禾的。
    #[test]
    fn a_favorite_remembers_which_provider_it_was_saved_under() {
        let body = include_str!("spread_analytics.rs");
        let call = body.split("&NewFavorite {").nth(1).expect("收藏的构造还在");
        let call = &call[..call.find('}').unwrap_or(call.len())];
        assert!(
            call.contains("provider_code: &provider"),
            "收藏必须带上真实来源：{call}"
        );
    }

    #[test]
    fn canonical_query_does_not_contain_url_or_headers() {
        let request = FreeSpreadQueryRequest {
            provider: "sanhe".to_string(),
            leg1: leg("玻璃", "FG", "01"),
            leg2: leg("纯碱", "SA", "01"),
        };
        let value = canonical_query(&request);
        assert_eq!(value.as_object().unwrap().len(), 4);
        assert!(value.get("url").is_none());
        assert!(value.get("headers").is_none());
    }

    fn own_request(a: (&str, &str), b: (&str, &str)) -> FreeSpreadQueryRequest {
        FreeSpreadQueryRequest {
            provider: SELF_PROVIDER_CODE.into(),
            leg1: leg("焦煤", a.0, a.1),
            leg2: leg("焦煤", b.0, b.1),
        }
    }

    #[test]
    fn own_engine_reads_the_variety_and_both_months_from_the_request() {
        let (instrument, front, back) =
            own_engine_legs(&own_request(("jm", "09"), ("jm", "01"))).expect("09-01 是合法组合");
        // 前腿是先到期的那条，价差就是它减后腿。
        assert_eq!((instrument.as_str(), front, back), ("JM", 9, 1));
    }

    #[test]
    fn own_engine_refuses_a_cross_variety_pair() {
        // 跨品种价差不是这个引擎在算的东西；放行只会算出一个没有意义的数。
        assert!(own_engine_legs(&own_request(("jm", "09"), ("jd", "01"))).is_none());
    }

    #[test]
    fn own_engine_refuses_a_month_that_is_not_a_month() {
        for pair in [("jm", "13"), ("jm", "00"), ("jm", "x")] {
            assert!(
                own_engine_legs(&own_request(("jm", "09"), pair)).is_none(),
                "{pair:?} 不该被当成月份"
            );
        }
    }

    #[test]
    fn own_engine_refuses_a_pair_of_the_same_month() {
        // 同月两腿相减恒为零，那不是价差，是把一个错误的选择画成一条直线。
        assert!(own_engine_legs(&own_request(("jm", "09"), ("jm", "09"))).is_none());
    }
}

/// 触发阈值的默认值与边界。
///
/// **5%，不是 10%。** 上线当天在生产的 91 组真实组合上量过（2026-08-11 快照）：
///
/// | 阈值 | 当年触发 | 历年触发 | 合计 |
/// |------|---------|---------|------|
/// | 5%   | 15      | 10      | 25   |
/// | 10%  | 21      | 27      | 47   |
/// | 15%  | 25      | 36      | 56   |
/// | 20%  | 30      | 41      | 61   |
///
/// 10% 那一档是个陡坎：历年触发从 10 跳到 27，合计 47/91 = 52%，半屏飘红等于没报。
/// 设计阶段我推荐过 10%，那是拿**历年轨还没去极端值、且郑商所合约年份还错着**的
/// 数据量的——口径变了，默认值就得重量一遍。去极端值把历年区间收窄，位置自然更
/// 容易落到两端，这是设计使然，不是 bug。
///
/// 上限 0.5 是护栏：到 50% 就是「整个区间都算触发」，那不是阈值而是关掉了过滤。
const MONITOR_THRESHOLD_DEFAULT: f64 = 0.05;
const MONITOR_THRESHOLD_MAX: f64 = 0.50;
/// 合约到期后不再有新快照，它的最后一条会永远留在表里。超过这些天没更新就不算「当前」。
const MONITOR_STALE_DAYS: i32 = 7;

#[derive(Debug, Deserialize)]
pub struct SpreadMonitorQuery {
    /// 落在区间两端多少算触发，0 到 0.5。不传用 0.10。
    pub threshold: Option<f64>,
    /// 看某一天的历史快照。不传看当前。
    pub trade_date: Option<String>,
    /// true = 一次取回全部快照日的行(历史信号视图),忽略 trade_date。
    pub history: Option<bool>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct SpreadMonitorTrack {
    /// 该口径下的历史最低 / 最高。历年轨是第 2.5 / 97.5 百分位，不是原始极值。
    pub low: String,
    pub high: String,
    /// 当前价差在区间里的位置。历年轨用百分位区间，所以允许落在 0~1 之外。
    pub position: Option<String>,
    pub days: Option<i32>,
    /// "high" / "low" / null。按本次请求的阈值算出来的，不是存下来的。
    pub alert: Option<String>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct SpreadMonitorItem {
    pub trade_date: String,
    pub instrument_1: String,
    pub contract_1: String,
    pub instrument_2: String,
    pub contract_2: String,
    pub is_cross_variety: bool,
    pub spread: String,
    pub pair: SpreadMonitorTrack,
    pub years: Option<SpreadMonitorTrack>,
    /// 两条轨里只要有一条触发就不为空。两条方向相反时以「更极端的那条」为准。
    pub alert: Option<String>,
    /// 今天触发、前一交易日按同一阈值不触发 —— 也就是**刚进极值**。
    ///
    /// 焦煤 2026 年有 64% 的交易日都在 3% 触发(价差持续创新低，滚动区间天天被
    /// 刷新)，而连续触发段的中位长度只有 3 日:绝大多数段是短的，长段拖着不放
    /// 才是噪音。区分这两者，页面才能把「新出现的机会」从一片红里挑出来。
    ///
    /// 前一日位置缺失(该组合的第一天、或前一日没有快照)时为 false —— 判不了就
    /// 不打标记，宁可漏标也不假报。
    pub is_new_alert: bool,
    /// 未触发、且未拐头时为空；否则给出**这一行要做的那笔交易**那一侧的历年统计,
    /// 样本不足也为空。侧别 = 拐头侧优先、其次报警侧(DEC-088):⚡ 由拐头触发,
    /// 资格就必须用拐头侧的数字判,否则会拿 A 方向的成绩给 B 方向发通行证。
    /// `side` 同时就是交易方向:"high" = 做空价差,"low" = 做多价差。
    pub revert: Option<SpreadRevertStats>,
    /// **报警侧与拐头侧相反时**,另一侧(报警侧)的统计。方向一致时为空。
    ///
    /// 存在即意味着这一行的两条轨在讲相反的故事(例:历年轨贴底 → 做多价差,
    /// 当年轨自顶部拐头 → 做空价差)。界面必须把它显示出来:DEC-088 那个 BUG 的
    /// 本质就是只显示了其中一侧,让人以为只有一笔交易可做。
    pub revert_alt: Option<SpreadRevertStats>,
    /// 平台位阶梯(DEC-095):价差自己走出来的横盘转折位,按档位从高到低。
    /// 运营者下单看的就是它——「收盘突破平台位,才能继续往下看」。
    /// 空数组 = 那天还没算出档位(序列太短,或旧行没有这一列)。
    pub shelves: Vec<SpreadShelf>,
    /// "high" / "low" / null —— **已拐头**：近 20 个交易日内当年轨曾进 3% 报警带，
    /// 且当前已自极值回撤超过区间宽度的 10%（= 位置退到 0.90 以下 / 0.10 以上）。
    ///
    /// 这是分层规则（DEC-063）的进场信号：报警只是机会出现，拐头才是上车点。
    /// 全样本回放里报警即进持到底中位为负；先按历年统计筛资格、再等拐头，
    /// 留一法验证下持到底中位 +39%。报警带取最严档（3%）、回撤量取通用 10%，
    /// 都是常量不随页面阈值变——给两个可调旋钮只会诱导挑参数。
    /// 只看当年轨：资格统计与回放验证都在当年轨的可交易窗口上，口径闭环。
    pub turn: Option<String>,
    /// **今天刚拐头**：拐头成立，且前一交易日位置还在回撤线的另一侧——位置是今天
    /// 才穿线的。这就是回放里的进场日；拐头标最多挂 20 个交易日,「处于可进场状态」
    /// 与「今天就是进场日」是两回事,界面靠它把后者单独点亮并置顶。
    /// 判定用 `prev_pair_position`(段首日标记的同一素材),前一日缺失时为 false
    /// ——判不了就不标,与 is_new_alert 同一条原则。
    pub is_new_turn: bool,
    /// 拐头侧在近 20 个交易日内的穿线次数(含今天)。≥2 = 同一段行情里拐头反复
    /// ——JM2609/JM2701 八天三次穿线、期间打回区间顶,前两次进场按离场规则都
    /// 止损;FG2701/SA2701 干脆的拐头只有 1。界面用它打「信号差」降级标。
    /// 仅拐头行给值(没拐头谈不上拐头质量)。
    pub turn_crosses: Option<i32>,
    /// 距该组合可交易窗口止点(先到期腿散户最后交易日)的剩余交易日,周内日近似。
    /// 界面按《体系》红线(≤15 清仓/压制进场)与数据实证的衰减区(<40)分档提示。
    pub days_left: Option<i32>,
    /// 该**月份模板**的手工产业备注(DEC-069):运营者手填的品种级知识,
    /// 跟月份走不跟具体合约走(JD2609/2701 与 JD2709/2801 共享「09-01」一条)。
    pub note: Option<String>,
    /// 组合已到期(先到期腿的散户窗口在最新快照日之前已关):历史信号里的
    /// 过期组合打灰标(DEC-071)。按最新快照日判而不是墙钟,结果可复现。
    pub expired: bool,
    /// 该品种当日的现货与基差背景(DEC-074)。跨期两条腿相对同一个现货,
    /// 所以基差之差就是价差本身——这里给的是**水平与历史分位**,是背景不是信号。
    pub basis: Option<SpreadBasisInfo>,
}

/// 现货与基差背景。跨品种组合按第一条腿的品种给(玻纯以玻璃为准)。
#[derive(Debug, Serialize, ToSchema)]
pub struct SpreadBasisInfo {
    pub instrument: String,
    /// 基差数据的交易日。源偶尔缺日,可能早于快照日(最多回看 7 天)。
    pub trade_date: String,
    pub spot_price: String,
    /// 主力基差 = 现货 − 主力期货。为正是期货贴水,为负是期货升水。
    pub dominant_basis: Option<String>,
    pub dominant_basis_rate: Option<String>,
    /// 主力基差率在该品种历年里的百分位(0~1),样本不足 60 天为 None。
    pub percentile: Option<String>,
}

/// 该月份组合模板在**可交易窗口**内、按日历位置对齐的历年表现。
///
/// **不是这一组合自己的胜率**:样本是同品种、同月份对、同年差的模板跨年拼起来的
/// (例如鸡蛋 09-01)。一个具体合约对一辈子只有一个生命周期,算不出有意义的比率。
///
/// 口径(完整版见迁移 202608170002):可交易窗口照 5A 窗口引擎——止点 = 先到期那条
/// 腿的散户最后交易日;历年按**月-日**对齐,一直看到各自窗口的止点;**曾经触及**
/// 即算回归,不比终点。只用已走完的年份实例。
///
/// **单看 rate 会骗人**,所以三个数一起给:剩余期一长 rate 就趋近 100%(任何波动
/// 序列在足够长的窗口里几乎必然回落一次);JD2612/JD2701 的 rate 是 100% 而 drift
/// 中位是 −166 点,方向反的。
#[derive(Debug, Serialize, ToSchema)]
pub struct SpreadRevertStats {
    /// 与本次报警同侧:"low" 统计低位、"high" 统计高位。
    pub side: String,
    /// 曾经触及回归的年数与样本年数。给原始计数是有意的:「12 年里 11 年」比孤零零
    /// 一个 92% 更能让人看出样本有多薄。不设年数门槛,薄不薄由界面写出来让人自己判断。
    pub hit: i32,
    pub n: i32,
    pub rate: String,
    /// 最有利那一刻相对起点走了多少**点**(择时平仓的上限)。价差会跨零,所以不给
    /// 百分比——2019-08-14 起点 −8 点、回落 407 点,百分比是 5000%,毫无意义。
    pub move_points: Option<String>,
    /// 一直持到窗口止点的净变化,已标准化成**正数 = 朝回归走**。
    pub drift_points: Option<String>,
    /// 历年 MAE 中位:锚点后先朝不利方向走的幅度,浮亏到这里是历年常态——
    /// **补仓参考**(《盖楼》猪 11-05 分批法的数据化,DEC-067)。
    pub mae_points: Option<String>,
    /// 历年 MAE 最大:**风险预留**。仓位 = 可承受亏损 ÷ (此数 × 点值)。
    /// 盈亏比分级(move÷MAE)已回测否决(">2.5 档"实际最差),只给分母不给比值。
    pub mae_max_points: Option<String>,
    /// 历年剩余交易日中位数,给上面几个数一个时间尺度。
    pub days: Option<i32>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct SpreadMonitorResponse {
    /// 本次采用的阈值，原样回给界面——省得界面自己记一份默认值。
    pub threshold: String,
    pub as_of: Option<String>,
    pub available_dates: Vec<String>,
    pub items: Vec<SpreadMonitorItem>,
}

/// 一条轨的触发判定。位置在下端 `threshold` 之内报低位，上端之内报高位。
///
/// **越界也算触发**：历年轨用的是百分位区间，当前价差可以落在第 2.5 百分位之下
/// （位置为负）或第 97.5 之上（位置大于 1）。那是比「贴着边」更强的信号，
/// 用 `<=` / `>=` 自然覆盖，不需要额外分支。
fn monitor_alert(position: Option<f64>, threshold: f64) -> Option<&'static str> {
    let position = position?;
    if position <= threshold {
        Some("low")
    } else if position >= 1.0 - threshold {
        Some("high")
    } else {
        None
    }
}

/// 报警带常量。写死不进 Query:用页面最严档,做成旋钮只会诱导挑参数。
const TURN_BAND: f64 = 0.03;

/// 回撤档按品种定(DEC-070,**2026-08-18 用干净数据重测后修订,见 DEC-075**)。
///
/// 回撤线画在「位置」刻度上,起作用的波动率是**位置的日间抖动**,不是价格波动率:
/// - JM 抖动全场最高(中位 6.4pp/日,P90 21pp),10% 线只有 1.5 天正常抖动宽,
///   噪音一天就穿——深到 20%(≈3 倍日抖动)才滤得住,逐笔核查躲开三笔大亏;
/// - JD 是唯一「早进不受罚」的品种(早进组中位 +7.4%,季节备货趋势盖过波动),
///   深回撤对它纯粹让利,浅至 5%;
/// - FG-SA 跨品种回归快,逐年留一 4/6 选 8%,深档代价惨重(20% 档 −29.7%);
///   **但它全部档位都是负期望,档位选择的意义有限,这个组合本身就该谨慎**;
/// - **AP 已退回默认档**:郑商所收盘价 0 的脏数据(DEC-073)修掉后重测,
///   8/10/12 三档结果完全相同(+28.9%),深档优势从 14.7pt 缩到 5.1pt,逐年
///   一致性从 8/8 掉到 5/8;而 AP 的位置抖动是全场**最低**的,深档优势从来没有
///   机制解释。撑不住就收回去。
/// - LH 与 FG 跨期相邻档正负翻转、逐年选择不稳,挑了就是过拟合,维持 10%。
///
/// **JM 与 JD 是大商所品种,数据未受那次污染影响,机制与数据两条腿都还在,不动。**
///
/// 与采集 SQL turn_crosses 的分档(compute-spread-monitor.sql)同值,改一处必须
/// 同批改另一处并重跑重算。
fn turn_retreat(instrument_1: &str, instrument_2: &str) -> f64 {
    if instrument_1 != instrument_2 {
        // 目前唯一的跨品种组合是玻璃纯碱;未来若出现别的跨品种对,回默认档。
        return if (instrument_1, instrument_2) == ("FG", "SA") {
            0.08
        } else {
            0.10
        };
    }
    match instrument_1 {
        "JM" => 0.20,
        "JD" => 0.05,
        _ => 0.10,
    }
}

/// 已拐头:近 20 个交易日当年轨曾进报警带,且当前位置已退到带外超过回撤量。
///
/// 「自极值回撤区间的 X%」等价于「位置退 X 个百分点」:报警时价差贴着滚动
/// 极值,极值就是区间端点,(端点 − 当前) / 区间宽 = 1 − 位置。所以不需要另存
/// 极值,只需要近 20 日位置的 max/min(迁移 202608170004)。X 按品种定,见
/// `turn_retreat`。两侧同时满足(20 日内既摸过上带又摸过下带)取**离自家门槛更近**
/// 的一侧 —— 那是刚穿线的、还能进的那一侧(DEC-088)。
fn monitor_turn(
    pos: Option<f64>,
    hi20: Option<f64>,
    lo20: Option<f64>,
    retreat: f64,
) -> Option<&'static str> {
    let pos = pos?;
    let high = hi20.is_some_and(|h| h >= 1.0 - TURN_BAND) && pos <= 1.0 - retreat;
    let low = lo20.is_some_and(|l| l <= TURN_BAND) && pos >= retreat;
    match (high, low) {
        (true, false) => Some("high"),
        (false, true) => Some("low"),
        // 两侧同时成立 = 20 日内既摸过上带又摸过下带。取**刚穿线的那一侧**,
        // 也就是离自己门槛**更近**的一侧(DEC-088,2026-08-19 修正方向)。
        //
        // 原来取的是「离门槛更远」,想的是「退得更多 = 拐得更实」,但退得多恰恰
        // 说明那一侧的回归**已经走完了**,不是能进的场。
        // 实例 LH2611−LH2705 @2026-08-19:位置 0.865,离高位线 0.90 只有 0.035
        // (08-18 还在 1.0,今天刚穿下来),离低位线 0.10 有 0.765(20 日前创的新低,
        // 那波做多价差已经走了 715 点)。原实现报「低位」,把一个走完的机会当成
        // 当前信号,还顺带把统计切到低位侧(持到期 −635)判成不合格 —— 而高位侧
        // 是 5/5、持到期 +635,本该亮 ⚡ 做空。
        (true, true) => Some(if (1.0 - retreat - pos) <= (pos - retreat) {
            "high"
        } else {
            "low"
        }),
        (false, false) => None,
    }
}

/// 拐头是不是今天刚发生:前一日位置还在回撤线的另一侧。
///
/// 不必看前一日的 hi20:band 触碰当天位置必然 ≥0.97 > 0.90,所以「昨天拐头不成立、
/// 今天成立」只可能因为位置今天穿线,不可能因为 band 今天才进窗(自相矛盾)。
/// 判定线附近的抖动(穿线→弹回→再穿线)会再亮一次——没上车的人得到第二次提示,
/// 已上车的人无视即可。
/// 报告表「昨持仓」的合成:实际昨行优先;席位整日掉榜但当日各腿增减齐全时,
/// 用「持仓 − 增减」的反推值补上(运营者 2026-08-17 拍板:回榜日能反推的要写进
/// 报告表);两者都没有的席位不计入,一家都没有 → 整格未知(横杠)。
/// 返回 (值, 是否含反推成分)。
fn report_prev_net(
    yesterday: &[database::spread_analytics::ReportNetRow],
    today: &[database::spread_analytics::ReportNetRow],
    members: &[String],
    instrument: &str,
) -> (Option<String>, bool) {
    let mut total = Decimal::ZERO;
    let mut seen = false;
    let mut inferred_used = false;
    for name in members {
        if let Some(row) = yesterday
            .iter()
            .find(|row| row.instrument == instrument && &row.member == name)
        {
            total += parse_decimal(&row.net_position);
            seen = true;
            continue;
        }
        let inferred = today
            .iter()
            .find(|row| row.instrument == instrument && &row.member == name && row.inferable);
        if let Some(row) = inferred
            && let Some(raw) = row.inferred_prev.as_deref()
        {
            total += parse_decimal(raw);
            seen = true;
            inferred_used = true;
        }
    }
    (seen.then(|| total.normalize().to_string()), inferred_used)
}

fn monitor_turn_is_new(turn: Option<&str>, prev_pair: Option<f64>, retreat: f64) -> bool {
    match turn {
        Some("high") => prev_pair.is_some_and(|p| p > 1.0 - retreat),
        Some("low") => prev_pair.is_some_and(|p| p < retreat),
        _ => false,
    }
}

/// 组合窗口止点(先到期腿的散户最后交易日)与当日之间的**剩余交易日**。
///
/// 口径照 5A 的 `last_weekday_before_delivery`:止点=交割月前月最后一个非周末日;
/// 计数按周内日近似(节假日没有价格点,误差 ±2 天以内,红线用途足够)。
/// 《体系》红线:交割前 15 个交易日全部清仓;留一法数据:合格段剩余 <15 日持到底
/// 中位 −21.7%、15~40 日 −32.5%、>40 日 +54.8%(DEC-067)。
fn days_to_window_end(c1: &str, c2: &str, today: Date) -> Option<i32> {
    fn deadline(code: &str) -> Option<Date> {
        let digits = code.find(|c: char| c.is_ascii_digit())?;
        let raw = &code[digits..];
        if raw.len() != 4 {
            return None;
        }
        let year = 2000 + raw[0..2].parse::<i32>().ok()?;
        let month = raw[2..4].parse::<u8>().ok()?;
        let (py, pm) = if month == 1 {
            (year - 1, 12u8)
        } else {
            (year, month - 1)
        };
        let month = time::Month::try_from(pm).ok()?;
        let mut day = time::util::days_in_month(month, py);
        loop {
            let date = Date::from_calendar_date(py, month, day).ok()?;
            if !matches!(
                date.weekday(),
                time::Weekday::Saturday | time::Weekday::Sunday
            ) {
                return Some(date);
            }
            day -= 1;
        }
    }
    let end = deadline(c1)?.min(deadline(c2)?);
    if end <= today {
        return Some(0);
    }
    let mut count = 0i32;
    let mut cursor = today.next_day()?;
    while cursor <= end {
        if !matches!(
            cursor.weekday(),
            time::Weekday::Saturday | time::Weekday::Sunday
        ) {
            count += 1;
        }
        cursor = cursor.next_day()?;
    }
    Some(count)
}

fn track_position(track: &SpreadMonitorTrack) -> Option<f64> {
    track.position.as_deref()?.parse().ok()
}

/// 两条轨合成一个结论，**只看位置与阈值**。
///
/// **方向可能相反**：焦煤 JM2609−JM2701 在 2026-08-11 就是当年高位（95.1%）、
/// 历年低位（16.1%）。这时报「更极端的那条」——离中线更远的一个。随便挑一条
/// 会有一半的机会把方向说反，而页面上看不出它挑错了。
///
/// 写成不依赖 `SpreadMonitorTrack` 的形式，是因为判段首日要拿**前一交易日**的两个
/// 位置走同一条规则。同一个「取更极端那条」的判断在两处各写一遍，迟早会漂。
fn combined_alert_at(
    pair: Option<f64>,
    years: Option<f64>,
    threshold: f64,
) -> Option<&'static str> {
    [pair, years]
        .into_iter()
        .flatten()
        .filter_map(|position| {
            monitor_alert(Some(position), threshold).map(|alert| ((position - 0.5).abs(), alert))
        })
        .max_by(|a, b| a.0.total_cmp(&b.0))
        .map(|(_, alert)| alert)
}

/// 到达概率曲线(DEC-095)。**合并品种与方向**的经验生存函数:
/// `P(能走到 z 个 σ√T 之外)` = 历史上从同样远近的处境出发,在窗口止点前摸到过的比例。
///
/// 为什么合并:逐品种、逐方向的版本**样本外崩了**(焦煤 25%→48%、生猪 69%→52%),
/// 那些差异是那段行情往哪边走了(=漂移),不是品种特性。合并之后按剩余期分桶,
/// 样本外差 ≤2.6 个点(14.4 万个观测)。
///
/// **固化成常量、不每晚重算**:这是研究结论,应当随代码评审一起变,不该因为多了
/// 一天数据就让页面上的数字无声漂移。重估要跑 `research/run_shelf_prob.py`。
///
/// **它不知道方向**:上下两侧用同一条曲线。方向由「日线收盘突破平台位」那条规矩定。
/// **逐年离散很大**:z=1.0 长期 42%,最低 17%(2014)、最高 53%(2019)——界面必须写出来。
const REACH_CURVE: [&[f64]; 4] = [
    // 剩余 5~20 个交易日,样本 18,848
    &[
        84.2, 78.4, 72.8, 67.3, 62.2, 57.3, 52.9, 48.6, 44.6, 40.8, 37.6, 34.7, 32.2, 29.7, 27.6,
        25.5, 23.5, 21.6, 20.1, 18.5, 17.2, 16.0, 15.0, 13.9, 13.0, 12.1, 11.4, 10.7, 9.9, 9.3,
        8.6,
    ],
    // 剩余 21~40 个交易日,样本 23,410
    &[
        91.4, 85.5, 79.9, 74.3, 68.7, 63.6, 59.0, 54.5, 50.5, 46.8, 43.4, 40.1, 36.9, 34.3, 31.8,
        29.6, 27.4, 25.6, 23.7, 22.1, 20.7, 19.2, 17.9, 16.7, 15.5, 14.5, 13.5, 12.6, 11.8, 11.1,
        10.3,
    ],
    // 剩余 41~80 个交易日,样本 43,902
    &[
        95.3, 89.9, 83.5, 77.1, 71.0, 65.4, 60.4, 56.0, 51.9, 47.9, 44.3, 41.1, 37.9, 35.4, 33.1,
        30.9, 28.9, 27.0, 25.3, 23.7, 22.1, 20.6, 19.4, 18.1, 17.1, 15.9, 14.9, 14.0, 13.1, 12.3,
        11.6,
    ],
    // 剩余 >80 个交易日,样本 57,542
    &[
        97.5, 91.9, 84.4, 75.9, 68.2, 61.1, 55.0, 50.0, 45.6, 41.7, 38.3, 35.1, 32.4, 30.1, 28.0,
        26.3, 24.6, 22.9, 21.3, 19.9, 18.5, 17.1, 16.0, 15.0, 14.0, 13.1, 12.3, 11.5, 10.9, 10.2,
        9.5,
    ],
];

/// 把库里存的平台位事实,配上读时才知道的东西:相对现价的偏移、z、到达概率、
/// 以及这一行要做的那笔交易下它是卖点还是止损。
///
/// 角色按 `trade_side` 定(DEC-088 的同一个侧别):做空价差(high)时下方是目标、
/// **上方最近的一档是止损**;做多价差(low)反过来。没有交易侧就不给角色——
/// 那种行本来就没有一笔要做的交易。
fn build_shelves(
    raw: Option<&str>,
    spread: Option<f64>,
    sigma: Option<f64>,
    days_left: Option<i32>,
    trade_side: Option<&str>,
) -> Vec<SpreadShelf> {
    let (Some(raw), Some(spread)) = (raw, spread) else {
        return Vec::new();
    };
    let Ok(items) = serde_json::from_str::<Vec<serde_json::Value>>(raw) else {
        return Vec::new();
    };
    let scale = match (sigma, days_left) {
        (Some(s), Some(d)) if s > 0.0 && d > 0 => Some(s * f64::from(d).sqrt()),
        _ => None,
    };
    let num = |v: &serde_json::Value, k: &str| -> Option<f64> {
        v.get(k).and_then(|x| match x {
            serde_json::Value::Number(n) => n.as_f64(),
            serde_json::Value::String(t) => t.parse().ok(),
            _ => None,
        })
    };
    let mut out: Vec<SpreadShelf> = items
        .iter()
        .filter_map(|v| {
            let level = num(v, "level")?;
            let offset = level - spread;
            let z = scale.map(|s| (offset.abs() / s * 100.0).round() / 100.0);
            Some(SpreadShelf {
                level: format!("{}", level.round() as i64),
                lo: format!("{}", num(v, "lo").unwrap_or(level).round() as i64),
                hi: format!("{}", num(v, "hi").unwrap_or(level).round() as i64),
                touches: v
                    .get("touches")
                    .and_then(serde_json::Value::as_i64)
                    .unwrap_or(0),
                offset: format!("{}", offset.round() as i64),
                z: z.map(|x| format!("{x:.2}")),
                reach_pct: z.zip(days_left).and_then(|(x, d)| reach_pct(x, d)),
                role: String::new(),
            })
        })
        .collect();
    // 按档位从高到低,库里已经是这个序,这里不依赖它。
    out.sort_by(|a, b| {
        b.level
            .parse::<i64>()
            .unwrap_or(0)
            .cmp(&a.level.parse::<i64>().unwrap_or(0))
    });
    let Some(side) = trade_side else {
        return out;
    };
    // 目标在交易方向那一侧;止损取**反方向最近的一档**——与运营者的规矩对称:
    // 「日线收盘突破平台位,就会前往下一个平台」,反过来突破就是止损。
    let target_above = side == "low";
    let mut stop_taken = false;
    let iter: Box<dyn Iterator<Item = &mut SpreadShelf>> = if target_above {
        Box::new(out.iter_mut()) // 止损在下方:从高到低,最先遇到的下方档最近
    } else {
        Box::new(out.iter_mut().rev()) // 止损在上方:从低到高
    };
    for sh in iter {
        let off: i64 = sh.offset.parse().unwrap_or(0);
        if off == 0 {
            continue;
        }
        let above = off > 0;
        if above == target_above {
            sh.role = "target".to_string();
        } else if !stop_taken {
            sh.role = "stop".to_string();
            stop_taken = true;
        }
    }
    out
}

fn reach_bucket(days_left: i32) -> usize {
    match days_left {
        ..=20 => 0,
        21..=40 => 1,
        41..=80 => 2,
        _ => 3,
    }
}

/// 曲线上按 0.1 的格子线性插值。z 超出格子就取末端——外推没有依据。
fn reach_pct(z: f64, days_left: i32) -> Option<f64> {
    if !z.is_finite() || z < 0.0 {
        return None;
    }
    let curve = REACH_CURVE[reach_bucket(days_left)];
    let last = curve.len() - 1;
    let x = (z / 0.1).min(last as f64);
    let i = x.floor() as usize;
    let p = if i >= last {
        curve[last]
    } else {
        curve[i] + (curve[i + 1] - curve[i]) * (x - i as f64)
    };
    Some((p * 10.0).round() / 10.0)
}

/// 这一行**要做的那笔交易**在哪一侧 —— 拐头侧优先(DEC-088)。
///
/// ⚡ 进场由拐头触发,所以资格、统计、方向文案都必须锚在拐头侧。原实现是
/// `alert.or(turn)`,两侧相反时会拿报警侧的成绩给拐头侧的交易发通行证。
/// 没拐头只报警的行按报警侧:那是「机会出现、还没到上车点」,本来就没有 ⚡。
fn trade_side(alert: Option<&'static str>, turn: Option<&'static str>) -> Option<&'static str> {
    turn.or(alert)
}

fn combined_alert(
    pair: &SpreadMonitorTrack,
    years: Option<&SpreadMonitorTrack>,
    threshold: f64,
) -> Option<&'static str> {
    combined_alert_at(
        track_position(pair),
        years.and_then(track_position),
        threshold,
    )
}

/// 平台位阶梯里的一档(DEC-095)。
///
/// 档位、区间、触碰回合是**库里存的事实**;偏移、z、到达概率、卖点/止损全部**读时算**
/// ——与报警/拐头/合格同一条纪律(存事实不存结论)。
#[derive(Debug, Clone, Serialize, utoipa::ToSchema)]
pub struct SpreadShelf {
    /// 档位(该档并入的转折位均值)。
    pub level: String,
    /// 并档区间的两端。链式合并会让几个转折位并出一个跨几十点的档,
    /// 只报均值是假精度,所以两端一起给出来。
    pub lo: String,
    pub hi: String,
    /// 收盘落在该档 ±25 点内的**独立回合数**(连续日算一回合)。
    pub touches: i64,
    /// 相对现价差的点数。**正 = 在上方**。
    pub offset: String,
    /// 距离 ÷ (σ√剩余交易日)。没有 σ 或剩余天数时为空。
    pub z: Option<String>,
    /// 到达概率(%),来自固化的合并曲线。**不含方向判断**,逐年离散很大。
    pub reach_pct: Option<f64>,
    /// `"target"`(卖点侧)/ `"stop"`(反方向最近的一档)/ `""`。
    /// 按这一行要做的那笔交易定:做空价差时下方是目标、上方最近的一档是止损。
    pub role: String,
}

/// 组装同侧的历年统计。样本为 0(或整块缺失)时返回 None —— 界面不显示这一块，
/// 而不是显示一个「0% 回归率」：那看着像结论，其实是没有数据。
#[allow(clippy::too_many_arguments)]
fn revert_stats(
    side: &str,
    hit: Option<i32>,
    n: Option<i32>,
    move_points: Option<String>,
    drift_points: Option<String>,
    mae_points: Option<String>,
    mae_max_points: Option<String>,
    days: Option<i32>,
) -> Option<SpreadRevertStats> {
    let (hit, n) = (hit?, n?);
    if n <= 0 {
        return None;
    }
    Some(SpreadRevertStats {
        side: side.to_string(),
        hit,
        n,
        rate: format!("{:.4}", f64::from(hit) / f64::from(n)),
        move_points,
        drift_points,
        mae_points,
        mae_max_points,
        days,
    })
}

fn monitor_track(
    low: Option<String>,
    high: Option<String>,
    position: Option<String>,
    days: Option<i32>,
    threshold: f64,
) -> Option<SpreadMonitorTrack> {
    let (low, high) = (low?, high?);
    let alert = monitor_alert(
        position.as_deref().and_then(|value| value.parse().ok()),
        threshold,
    );
    Some(SpreadMonitorTrack {
        low,
        high,
        position,
        days,
        alert: alert.map(str::to_string),
    })
}

#[utoipa::path(
    get,
    path = "/api/v1/spread-analytics/monitor",
    params(
        ("threshold" = Option<f64>, Query),
        ("trade_date" = Option<String>, Query),
        ("history" = Option<bool>, Query)
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = SpreadMonitorResponse),
        (status = 400, body = SpreadErrorBody),
        (status = 401, body = SpreadErrorBody),
        (status = 403, body = SpreadErrorBody)
    )
)]
pub async fn query_spread_monitor(
    State(state): State<Arc<SpreadAnalyticsState>>,
    headers: HeaderMap,
    Query(query): Query<SpreadMonitorQuery>,
) -> Result<Response, SpreadApiError> {
    let request_id = Uuid::now_v7();
    let context = read_context(&state, &headers, request_id).await?;

    let threshold = query.threshold.unwrap_or(MONITOR_THRESHOLD_DEFAULT);
    if !threshold.is_finite() || threshold <= 0.0 || threshold > MONITOR_THRESHOLD_MAX {
        return Err(SpreadApiError::Validation("invalid_threshold", request_id));
    }

    let trade_date = match query.trade_date.as_deref().map(str::trim) {
        None | Some("") => None,
        Some(value) => Some(
            Date::parse(value, &time::format_description::well_known::Iso8601::DATE)
                .map_err(|_| SpreadApiError::Validation("invalid_trade_date", request_id))?,
        ),
    };

    // 历史模式(DEC-070):一次取回全部快照日,前端只渲染进场行——
    // 运营者要看历年 ⚡ 不必逐日期点选。判定与单日路径同一套读时逻辑。
    let rows = if query.history.unwrap_or(false) {
        database::spread_analytics::spread_monitor_history(&state.auth.pool, context.workspace_id())
            .await
    } else {
        match trade_date {
            Some(day) => {
                database::spread_analytics::spread_monitor_on(
                    &state.auth.pool,
                    context.workspace_id(),
                    day,
                )
                .await
            }
            None => {
                database::spread_analytics::spread_monitor_snapshot(
                    &state.auth.pool,
                    context.workspace_id(),
                    MONITOR_STALE_DAYS,
                )
                .await
            }
        }
    }
    .map_err(|_| SpreadApiError::Internal(request_id))?;

    let dates = database::spread_analytics::spread_monitor_dates(
        &state.auth.pool,
        context.workspace_id(),
        400,
    )
    .await
    .map_err(|_| SpreadApiError::Internal(request_id))?;

    let notes =
        database::spread_analytics::load_template_notes(&state.auth.pool, context.workspace_id())
            .await
            .map_err(|_| SpreadApiError::Internal(request_id))?;

    // 现货基差背景(DEC-074)。按最新快照日取一批,历史模式下不逐日取——
    // 历史行看的是当时的进场信号,基差是"现在的产业背景",给最新的即可。
    let basis_rows = match rows.iter().map(|row| row.trade_date).max() {
        Some(day) => database::spread_analytics::load_spot_basis(
            &state.auth.pool,
            context.workspace_id(),
            day,
        )
        .await
        .unwrap_or_default(),
        None => Vec::new(),
    };
    let basis_map: std::collections::HashMap<String, SpreadBasisInfo> = basis_rows
        .into_iter()
        .map(|row| {
            (
                row.instrument.clone(),
                SpreadBasisInfo {
                    instrument: row.instrument,
                    trade_date: row.trade_date.to_string(),
                    spot_price: row.spot_price,
                    dominant_basis: row.dominant_basis,
                    dominant_basis_rate: row.dominant_basis_rate,
                    percentile: row.basis_percentile,
                },
            )
        })
        .collect();
    let note_map: std::collections::HashMap<(String, i32, String, i32), String> = notes
        .into_iter()
        .map(|(i1, m1, i2, m2, note)| ((i1, m1, i2, m2), note))
        .collect();
    let month_of = |contract: &str| -> Option<i32> {
        contract
            .get(contract.len().saturating_sub(2)..)
            .and_then(|mm| mm.parse::<i32>().ok())
    };

    // 过期判定的基准日 = 这批行里最新的快照日(不是墙钟,结果可复现)。
    let latest_snapshot = rows.iter().map(|row| row.trade_date).max();

    let items: Vec<SpreadMonitorItem> = rows
        .into_iter()
        .map(|row| {
            let note = match (month_of(&row.contract_1), month_of(&row.contract_2)) {
                (Some(m1), Some(m2)) => note_map
                    .get(&(row.instrument_1.clone(), m1, row.instrument_2.clone(), m2))
                    .cloned(),
                _ => None,
            };
            let pair = monitor_track(
                Some(row.pair_low),
                Some(row.pair_high),
                row.pair_position,
                Some(row.pair_days),
                threshold,
            )
            .expect("当年轨的上下界在库里是 not null");
            let years = monitor_track(
                row.years_low,
                row.years_high,
                row.years_position,
                row.years_days,
                threshold,
            );
            let alert = combined_alert(&pair, years.as_ref(), threshold);

            // 段首日：今天触发、前一交易日按同一阈值不触发。前一日位置整个缺失时
            // `combined_alert_at` 返回 None，会把「判不了」误判成「刚触发」，所以
            // 额外要求至少有一条轨的前值存在。
            let parse = |value: &Option<String>| -> Option<f64> {
                value.as_deref().and_then(|raw| raw.parse().ok())
            };
            let prev_pair = parse(&row.prev_pair_position);
            let prev_years = parse(&row.prev_years_position);
            let has_prev = prev_pair.is_some() || prev_years.is_some();
            let is_new_alert = alert.is_some()
                && has_prev
                && combined_alert_at(prev_pair, prev_years, threshold).is_none();

            let expired = latest_snapshot.is_some_and(|day| {
                days_to_window_end(&row.contract_1, &row.contract_2, day) == Some(0)
            });
            // 跨品种组合按第一条腿的品种给基差(玻纯以玻璃为准):两个品种各有
            // 自己的现货,挑一个是显示取舍,不是计算。
            let basis = basis_map
                .get(&row.instrument_1)
                .map(|info| SpreadBasisInfo {
                    instrument: info.instrument.clone(),
                    trade_date: info.trade_date.clone(),
                    spot_price: info.spot_price.clone(),
                    dominant_basis: info.dominant_basis.clone(),
                    dominant_basis_rate: info.dominant_basis_rate.clone(),
                    percentile: info.percentile.clone(),
                });
            let retreat = turn_retreat(&row.instrument_1, &row.instrument_2);
            let turn = monitor_turn(
                track_position(&pair),
                parse(&row.pair_pos_hi20),
                parse(&row.pair_pos_lo20),
                retreat,
            );
            let days_left = days_to_window_end(&row.contract_1, &row.contract_2, row.trade_date);
            let is_new_turn = monitor_turn_is_new(turn, prev_pair, retreat);
            let turn_crosses = match turn {
                Some("high") => row.turn_crosses_high_20,
                Some("low") => row.turn_crosses_low_20,
                _ => None,
            };

            // 计数是 Copy、点数是短字符串 clone 一下，都不会妨碍下面把 row 的其余
            // 字段移走。统计与阈值无关，所以这里不再挑档位。
            let stats_for = |side: &'static str| {
                if side == "high" {
                    revert_stats(
                        side,
                        row.revert_high_hit,
                        row.revert_high_n,
                        row.revert_high_move.clone(),
                        row.revert_high_drift.clone(),
                        row.revert_high_mae.clone(),
                        row.revert_high_mae_max.clone(),
                        row.revert_high_days,
                    )
                } else {
                    revert_stats(
                        side,
                        row.revert_low_hit,
                        row.revert_low_n,
                        row.revert_low_move.clone(),
                        row.revert_low_drift.clone(),
                        row.revert_low_mae.clone(),
                        row.revert_low_mae_max.clone(),
                        row.revert_low_days,
                    )
                }
            };

            // **拐头侧优先**(DEC-088,2026-08-19 修 BUG)。原来是 `alert.or(turn)`,
            // 想的是「报警侧更贴近当下」;但 ⚡ 进场是**拐头**触发的,资格却拿报警侧
            // 的统计去判——两侧相反时,合格标说的是 A 方向,进场标说的是 B 方向,
            // 乘在一起就放行了一笔没有任何统计支持的交易。
            //
            // 实例 JM2612−JM2705 @2026-08-06:历年轨 3.6% 报低位、当年轨自 100% 退到
            // 70.8% 拐头报高位。显示的是低位侧(13/13、持到期 +45,合格),⚡ 指的却是
            // 高位侧(做空),而高位侧持到期 −45 本该判不合格。此后两周价差从 −155 走到
            // −71.5,涨 83.5 点——做空方向反了。
            //
            // 没拐头只报警的行仍按报警侧给统计:那是「机会出现、还没到上车点」,
            // 本来就不该有 ⚡,给统计是为了让人提前看数字。
            let trade_side = trade_side(alert, turn);
            let revert = trade_side.and_then(stats_for);
            // 另一侧只在**两侧方向相反**时给:这正是上面那个 BUG 的现场,藏起来就是
            // 藏证据。方向一致时给它只会让页面多出一串同义数字。
            let shelves = build_shelves(
                row.shelves.as_deref(),
                row.spread.parse::<f64>().ok(),
                row.spread_sigma
                    .as_deref()
                    .and_then(|v| v.parse::<f64>().ok()),
                days_left,
                trade_side,
            );
            let revert_alt = match (alert, turn) {
                (Some(a), Some(t)) if a != t => stats_for(a),
                _ => None,
            };

            SpreadMonitorItem {
                trade_date: row.trade_date.to_string(),
                instrument_1: row.instrument_1,
                contract_1: row.contract_1,
                instrument_2: row.instrument_2,
                contract_2: row.contract_2,
                is_cross_variety: row.is_cross_variety,
                spread: row.spread,
                pair,
                years,
                alert: alert.map(str::to_string),
                is_new_alert,
                revert,
                revert_alt,
                shelves,
                turn: turn.map(str::to_string),
                is_new_turn,
                turn_crosses,
                days_left,
                note,
                expired,
                basis,
            }
        })
        .collect();

    // as_of 在过滤前算:历史模式下 items 只剩进场候选行,拿它的 max 会把
    // 「最新快照」显示成最后一次进场的日子。
    let as_of = items.iter().map(|item| item.trade_date.clone()).max();

    // 历史视图只回传进场候选行(今天刚穿线且是本轮首次):快照回填到组合整个
    // 生命周期后全量行有上万,整包返回太重。资格与红线的最终判定仍在前端
    // isEntry 一处,这里只做粗筛,不产生第二份口径。
    let items: Vec<SpreadMonitorItem> = if query.history.unwrap_or(false) {
        items
            .into_iter()
            .filter(|item| item.is_new_turn && item.turn_crosses == Some(1))
            .collect()
    } else {
        items
    };

    Ok(Json(ApiResponse::new(
        SpreadMonitorResponse {
            threshold: threshold.to_string(),
            as_of,
            available_dates: dates.iter().map(ToString::to_string).collect(),
            items,
        },
        request_id,
    ))
    .into_response())
}
