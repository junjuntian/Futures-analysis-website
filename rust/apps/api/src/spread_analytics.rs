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
use domain::seat_cost::{DailyPosition, build_cost_series};
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
use std::{collections::BTreeSet, sync::Arc};
use time::{Date, OffsetDateTime, UtcOffset};
use tracing::warn;
use utoipa::ToSchema;
use uuid::Uuid;

#[derive(Clone)]
pub struct SpreadAnalyticsState {
    pub auth: Arc<AuthState>,
    pub provider: Arc<dyn SpreadSeriesProvider>,
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
        400,
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
    pub long_position: String,
    pub short_position: String,
    pub net_position: String,
    /// 净持仓成本（推算），不是成交均价——我们看不到成交明细。
    pub cost: Option<String>,
    pub daily_pnl: Option<String>,
    pub open_pnl: Option<String>,
    pub cost_unknown_reason: Option<String>,
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
    pub days: Vec<BuildingDayItem>,
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

    let mut days = Vec::new();
    if !member.is_empty() {
        let raw = database::spread_analytics::load_building_days(
            &state.auth.pool,
            context.workspace_id(),
            &instrument,
            &member,
            contract.as_deref(),
        )
        .await
        .map_err(|_| SpreadApiError::Internal(request_id))?;
        // 没有点值就不算盈亏：宁可少一条曲线，也不要一条乘错倍数的曲线。
        let factor: Decimal = multiplier
            .as_deref()
            .and_then(|value| value.parse().ok())
            .unwrap_or(Decimal::ZERO);
        let positions: Vec<_> = raw
            .iter()
            .map(|row| DailyPosition {
                trade_date: row.trade_date,
                net_position: parse_decimal(&row.long_position)
                    - parse_decimal(&row.short_position),
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
                net_position: cost.net_position.to_string(),
                cost: cost.cost.map(|value| value.round_dp(4).to_string()),
                daily_pnl: cost.daily_pnl.map(|value| value.round_dp(2).to_string()),
                open_pnl: cost.open_pnl.map(|value| value.round_dp(2).to_string()),
                cost_unknown_reason: cost.cost_unknown_reason.map(str::to_string),
            })
            .collect();
    }

    Ok(Json(ApiResponse::new(
        SeatBuildingResponse {
            instrument,
            member,
            is_variety_total: contract.is_none(),
            contract,
            price_multiplier: multiplier,
            members,
            days,
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
