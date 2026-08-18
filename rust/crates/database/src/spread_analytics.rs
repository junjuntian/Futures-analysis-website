use application::spread_analytics::ProviderEndpoint;
use domain::spread_analytics::{
    ContractWindowInfo, DEFAULT_RULE_VERSION, STATISTICS_ALGORITHM_VERSION,
    WINDOW_ALGORITHM_VERSION, WindowedSpreadAnalytics,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::{PgPool, Postgres, Row, Transaction};
use std::{collections::HashMap, time::Duration as StdDuration};
use time::{Date, Duration, OffsetDateTime};
use uuid::Uuid;

const SANHE_PROVIDER: &str = "sanhe";
const PROVIDER_REQUEST_INTERVAL_MS: i64 = 2_000;
/// Business dates kept per cached parameter set.
///
/// The cache is keyed by business date, so a combination queried every day
/// stores a fresh copy of the whole series every day — around 0.77 MB of
/// observations each, which for the combinations under watch is several
/// gigabytes a year of identical history. Two is the smallest number that
/// still leaves something to fall back on when the upstream is unreachable
/// while today's copy has not been fetched yet.
const CACHE_BUSINESS_DATES_KEPT: i64 = 2;

#[derive(Debug, Clone)]
pub struct CachedProviderPayload {
    pub payload: Value,
    pub fetched_at: OffsetDateTime,
    pub result_kind: String,
    pub payload_hash: String,
}

#[derive(Debug, Clone)]
pub struct NewProviderCache<'a> {
    pub endpoint: ProviderEndpoint,
    pub parameter_hash: &'a str,
    pub parameters: &'a Value,
    pub business_date: Date,
    pub fetched_at: OffsetDateTime,
    pub http_status: u16,
    pub business_code: i64,
    pub payload: &'a Value,
    pub result_kind: &'a str,
    pub payload_hash: &'a str,
}

#[derive(Debug, Clone)]
pub struct FailureSuppression {
    pub stable_error_code: String,
    pub retry_after_seconds: u64,
}

#[derive(Debug, Clone)]
pub struct SeriesPersistence<'a> {
    pub workspace_id: Uuid,
    pub actor_user_id: Uuid,
    pub request_id: Uuid,
    pub query_hash: &'a str,
    pub business_date: Date,
    pub query_json: &'a Value,
    pub fetched_at: OffsetDateTime,
    pub data_cutoff_at: Option<Date>,
    pub payload_hash: &'a str,
    pub derivation_hash: &'a str,
    pub analytics: &'a WindowedSpreadAnalytics,
    /// 这条序列是我们自己算的还是三禾给的。决定落账时的 provider、口径和
    /// 有没有外部来源——三者必须一致，库里的约束会兜住写错的情况。
    pub own_engine: bool,
}

#[derive(Debug, Clone)]
pub struct NewFavorite<'a> {
    pub workspace_id: Uuid,
    /// 收藏是在哪条来源下存的。存错了，切回三禾比对时两边就对不上账。
    pub provider_code: &'a str,
    pub actor_user_id: Uuid,
    pub request_id: Uuid,
    pub name: &'a str,
    pub leg1: &'a FavoriteLeg,
    pub leg2: &'a FavoriteLeg,
    pub normalized_hash: &'a str,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct FavoriteLeg {
    pub variety: String,
    pub symbol: String,
    pub month: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct FavoriteRecord {
    pub id: Uuid,
    pub name: String,
    pub provider: String,
    pub leg1: FavoriteLeg,
    pub leg2: FavoriteLeg,
    pub created_at: OffsetDateTime,
}

#[derive(Debug, thiserror::Error)]
pub enum SpreadRepositoryError {
    #[error("favorite already exists")]
    FavoriteConflict,
    #[error("favorite is not visible")]
    FavoriteNotFound,
    #[error("database operation failed")]
    Database(#[from] sqlx::Error),
    #[error("stored provider data is invalid")]
    InvalidStoredData,
}

pub async fn get_cache(
    pool: &PgPool,
    endpoint: ProviderEndpoint,
    parameter_hash: &str,
    business_date: Date,
) -> Result<Option<CachedProviderPayload>, sqlx::Error> {
    let row = sqlx::query(
        "select payload_json, fetched_at, result_kind, payload_hash
           from spread_provider_cache
          where provider_code = $1 and endpoint_code = $2
            and parameter_hash = $3 and business_date = $4",
    )
    .bind(SANHE_PROVIDER)
    .bind(endpoint.code())
    .bind(parameter_hash)
    .bind(business_date)
    .fetch_optional(pool)
    .await?;
    Ok(row.map(|row| CachedProviderPayload {
        payload: row.get("payload_json"),
        fetched_at: row.get("fetched_at"),
        result_kind: row.get("result_kind"),
        payload_hash: row.get("payload_hash"),
    }))
}

pub async fn begin_cache_fill(
    pool: &PgPool,
    endpoint: ProviderEndpoint,
    parameter_hash: &str,
) -> Result<Transaction<'static, Postgres>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    let lock_identity = format!("{SANHE_PROVIDER}:{}:{parameter_hash}", endpoint.code());
    sqlx::query("select pg_advisory_xact_lock(hashtextextended($1, 50042))")
        .bind(lock_identity)
        .execute(&mut *tx)
        .await?;
    Ok(tx)
}

pub async fn get_cache_in_transaction(
    tx: &mut Transaction<'_, Postgres>,
    endpoint: ProviderEndpoint,
    parameter_hash: &str,
    business_date: Date,
) -> Result<Option<CachedProviderPayload>, sqlx::Error> {
    let row = sqlx::query(
        "select payload_json, fetched_at, result_kind, payload_hash
           from spread_provider_cache
          where provider_code = $1 and endpoint_code = $2
            and parameter_hash = $3 and business_date = $4",
    )
    .bind(SANHE_PROVIDER)
    .bind(endpoint.code())
    .bind(parameter_hash)
    .bind(business_date)
    .fetch_optional(&mut **tx)
    .await?;
    Ok(row.map(|row| CachedProviderPayload {
        payload: row.get("payload_json"),
        fetched_at: row.get("fetched_at"),
        result_kind: row.get("result_kind"),
        payload_hash: row.get("payload_hash"),
    }))
}

pub async fn store_cache(
    pool: &PgPool,
    cache: &NewProviderCache<'_>,
) -> Result<CachedProviderPayload, sqlx::Error> {
    sqlx::query(
        "insert into spread_provider_cache
            (id, provider_code, endpoint_code, parameter_hash, parameters_json,
             business_date, fetched_at, http_status, business_code, payload_json,
             result_kind, payload_hash)
         values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
         on conflict (provider_code, endpoint_code, parameter_hash, business_date)
         do nothing",
    )
    .bind(Uuid::now_v7())
    .bind(SANHE_PROVIDER)
    .bind(cache.endpoint.code())
    .bind(cache.parameter_hash)
    .bind(cache.parameters)
    .bind(cache.business_date)
    .bind(cache.fetched_at)
    .bind(i32::from(cache.http_status))
    .bind(i32::try_from(cache.business_code).unwrap_or(i32::MAX))
    .bind(cache.payload)
    .bind(cache.result_kind)
    .bind(cache.payload_hash)
    .execute(pool)
    .await?;
    clear_failure(pool, cache.endpoint, cache.parameter_hash).await?;
    // Trimming old business dates is housekeeping. It must never be able to
    // fail the request that triggered it: the payload is already stored and
    // the caller wants it, so a pruning problem is a reason to keep an extra
    // copy, not a reason to answer 500.
    if let Err(error) = prune_cache(pool, cache.endpoint, cache.parameter_hash).await {
        tracing::warn!(
            provider = SANHE_PROVIDER,
            endpoint = cache.endpoint.code(),
            %error,
            "could not trim the provider cache; leaving the older copies in place"
        );
    }
    // PostgreSQL canonicalizes timestamptz precision, and an immutable same-day row may
    // already have won the conflict. Always return the persisted row so the cache-fill
    // response and every later cache hit expose exactly the same payload and metadata.
    get_cache(
        pool,
        cache.endpoint,
        cache.parameter_hash,
        cache.business_date,
    )
    .await?
    .ok_or(sqlx::Error::RowNotFound)
}

/// Drop all but the newest few business dates for one cached parameter set.
///
/// Pruning on write rather than on a schedule keeps the bound tied to the thing
/// that grows it, so a combination that stops being queried simply stops
/// accumulating instead of needing a sweep to find it.
pub async fn prune_cache(
    pool: &PgPool,
    endpoint: ProviderEndpoint,
    parameter_hash: &str,
) -> Result<u64, sqlx::Error> {
    let deleted = sqlx::query(
        "delete from spread_provider_cache
          where provider_code = $1 and endpoint_code = $2 and parameter_hash = $3
            and business_date not in (
                select business_date
                  from spread_provider_cache
                 where provider_code = $1 and endpoint_code = $2 and parameter_hash = $3
                 order by business_date desc
                 limit $4
            )",
    )
    .bind(SANHE_PROVIDER)
    .bind(endpoint.code())
    .bind(parameter_hash)
    .bind(CACHE_BUSINESS_DATES_KEPT)
    .execute(pool)
    .await?;
    Ok(deleted.rows_affected())
}

/// The newest cached payload for a parameter set, whatever business date it is
/// from.
///
/// Used only when the upstream refuses: serving yesterday's series with its own
/// fetch time on it beats serving an error page, as long as the age is
/// disclosed rather than passed off as current.
pub async fn get_latest_cache(
    pool: &PgPool,
    endpoint: ProviderEndpoint,
    parameter_hash: &str,
) -> Result<Option<CachedProviderPayload>, sqlx::Error> {
    let row = sqlx::query(
        "select payload_json, fetched_at, result_kind, payload_hash
           from spread_provider_cache
          where provider_code = $1 and endpoint_code = $2 and parameter_hash = $3
          order by business_date desc
          limit 1",
    )
    .bind(SANHE_PROVIDER)
    .bind(endpoint.code())
    .bind(parameter_hash)
    .fetch_optional(pool)
    .await?;
    Ok(row.map(|row| CachedProviderPayload {
        payload: row.get("payload_json"),
        fetched_at: row.get("fetched_at"),
        result_kind: row.get("result_kind"),
        payload_hash: row.get("payload_hash"),
    }))
}

pub async fn active_failure(
    pool: &PgPool,
    endpoint: ProviderEndpoint,
    parameter_hash: &str,
) -> Result<Option<FailureSuppression>, sqlx::Error> {
    let now = OffsetDateTime::now_utc();
    let row = sqlx::query(
        "select stable_error_code, suppressed_until
           from spread_provider_failures
          where provider_code = $1 and endpoint_code = $2 and parameter_hash = $3
            and suppressed_until > $4",
    )
    .bind(SANHE_PROVIDER)
    .bind(endpoint.code())
    .bind(parameter_hash)
    .bind(now)
    .fetch_optional(pool)
    .await?;
    Ok(row.map(|row| {
        let suppressed_until: OffsetDateTime = row.get("suppressed_until");
        let seconds = (suppressed_until - now).whole_seconds().max(1) as u64;
        FailureSuppression {
            stable_error_code: row.get("stable_error_code"),
            retry_after_seconds: seconds,
        }
    }))
}

pub async fn reserve_request_slot(pool: &PgPool) -> Result<StdDuration, sqlx::Error> {
    let mut tx = pool.begin().await?;
    sqlx::query(
        "insert into spread_provider_throttles (provider_code)
         values ($1) on conflict (provider_code) do nothing",
    )
    .bind(SANHE_PROVIDER)
    .execute(&mut *tx)
    .await?;
    let row = sqlx::query(
        "select last_requested_at, suppressed_until
           from spread_provider_throttles
          where provider_code = $1
          for update",
    )
    .bind(SANHE_PROVIDER)
    .fetch_one(&mut *tx)
    .await?;
    let now = OffsetDateTime::now_utc();
    let last: Option<OffsetDateTime> = row.get("last_requested_at");
    let suppressed: Option<OffsetDateTime> = row.get("suppressed_until");
    let mut not_before = now;
    if let Some(last) = last {
        not_before = not_before.max(last + Duration::milliseconds(PROVIDER_REQUEST_INTERVAL_MS));
    }
    if let Some(suppressed) = suppressed {
        not_before = not_before.max(suppressed);
    }
    sqlx::query(
        "update spread_provider_throttles
            set last_requested_at = $2, updated_at = now()
          where provider_code = $1",
    )
    .bind(SANHE_PROVIDER)
    .bind(not_before)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    let milliseconds = (not_before - now).whole_milliseconds().max(0) as u64;
    Ok(StdDuration::from_millis(milliseconds))
}

pub async fn record_failure(
    pool: &PgPool,
    endpoint: ProviderEndpoint,
    parameter_hash: &str,
    stable_error_code: &str,
    retry_after_seconds: u64,
) -> Result<(), sqlx::Error> {
    let now = OffsetDateTime::now_utc();
    let seconds = retry_after_seconds.clamp(60, 86_400);
    let suppressed_until = now + Duration::seconds(i64::try_from(seconds).unwrap_or(86_400));
    let mut tx = pool.begin().await?;
    sqlx::query(
        "insert into spread_provider_failures
            (provider_code, endpoint_code, parameter_hash, stable_error_code,
             occurred_at, suppressed_until)
         values ($1, $2, $3, $4, $5, $6)
         on conflict (provider_code, endpoint_code, parameter_hash)
         do update set stable_error_code = excluded.stable_error_code,
                       occurred_at = excluded.occurred_at,
                       suppressed_until = excluded.suppressed_until,
                       updated_at = now()",
    )
    .bind(SANHE_PROVIDER)
    .bind(endpoint.code())
    .bind(parameter_hash)
    .bind(stable_error_code)
    .bind(now)
    .bind(suppressed_until)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(())
}

async fn clear_failure(
    pool: &PgPool,
    endpoint: ProviderEndpoint,
    parameter_hash: &str,
) -> Result<(), sqlx::Error> {
    let mut tx = pool.begin().await?;
    sqlx::query(
        "delete from spread_provider_failures
          where provider_code = $1 and endpoint_code = $2 and parameter_hash = $3",
    )
    .bind(SANHE_PROVIDER)
    .bind(endpoint.code())
    .bind(parameter_hash)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "update spread_provider_throttles
            set suppressed_until = null, updated_at = now()
          where provider_code = $1 and suppressed_until <= now()",
    )
    .bind(SANHE_PROVIDER)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(())
}

pub async fn ensure_sanhe_source(pool: &PgPool, workspace_id: Uuid) -> Result<Uuid, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let source_id = sqlx::query_scalar::<_, Uuid>(
        "insert into data_sources
            (id, workspace_id, code, name, source_type, base_domain,
             authorization_status, connector_code, priority)
         values ($1, $2, 'sanhe_spread_readonly', '三禾数据', 'aggregator',
                 'sanheshuju.com', 'user_authorized_readonly', 'sanhe_spread_v1', 100)
         on conflict (workspace_id, code) do update
            set name = excluded.name, updated_at = now()
          where data_sources.source_type = 'aggregator'
            and data_sources.base_domain = 'sanheshuju.com'
            and data_sources.authorization_status = 'user_authorized_readonly'
            and data_sources.connector_code = 'sanhe_spread_v1'
         returning id",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .fetch_one(&mut *tx)
    .await?;
    sqlx::query(
        "insert into data_source_allowed_domains
            (id, workspace_id, data_source_id, domain)
         values ($1, $2, $3, 'www.sanheshuju.com')
         on conflict (workspace_id, data_source_id, domain) do nothing",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(source_id)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(source_id)
}

pub async fn resolve_contract_windows(
    pool: &PgPool,
    workspace_id: Uuid,
    codes: &[String],
) -> Result<HashMap<String, ContractWindowInfo>, sqlx::Error> {
    if codes.is_empty() {
        return Ok(HashMap::new());
    }
    let normalized: Vec<_> = codes.iter().map(|code| code.to_ascii_uppercase()).collect();
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(
        "select upper(contract.code) as code,
                upper(instrument.code) as instrument_code,
                upper(exchange.code) as exchange_code,
                extract(year from delivery.delivery_date)::integer as delivery_year,
                extract(month from delivery.delivery_date)::integer as delivery_month,
                deadline.trade_date as retail_deadline,
                deadline.calendar_version_id
           from contracts contract
           join instruments instrument
             on instrument.workspace_id = contract.workspace_id
            and instrument.id = contract.instrument_id
           join exchanges exchange
             on exchange.workspace_id = instrument.workspace_id
            and exchange.id = instrument.exchange_id
           cross join lateral (
                select to_date(contract.delivery_month || '-01', 'YYYY-MM-DD') as delivery_date
           ) delivery
           left join lateral (
                select rule.rule_json
                  from retail_trade_window_rules rule
                  join retail_trade_window_rule_versions version
                    on version.id = rule.rule_version_id
                 where version.version = $3
                   and version.status = 'active'
                   and (rule.exchange_code is null or rule.exchange_code = upper(exchange.code))
                   and (rule.instrument_code is null or rule.instrument_code = upper(instrument.code))
                 order by (rule.instrument_code is not null) desc, rule.priority asc
                 limit 1
           ) selected_rule on true
           cross join lateral (
                select date_trunc(
                           'month',
                           delivery.delivery_date::timestamp
                           + make_interval(months => coalesce(
                               (selected_rule.rule_json ->> 'month_offset')::integer,
                               -1
                           ))
                       )::date as month_start
           ) deadline_month
           cross join lateral (
                select (deadline_month.month_start
                        + interval '1 month - 1 day')::date as month_end
           ) deadline_month_end
           left join lateral (
                select day.trade_date, calendar.id as calendar_version_id
                  from trading_calendar_days day
                  join trading_calendar_versions calendar
                    on calendar.workspace_id = day.workspace_id
                   and calendar.id = day.calendar_version_id
                 where day.workspace_id = contract.workspace_id
                   and calendar.exchange_id = exchange.id
                   and calendar.id = (
                       select selected_calendar.id
                         from trading_calendar_versions selected_calendar
                        where selected_calendar.workspace_id = contract.workspace_id
                          and selected_calendar.exchange_id = exchange.id
                          and selected_calendar.effective_from <= deadline_month_end.month_end
                        order by selected_calendar.effective_from desc,
                                 selected_calendar.created_at desc,
                                 selected_calendar.id desc
                        limit 1
                   )
                   and day.is_trading_day
                   and date_trunc('month', day.trade_date::timestamp)
                       = deadline_month.month_start
                   -- The calendar only holds days the collector has actually
                   -- seen, so a month it is still filling looks exactly like a
                   -- complete one: its newest row silently passes for the last
                   -- trading day of the month and the retail window closes
                   -- weeks early. Trust the month only once the calendar has
                   -- moved past it; otherwise fall back to the last weekday
                   -- before delivery in Rust.
                   and exists (
                       select 1
                         from trading_calendar_days probe
                        where probe.workspace_id = day.workspace_id
                          and probe.calendar_version_id = day.calendar_version_id
                          and probe.trade_date > deadline_month_end.month_end
                   )
                 order by
                   case when coalesce((selected_rule.rule_json ->> 'trading_day_ordinal')::integer, -1) > 0
                        then day.trade_date end asc,
                   case when coalesce((selected_rule.rule_json ->> 'trading_day_ordinal')::integer, -1) < 0
                        then day.trade_date end desc,
                   day.trade_date asc
                 offset greatest(abs(coalesce(
                     (selected_rule.rule_json ->> 'trading_day_ordinal')::integer,
                     -1
                 )) - 1, 0)
                 limit 1
           ) deadline on true
          where contract.workspace_id = $1
            and upper(contract.code) = any($2)
            and contract.delivery_month is not null",
    )
    .bind(workspace_id)
    .bind(&normalized)
    .bind(DEFAULT_RULE_VERSION)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    let mut resolved = HashMap::new();
    for row in rows {
        let deadline: Option<Date> = row.get("retail_deadline");
        let calendar_version_id: Option<Uuid> = row.get("calendar_version_id");
        // The stored calendar only covers days the collector has actually
        // seen, so most deadline months have no calendar rows yet -- and a
        // month the collector is still filling is deliberately rejected by the
        // query above, because its newest row would masquerade as the month's
        // last trading day.  Fall back to the last non-weekend day of the
        // month before delivery: any divergence from the exchange calendar
        // lands on holidays that carry no price points anyway, so the window
        // maths are unaffected.  The nil calendar version marks the
        // approximation.
        let (retail_deadline, calendar_version_id) = match (deadline, calendar_version_id) {
            (Some(deadline), Some(calendar_version_id)) => (deadline, calendar_version_id),
            _ => {
                let delivery_year: i32 = row.get("delivery_year");
                let delivery_month: i32 = row.get("delivery_month");
                let Some(fallback) = fallback_retail_deadline(delivery_year, delivery_month) else {
                    continue;
                };
                (fallback, Uuid::nil())
            }
        };
        let code: String = row.get("code");
        resolved.insert(
            code.clone(),
            ContractWindowInfo {
                code,
                instrument_code: row.get("instrument_code"),
                exchange_code: row.get("exchange_code"),
                delivery_year: row.get("delivery_year"),
                delivery_month: u8::try_from(row.get::<i32, _>("delivery_month")).unwrap_or(0),
                retail_deadline,
                calendar_version_id,
            },
        );
    }
    Ok(resolved)
}

fn fallback_retail_deadline(delivery_year: i32, delivery_month: i32) -> Option<Date> {
    let (year, month) = if delivery_month == 1 {
        (delivery_year - 1, 12u8)
    } else {
        (delivery_year, u8::try_from(delivery_month - 1).ok()?)
    };
    let first_of_month =
        Date::from_calendar_date(year, time::Month::try_from(month).ok()?, 1).ok()?;
    let mut day = first_of_month
        .replace_day(1)
        .ok()?
        .checked_add(Duration::days(31))?
        .replace_day(1)
        .ok()?
        .checked_sub(Duration::days(1))?;
    if day.month() as u8 != month {
        day = day.replace_day(1).ok()?.checked_sub(Duration::days(1))?;
    }
    while matches!(
        day.weekday(),
        time::Weekday::Saturday | time::Weekday::Sunday
    ) {
        day = day.checked_sub(Duration::days(1))?;
    }
    Some(day)
}

#[cfg(test)]
mod fallback_tests {
    use super::fallback_retail_deadline;
    use time::macros::date;

    #[test]
    fn falls_back_to_last_weekday_of_prior_month() {
        assert_eq!(
            fallback_retail_deadline(2026, 9),
            Some(date!(2026 - 08 - 31))
        );
        assert_eq!(
            fallback_retail_deadline(2027, 1),
            Some(date!(2026 - 12 - 31))
        );
        assert_eq!(
            fallback_retail_deadline(2026, 6),
            Some(date!(2026 - 05 - 29))
        );
        assert_eq!(
            fallback_retail_deadline(2026, 12),
            Some(date!(2026 - 11 - 30))
        );
    }
}

/// 序列留存清理。同样提成常量供测试断言——理由见 `OWN_SPREAD_POINTS_SQL`。
///
/// 只清自己这一路的：三禾与自研的 query_hash 可能相同（同一组腿），
/// 不按 provider 分开会把另一路的历史一并删掉。
const SERIES_RETENTION_SQL: &str = "delete from spread_provider_series
          where workspace_id = $1 and provider_code = $4 and query_hash = $2
            and business_date not in (
                select business_date
                  from spread_provider_series
                 where workspace_id = $1 and provider_code = $4 and query_hash = $2
                 order by business_date desc
                 limit $3
            )";

/// 来源与口径成对出现，库里的约束也是成对校验的，所以只在这一处决定。
fn provider_and_basis(own_engine: bool) -> (&'static str, &'static str) {
    if own_engine {
        ("self", "own_close_difference")
    } else {
        ("sanhe", "upstream_spread")
    }
}

pub async fn save_series(
    pool: &PgPool,
    input: &SeriesPersistence<'_>,
) -> Result<Uuid, SpreadRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, input.workspace_id).await?;
    // 自己算的没有外部来源，不去 ensure 三禾那条 data_sources——挂上去就是假账。
    let source_id = if input.own_engine {
        None
    } else {
        Some(ensure_sanhe_source_in_tx(&mut tx, input.workspace_id).await?)
    };
    let (provider_code, price_basis) = provider_and_basis(input.own_engine);
    let new_id = Uuid::now_v7();
    let inserted = sqlx::query_scalar::<_, Uuid>(
        "insert into spread_provider_series
            (id, workspace_id, provider_code, source_id, query_hash, business_date,
             query_json, fetched_at, data_cutoff_at, payload_hash, derivation_hash, price_basis,
             window_algorithm_version, statistics_algorithm_version, rule_version, created_by)
         values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11,
                 $12, $13, $14, $15, $16)
         on conflict (workspace_id, provider_code, query_hash, business_date, derivation_hash)
         do nothing
         returning id",
    )
    .bind(new_id)
    .bind(input.workspace_id)
    .bind(provider_code)
    .bind(source_id)
    .bind(input.query_hash)
    .bind(input.business_date)
    .bind(input.query_json)
    .bind(input.fetched_at)
    .bind(input.data_cutoff_at)
    .bind(input.payload_hash)
    .bind(input.derivation_hash)
    .bind(price_basis)
    .bind(WINDOW_ALGORITHM_VERSION)
    .bind(STATISTICS_ALGORITHM_VERSION)
    .bind(DEFAULT_RULE_VERSION)
    .bind(input.actor_user_id)
    .fetch_optional(&mut *tx)
    .await?;
    let series_id = if let Some(id) = inserted {
        for observation in &input.analytics.observations {
            sqlx::query(
                "insert into spread_provider_observations
                    (workspace_id, series_id, point_seq, trade_date, spread_value,
                     from_code, to_code, segment_no, retained, exclusion_reason)
                 values ($1, $2, $3, $4, $5::numeric, $6, $7, $8, $9, $10)",
            )
            .bind(input.workspace_id)
            .bind(id)
            .bind(i32::try_from(observation.point_seq).unwrap_or(i32::MAX))
            .bind(observation.trade_date)
            .bind(format!("{:.8}", observation.value))
            .bind(&observation.from_code)
            .bind(&observation.to_code)
            .bind(
                observation
                    .segment_no
                    .and_then(|value| i32::try_from(value).ok()),
            )
            .bind(observation.retained)
            .bind(observation.exclusion_reason.map(|reason| reason.as_str()))
            .execute(&mut *tx)
            .await?;
        }
        for segment in &input.analytics.segments {
            sqlx::query(
                "insert into spread_window_segments
                    (id, workspace_id, series_id, segment_no, window_year, from_code, to_code,
                     candidate_start, candidate_end, window_start, window_end, rule_version,
                     calendar_version_ids, retained_point_count, excluded_point_count,
                     boundary_reason)
                 values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
                         $13, $14, $15, $16)",
            )
            .bind(Uuid::now_v7())
            .bind(input.workspace_id)
            .bind(id)
            .bind(i32::try_from(segment.segment_no).unwrap_or(i32::MAX))
            .bind(segment.window_year)
            .bind(&segment.from_code)
            .bind(&segment.to_code)
            .bind(segment.candidate_start)
            .bind(segment.candidate_end)
            .bind(segment.window_start)
            .bind(segment.window_end)
            .bind(DEFAULT_RULE_VERSION)
            .bind(&segment.calendar_version_ids)
            .bind(i32::try_from(segment.retained_point_count).unwrap_or(i32::MAX))
            .bind(i32::try_from(segment.excluded_point_count).unwrap_or(i32::MAX))
            .bind(&segment.boundary_reason)
            .execute(&mut *tx)
            .await?;
        }
        sqlx::query(
            "insert into audit_logs
                (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
             values ($1, $2, $3, 'spread.provider_series.created', 'success', $4,
                     jsonb_build_object('series_id', $5::text, 'provider', $6::text))",
        )
        .bind(Uuid::now_v7())
        .bind(input.workspace_id)
        .bind(input.actor_user_id)
        .bind(input.request_id)
        .bind(id)
        .bind(provider_code)
        .execute(&mut *tx)
        .await?;
        id
    } else {
        sqlx::query_scalar::<_, Uuid>(
            "select id from spread_provider_series
              where workspace_id = $1 and provider_code = $5
                and query_hash = $2 and business_date = $3 and derivation_hash = $4",
        )
        .bind(input.workspace_id)
        .bind(input.query_hash)
        .bind(input.business_date)
        .bind(input.derivation_hash)
        .bind(provider_code)
        .fetch_one(&mut *tx)
        .await?
    };
    // The observations are the bulk of this: a single thirteen-year series is
    // some three thousand rows, and one is stored per business date, so a
    // combination on the page every day accumulates the same history over and
    // over. Bound it here, inside the same transaction that grew it.
    sqlx::query(SERIES_RETENTION_SQL)
        .bind(input.workspace_id)
        .bind(input.query_hash)
        .bind(CACHE_BUSINESS_DATES_KEPT)
        .bind(provider_code)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(series_id)
}

pub async fn list_favorites(
    pool: &PgPool,
    workspace_id: Uuid,
) -> Result<Vec<FavoriteRecord>, SpreadRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(
        "select id, name, provider_code, leg1_json, leg2_json, created_at
           from spread_favorites
          where workspace_id = $1
          order by created_at desc",
    )
    .bind(workspace_id)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    rows.into_iter()
        .map(|row| {
            Ok(FavoriteRecord {
                id: row.get("id"),
                name: row.get("name"),
                provider: row.get("provider_code"),
                leg1: serde_json::from_value(row.get("leg1_json"))
                    .map_err(|_| SpreadRepositoryError::InvalidStoredData)?,
                leg2: serde_json::from_value(row.get("leg2_json"))
                    .map_err(|_| SpreadRepositoryError::InvalidStoredData)?,
                created_at: row.get("created_at"),
            })
        })
        .collect()
}

pub async fn create_favorite(
    pool: &PgPool,
    input: &NewFavorite<'_>,
) -> Result<FavoriteRecord, SpreadRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, input.workspace_id).await?;
    let id = Uuid::now_v7();
    let row = sqlx::query(
        "insert into spread_favorites
            (id, workspace_id, name, provider_code, leg1_json, leg2_json,
             normalized_hash, created_by)
         -- 占位符按绑定顺序排，不跳号：原来把新加的 provider_code 写成 $8 塞在中间，
         -- 绑定却加在了 created_by 前面，于是 uuid 列收到文本、文本列收到 uuid，
         -- 每次建收藏都 500。顺序一致就不会再有这种错位。
         values ($1, $2, $3, $4, $5, $6, $7, $8)
         returning created_at",
    )
    .bind(id)
    .bind(input.workspace_id)
    .bind(input.name)
    .bind(input.provider_code)
    .bind(serde_json::to_value(input.leg1).map_err(|_| SpreadRepositoryError::InvalidStoredData)?)
    .bind(serde_json::to_value(input.leg2).map_err(|_| SpreadRepositoryError::InvalidStoredData)?)
    .bind(input.normalized_hash)
    .bind(input.actor_user_id)
    .fetch_one(&mut *tx)
    .await
    .map_err(|error| {
        if error
            .as_database_error()
            .is_some_and(|db| db.is_unique_violation())
        {
            SpreadRepositoryError::FavoriteConflict
        } else {
            SpreadRepositoryError::Database(error)
        }
    })?;
    insert_favorite_audit(
        &mut tx,
        input.workspace_id,
        input.actor_user_id,
        input.request_id,
        id,
        "created",
    )
    .await?;
    tx.commit().await?;
    Ok(FavoriteRecord {
        id,
        name: input.name.to_string(),
        provider: SANHE_PROVIDER.to_string(),
        leg1: input.leg1.clone(),
        leg2: input.leg2.clone(),
        created_at: row.get("created_at"),
    })
}

pub async fn delete_favorite(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    request_id: Uuid,
    favorite_id: Uuid,
) -> Result<(), SpreadRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let deleted = sqlx::query("delete from spread_favorites where workspace_id = $1 and id = $2")
        .bind(workspace_id)
        .bind(favorite_id)
        .execute(&mut *tx)
        .await?
        .rows_affected();
    if deleted == 0 {
        return Err(SpreadRepositoryError::FavoriteNotFound);
    }
    insert_favorite_audit(
        &mut tx,
        workspace_id,
        actor_user_id,
        request_id,
        favorite_id,
        "deleted",
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

async fn ensure_sanhe_source_in_tx(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
) -> Result<Uuid, sqlx::Error> {
    let source_id = sqlx::query_scalar::<_, Uuid>(
        "insert into data_sources
            (id, workspace_id, code, name, source_type, base_domain,
             authorization_status, connector_code, priority)
         values ($1, $2, 'sanhe_spread_readonly', '三禾数据', 'aggregator',
                 'sanheshuju.com', 'user_authorized_readonly', 'sanhe_spread_v1', 100)
         on conflict (workspace_id, code) do update
            set name = excluded.name, updated_at = now()
          where data_sources.source_type = 'aggregator'
            and data_sources.base_domain = 'sanheshuju.com'
            and data_sources.authorization_status = 'user_authorized_readonly'
            and data_sources.connector_code = 'sanhe_spread_v1'
         returning id",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .fetch_one(&mut **tx)
    .await?;
    sqlx::query(
        "insert into data_source_allowed_domains
            (id, workspace_id, data_source_id, domain)
         values ($1, $2, $3, 'www.sanheshuju.com')
         on conflict (workspace_id, data_source_id, domain) do nothing",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(source_id)
    .execute(&mut **tx)
    .await?;
    Ok(source_id)
}

async fn insert_favorite_audit(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    request_id: Uuid,
    favorite_id: Uuid,
    action: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "insert into audit_logs
            (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, $4, 'success', $5,
                 jsonb_build_object('favorite_id', $6::text))",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(format!("spread.favorite.{action}"))
    .bind(request_id)
    .bind(favorite_id)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

async fn set_workspace(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
) -> Result<(), sqlx::Error> {
    sqlx::query("select set_config('app.current_workspace_id', $1, true)")
        .bind(workspace_id.to_string())
        .execute(&mut **tx)
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use sqlx::postgres::{PgConnectOptions, PgPoolOptions};
    use std::{str::FromStr, time::Instant};

    const MIGRATION: &str =
        include_str!("../../../migrations/202608050001_phase_5a_spread_provider.sql");
    const RETENTION_GRANTS: &str =
        include_str!("../../../migrations/202608100005_spread_retention_delete_grants.sql");
    const SELF_PROVIDER: &str =
        include_str!("../../../migrations/202608100009_self_spread_provider.sql");

    #[test]
    fn every_insert_numbers_its_placeholders_in_order() {
        // 跳号的占位符要求读代码的人在心里把列顺序和绑定顺序对齐一遍。我对错过
        // 一次：新加的 provider_code 编号跳到末尾却塞在列表中间，绑定又加在
        // created_by 前面，于是 uuid 列收到文本，每次建收藏都 500——静态测试看不见，
        // 只有真连库的验收才会红。
        //
        // 注意：这段注释里不能出现占位符列表的样子，否则会被下面这段扫到自己身上。
        // 同一个坑今天已经踩到第三次——凡是 include_str! 自己源码的测试，
        // 锚点都必须是生产代码里才会出现的形状。
        //
        // 这里不判断绑定对不对（那要连库），只要求占位符从 $1 起顺序排列：
        // 顺序一致时，列顺序就是绑定顺序，错位无从发生。
        let source = include_str!("spread_analytics.rs");
        for (index, chunk) in source.split("values (").enumerate().skip(1) {
            let list = &chunk[..chunk.find(')').unwrap_or(0)];
            if !list.starts_with('$') {
                continue; // 不是占位符列表，例如 values ('a', 'b')
            }
            let numbers: Vec<u32> = list
                .split(',')
                .filter_map(|piece| piece.trim().strip_prefix('$'))
                .filter_map(|piece| {
                    piece
                        .split(|c: char| !c.is_ascii_digit())
                        .next()
                        .filter(|value| !value.is_empty())
                        .and_then(|value| value.parse().ok())
                })
                .collect();
            if numbers.is_empty() {
                continue;
            }
            let expected: Vec<u32> = (1..=numbers.len() as u32).collect();
            assert_eq!(
                numbers, expected,
                "第 {index} 处 insert 的占位符跳号了：{list}"
            );
        }
    }

    #[test]
    fn self_computed_series_are_not_recorded_as_coming_from_sanhe() {
        // 来源、口径、外部来源三者必须一起翻。任何一处漏了，落账就在撒谎：
        // 我们自己用两腿收盘价算的东西，记录上写着是三禾给的。
        assert_eq!(provider_and_basis(true), ("self", "own_close_difference"));
        assert_eq!(provider_and_basis(false), ("sanhe", "upstream_spread"));

        // 库里也得挡住写反的情况，光靠这一处函数不够——迁移必须双向绑定。
        assert!(
            SELF_PROVIDER.contains("(provider_code = 'self') = (source_id is null)"),
            "自研序列必须没有外部来源，外部序列必须有，缺一个方向都能写进假账"
        );
        assert!(
            SELF_PROVIDER
                .contains("provider_code = 'self' and price_basis = 'own_close_difference'")
                && SELF_PROVIDER
                    .contains("provider_code = 'sanhe' and price_basis = 'upstream_spread'"),
            "口径必须跟着来源走，不允许错配"
        );
    }

    #[test]
    fn gold_and_silver_are_weighted_everything_else_is_dominant() {
        // 运营者定的：金银用加权，其余用主连。
        for weighted in ["AU", "AG"] {
            assert_eq!(
                VarietyCandleMode::for_instrument(weighted),
                VarietyCandleMode::OpenInterestWeighted
            );
        }
        for dominant in ["CU", "RB", "I", "AP", "JM", "A"] {
            assert_eq!(
                VarietyCandleMode::for_instrument(dominant),
                VarietyCandleMode::DominantUnadjusted
            );
        }
    }

    #[test]
    fn candle_mode_names_match_the_frontend_union_type() {
        // 前端 api.ts 里 price_series_kind 就是这两个字面量的联合类型，改这里要改那里。
        assert_eq!(
            VarietyCandleMode::OpenInterestWeighted.as_str(),
            "open_interest_weighted"
        );
        assert_eq!(
            VarietyCandleMode::DominantUnadjusted.as_str(),
            "dominant_unadjusted"
        );
    }

    #[test]
    fn weighted_candles_take_numerator_and_denominator_from_the_same_contracts() {
        // 分子分母必须落在同一批合约上。某个合约缺开盘价却把它的持仓量计进分母，
        // 算出来的开盘价会被系统性拉低——图上就是一根不存在的低开。
        let sql = variety_candles_sql(VarietyCandleMode::OpenInterestWeighted);
        assert!(sql.contains("open_price is not null and high_price is not null"));
        assert!(sql.contains("low_price is not null and close_price is not null"));
        assert!(sql.contains("and open_interest > 0"));
        // 过滤只发生在 usable 一处，四个价格都从它取数。
        assert_eq!(sql.matches("from usable").count(), 1);
        assert_eq!(sql.matches("/ sum(open_interest)").count(), 4);
    }

    #[test]
    fn candle_completeness_filter_runs_before_source_convergence() {
        // 残缺行不能顶掉完整行。
        //
        // 过滤一旦排在 `distinct on` 的下游，语义就变成「先按源优先级挑一行，
        // 再看它合不合格」——同一天同一合约若既有残缺的高优先级行、又有完整的
        // 低优先级行，挑中前者、过滤掉，那天整根蜡烛就没了，而好数据就在旁边。
        //
        // 2026-08-18 实际咬到：通道退役前后留下的 `akshare_v1` 行只有收盘价与
        // 结算价，把新浪的完整行顶掉，JM/LH/JD 三个品种 07-31、08-03、08-05
        // 三天的品种汇总 K 线整根消失。
        for mode in [
            VarietyCandleMode::OpenInterestWeighted,
            VarietyCandleMode::DominantUnadjusted,
        ] {
            let sql = variety_candles_sql(mode);
            let distinct_at = sql
                .find("distinct on (contract, trade_date)")
                .expect("没有按 (合约, 交易日) 收敛来源");
            let filter_at = sql
                .find("open_price is not null")
                .expect("没有四价完整性过滤");
            let order_at = sql
                .find("order by contract, trade_date")
                .expect("没有源优先级定序");
            // 三者同处一个 select：distinct on → where 过滤 → order by 定优先级。
            assert!(
                distinct_at < filter_at && filter_at < order_at,
                "{mode:?} 的完整性过滤没有和 distinct on 同处一个 select 的 where 子句"
            );
        }
    }

    #[test]
    fn dominant_candles_pick_the_largest_open_interest_and_stay_stable() {
        let sql = variety_candles_sql(VarietyCandleMode::DominantUnadjusted);
        assert!(sql.contains("distinct on (trade_date)"));
        // 并列时按合约代码定序：同一天两次查询给出不同的主力是不能接受的。
        assert!(sql.contains("order by trade_date, open_interest desc, contract"));
        // 主连取的是真实合约的真实价，不做任何加权。
        assert!(!sql.contains("sum("), "主连档不该出现聚合");
    }

    #[test]
    fn both_candle_modes_converge_sources_before_synthesising() {
        // 不按可信度收敛的话，日更源与官方历史源两行都会进来，
        // 等于把同一个合约在同一天算了两遍。
        for mode in [
            VarietyCandleMode::OpenInterestWeighted,
            VarietyCandleMode::DominantUnadjusted,
        ] {
            assert!(
                variety_candles_sql(mode).contains("distinct on (contract, trade_date)"),
                "{mode:?} 没有先收敛来源"
            );
        }
    }

    #[test]
    fn synthetic_candles_never_touch_the_cost_query() {
        // 合成价只画图。它一旦流进成本链路，算出来的就是「他在加权指数上的成本」，
        // 而他持的是一个个具体合约——那个数不对应任何真实仓位。
        let cost_sql = variety_building_sql();
        assert!(
            !cost_sql.contains("open_interest"),
            "成本取数不该按持仓量加权"
        );
        assert!(
            cost_sql.contains("p.settlement_price"),
            "成本仍走各合约自己的结算价"
        );
    }

    #[test]
    fn rust_normalisation_matches_the_sql_member_key() {
        // 过滤在 Rust 里归一（seat_member_variants），展示与去重键在 SQL 里归一
        // （MEMBER_KEY）。两边语义必须逐字相同，否则同一家会员在下拉里是一个名字、
        // 在数据里是另一个，页面查不出任何东西还不报错。
        for (raw, expected) in [
            ("国泰君安（代客）", "国泰君安"),
            ("国泰君安", "国泰君安"),
            ("中信期货(代客)", "中信期货"),
            // 结尾没有闭括号：正则不匹配，原样保留。
            ("中信（国际", "中信（国际"),
            // 只有闭括号没有开括号：不匹配。
            ("某某)", "某某)"),
            // 正则 [^）)]* 允许中间再有开括号——匹配从最左那个可行的开括号起。
            ("甲（（乙）", "甲"),
            // 前一组括号已闭合：只剥最后一组。
            ("甲（乙）（丙）", "甲（乙）"),
            ("", ""),
            // 更名别名：剥完括号后套用。乾坤期货 2026-05-26 更名高盛期货，
            // 不并的话选「高盛期货」只看得到更名后的 55 天。
            ("乾坤期货", "高盛期货"),
            ("乾坤期货（代客）", "高盛期货"),
            ("浙江永安", "永安期货"),
            ("上海东证", "东证期货"),
            ("国投安信", "国投期货"),
            ("国投安信期货", "国投期货"),
            ("申银万国", "申万期货"),
            ("申银万国（代客）", "申万期货"),
            // 现用名不受别名影响。
            ("高盛期货（代客）", "高盛期货"),
        ] {
            assert_eq!(normalize_member(raw), expected, "raw = {raw:?}");
        }
    }

    #[test]
    fn every_alias_is_in_the_sql_key_and_none_is_self_referential() {
        // SQL 键与 Rust 归一都从 MEMBER_ALIASES 生成，这里断言生成结果真的
        // 把每一对都带上了——生成函数写错（比如漏了循环体）时测试才有得抓。
        let sql = member_key_sql();
        for (old, new) in MEMBER_ALIASES {
            assert!(
                sql.contains(&format!("when '{old}' then '{new}'")),
                "SQL 键缺别名 {old} → {new}：{sql}"
            );
            assert_ne!(old, new, "自引用别名没有意义");
            // 别名的目标必须是终点：A→B、B→C 这样的链条两边各归一次就会不一致。
            assert!(
                !MEMBER_ALIASES.iter().any(|(o, _)| o == new),
                "别名目标 {new} 自身又是别名源，链式映射两侧行为会分叉"
            );
        }
        assert!(sql.starts_with("case ") && sql.ends_with(" end"));
    }

    #[test]
    fn the_monitor_snapshot_takes_the_latest_row_per_combination() {
        // 不能按「最新日期」一刀切：大商所的行情比郑商所晚一天到，08-11 那天郑商所
        // 已经有数而大商所停在 08-10，一刀切会把焦煤鸡蛋生猪整片漏掉——而页面上
        // 看不出漏了，它只是少了几行。
        assert!(
            MONITOR_SNAPSHOT_SQL.contains("distinct on (contract_1, contract_2)"),
            "必须按组合去重取最新，而不是按日期一刀切：{}",
            *MONITOR_SNAPSHOT_SQL
        );
        assert!(
            MONITOR_SNAPSHOT_SQL.contains("order by contract_1, contract_2, trade_date desc"),
            "distinct on 的排序必须与去重键一致，否则取到的不是最新那条"
        );
        // 到期合约的最后一条会永远留在表里，必须有个新鲜度兜底。
        assert!(
            MONITOR_SNAPSHOT_SQL.contains("days')::interval"),
            "缺少过期兜底：到期合约会一直挂在当前快照里"
        );
        // 过滤走原始列，理由见 MEMBER_KEY 上的 RLS 教训。
        assert!(
            !MONITOR_SNAPSHOT_SQL.contains("regexp_replace"),
            "监控快照的过滤条件里不许出现非 leakproof 函数"
        );
    }

    #[test]
    fn every_column_the_row_mapper_reads_is_actually_selected() {
        // `monitor_row` 按列名取值，列清单少一列就是运行期的 ColumnNotFound——
        // 编译器不管，只有真去查一次库才炸。两处 SQL 都从 MONITOR_COLUMNS 生成，
        // 这里把「取的列」与「选的列」对一遍，漏改在编译期的测试里就拦下。
        for column in [
            "prev_pair_position",
            "prev_years_position",
            "pair_pos_hi20",
            "pair_pos_lo20",
            "turn_crosses_high_20",
            "turn_crosses_low_20",
            "revert_high_hit",
            "revert_high_mae",
            "revert_low_mae_max",
            "revert_high_n",
            "revert_high_move",
            "revert_high_drift",
            "revert_high_days",
            "revert_low_hit",
            "revert_low_n",
            "revert_low_move",
            "revert_low_drift",
            "revert_low_days",
        ] {
            assert!(
                MONITOR_COLUMNS.contains(column),
                "monitor_row 读了 {column}，但列清单里没有它"
            );
            assert!(
                MONITOR_SNAPSHOT_SQL.contains(column),
                "快照 SQL 没有带上 {column}"
            );
        }
    }

    #[test]
    fn the_revert_migration_keeps_the_threshold_out_of_the_table() {
        // 与 202608120001 同一条原则：存素材不存结论。段首日靠「前一日位置」在读时
        // 判，不许出现 is_segment_start 之类的落库列——那等于把阈值焊死。
        let first = include_str!("../../../migrations/202608170001_spread_monitor_revert.sql");
        assert!(first.contains("prev_pair_position"));
        assert!(first.contains("prev_years_position"));
        assert!(
            !first.contains("is_segment_start") && !first.contains("is_new_alert"),
            "段首日必须读时判，不许落库"
        );
        // 加列迁移必须可重跑：2026-08-17 这套 DDL 曾被开发期的验证脚本意外落进生产，
        // 不幂等的话正式部署必然撞「列已存在」。
        assert!(
            first.matches("add column if not exists").count() == 14,
            "14 个新列都必须 add column if not exists"
        );
        assert!(
            first.contains("drop constraint if exists"),
            "约束没有 add if not exists，必须先 drop if exists 才能重跑"
        );

        // 换口径那一版同样要能重跑，而且必须把带档位的旧列删干净——新口径与阈值
        // 无关，留着 _3/_5/_10 只会让人以为还能按档查。
        let second = include_str!("../../../migrations/202608170002_spread_revert_by_window.sql");
        assert!(
            second.matches("drop column if exists").count() == 12,
            "202608170001 加的 12 列都要删掉"
        );
        assert!(
            second.matches("add column if not exists").count() == 10,
            "新口径是 5 个数 × 高低两侧"
        );
        // move 可以为负（低位段历年后续最高仍低于起点时）。202608170002 曾把它约束成
        // 非负，全量重算时被 check 当场拦下（LH2609/LH2701 低位 move=−45），
        // 202608170003 修掉了——别再加回来。
        let third =
            include_str!("../../../migrations/202608170003_revert_move_may_be_negative.sql");
        assert!(
            !third.contains("revert_high_move >= 0") && !third.contains("revert_low_move >= 0"),
            "move 不是非负的"
        );
        assert!(
            third.contains("drop constraint if exists"),
            "替换约束必须先 drop if exists，否则重放迁移会撞「约束已存在」"
        );

        // 0004（拐头素材）同样必须可重跑，且只存事实：判定阈值不落库。
        let fourth = include_str!("../../../migrations/202608170004_pair_pos_rolling_extremes.sql");
        assert!(
            fourth.matches("add column if not exists").count() == 2
                && fourth.contains("drop constraint if exists"),
            "0004 必须幂等"
        );
        assert!(
            !fourth.contains("is_turned") && !fourth.contains("turned boolean"),
            "拐头是读时判定，不许落库"
        );

        for gone in ["revert_low_hit_3", "revert_high_n_10"] {
            assert!(
                !MONITOR_COLUMNS.contains(gone),
                "列清单里还留着已删的 {gone}"
            );
        }

        // 历年轨的 sane 护栏必须容得下真实的极端观测(AP2111-AP2112 在 2021-11-04
        // prev_years_position=-12.26,首轮历年回填被 ±10 拦死);当年轨恒在 [0,1],
        // 它的 ±10 护栏保持原样,放宽的只能是历年侧。
        // 202608180002 曾把历年侧放宽到 ±100,理由是「−8020 是真实观测」——那其实是
        // 收盘价 0 造成的脏数据(DEC-073)。根因修掉、全量重算后实际值域是
        // −2.85~5.01,202608180004 已把护栏收回 ±10。两个迁移都必须可重跑。
        for sql in [
            include_str!("../../../migrations/202608180002_widen_years_position_sane.sql"),
            include_str!("../../../migrations/202608180004_restore_years_position_guard.sql"),
        ] {
            assert!(
                sql.matches("drop constraint if exists").count() == 2,
                "替换两条约束都要先 drop if exists 才能重跑"
            );
        }
        let restored =
            include_str!("../../../migrations/202608180004_restore_years_position_guard.sql");
        assert!(
            restored.contains("years_position >= -10")
                && restored.contains("prev_years_position >= -10")
                && !restored.contains("-100"),
            "护栏必须收回 ±10,不许留 ±100 的残迹"
        );
    }

    #[test]
    fn the_monitor_stores_positions_so_the_threshold_stays_adjustable() {
        // 存位置不存结论：阈值留到读的时候套。存了结论就等于把阈值焊死，日后想把
        // 10% 调成 15% 得重算全部历史，还会出现新旧阈值混在一张表里的局面。
        let migration = include_str!("../../../migrations/202608120001_spread_monitor_daily.sql");
        for column in [
            "pair_position",
            "years_position",
            "pair_low",
            "pair_high",
            "years_low",
            "years_high",
        ] {
            assert!(migration.contains(column), "缺少 {column}");
        }
        assert!(
            !migration.contains("alert") && !migration.contains("triggered"),
            "表里不该有触发结论列——阈值必须留到读的时候套"
        );
    }

    #[test]
    fn member_filters_run_on_the_raw_column_not_through_a_function() {
        // RLS 教训：regexp_replace 不是 leakproof，放进过滤条件后优化器拒绝下推成
        // 索引条件，runtime 角色下 60 秒超时。过滤必须是原始列上的纯比较。
        let building = building_days_sql();
        assert!(
            building.contains("member = any($3::text[])"),
            "建仓过程的会员过滤必须走原始列"
        );
        assert!(
            !building.contains("regexp_replace(member, '[（(][^）)]*[）)]$', '') = $"),
            "归一化表达式不许出现在过滤条件里"
        );
        assert!(
            RAW_MEMBER_WALK_SQL.contains("member > walk.member"),
            "跳跃扫描必须是纯列比较"
        );
        assert!(
            !RAW_MEMBER_WALK_SQL.contains("regexp_replace"),
            "跳跃扫描里不许有函数包装"
        );
    }

    #[test]
    fn one_firm_stays_one_firm_across_sources() {
        // 同一家在不同源下写法不同：大商所 08-05 由 akshare 采写「国泰君安」，
        // 08-06 起改由东财采写「国泰君安（代客）」。不归一，页面上就是两个会员，
        // 同一家的持仓变化被从中间劈开，建仓过程页直接断掉。
        //
        // 括号与自营/代客无关——郑商所两个源都带、89 家全带，上期所两个源都不带、
        // 86 家全不带。一个交易所全带一个全不带，那是数据源的命名习惯。
        assert!(MEMBER_KEY.contains("regexp_replace(member"));
        // 全角半角都要认：源之间连括号字符都不统一。
        assert!(MEMBER_KEY.contains('（') && MEMBER_KEY.contains('('));
        // 只去尾部的括号。公司名里本来就带括号的（如「中信建投（国际）」若存在），
        // 去掉中间的会把两家不同机构并成一家。
        assert!(MEMBER_KEY.contains("$'"), "必须锚在结尾：{MEMBER_KEY}");
    }

    #[test]
    fn the_building_chart_never_counts_a_day_twice() {
        // 这条是四处里最贵的一处：建仓过程按会员逐日求和，同一天同一榜若有两个源
        // 各一行，持仓被直接算成两倍，而图上看不出任何异常——线还是连续的，
        // 形状还像那么回事，只是数值全错。
        let query = building_days_sql();
        let dedupe = query.find("distinct on").expect("求和之前必须先按来源去重");
        let sum = query
            .find("sum(case when rank_type")
            .expect("建仓过程的求和还在");
        assert!(dedupe < sum, "去重必须发生在求和之前，否则等于没去");
        assert!(
            query.contains("regexp_replace(member"),
            "汇总必须按归一后的会员，否则同一家的两种写法各算一份"
        );
    }

    #[test]
    fn the_building_chart_prices_collapse_to_one_row_per_day() {
        // 席位那侧早有 distinct on，行情这侧曾经没有——同一合约同一天日更源与
        // 官方历史源各一行（生产 AU/AG 实测 140 组），同一交易日进成本引擎两次：
        // 页面重复日期、多余盈亏柱，两源结算不同时成本还随行序摆。
        let query = building_days_sql();
        let prices = query.find("prices as (").expect("行情 CTE 还在");
        let dedupe = query[prices..]
            .find("distinct on (trade_date)")
            .expect("行情必须按交易日收敛为一行");
        let rank = query[prices..]
            .find("_official")
            .expect("行情去重必须带来源优先级，交易所自己的数字压过转售源");
        assert!(dedupe < rank, "distinct on 与来源排序都要在行情 CTE 里");
    }

    #[test]
    fn the_volume_board_is_never_read_as_a_position() {
        // 成交量榜与持仓榜是两张榜。某席位某天只上了成交量榜时，若不排掉这行，
        // 它会进 seats 分组、两个 sum 双双得零，被成本引擎读成「平仓」：成本清零、
        // 当日盈亏算出一次假的巨额了结。生产实测 AU/AG 共 166,247 个席位-合约-日
        // 属于这种情形（中信建投 AU2608 2026-08-05）。
        for query in [building_days_sql(), variety_building_sql()] {
            assert!(
                query.contains("rank_type in ('long', 'short')"),
                "取数必须只认多空两张榜：{query}"
            );
        }
    }

    #[test]
    fn the_variety_total_adds_up_the_contract_rows() {
        // 品种汇总要按合约相加，不能拿交易所的品种汇总榜那一行——那行只有一个
        // 总手数，推不出成本，更分不出净多的合约与净空的合约两腿。
        let query = variety_building_sql();
        assert!(
            query.contains("not is_variety_total"),
            "品种汇总必须从逐合约行卷出来：{query}"
        );
        assert!(
            query.contains("group by contract, trade_date"),
            "必须逐合约分组，合并留给成本引擎做：{query}"
        );
        // 结算价要跟着各自的合约走。少了这个连接条件，所有合约会共用同一条行情。
        assert!(
            query.contains("p.contract = s.contract and p.trade_date = s.trade_date"),
            "每个合约要配自己的结算价：{query}"
        );
        // 去重纪律与单合约那条一致，否则同一天算两遍。
        let dedupe = query.find("distinct on").expect("求和之前必须先按来源去重");
        let sum = query.find("sum(case when rank_type").expect("求和还在");
        assert!(dedupe < sum, "去重必须发生在求和之前");
        assert!(
            query.contains("regexp_replace(member"),
            "汇总必须按归一后的会员"
        );
    }

    #[test]
    fn the_freshness_queries_stay_inside_a_bounded_window() {
        // 没有窗口就是全表 group by：runtime 角色下实测 4.5 秒，首页等不起。
        // 这条锁住「限定日期范围」本身，改 SQL 时不至于顺手把它删了。
        for sql in [SEAT_FRESHNESS_SQL, PRICE_FRESHNESS_SQL] {
            assert!(
                sql.contains("trade_date >= current_date -"),
                "新鲜度查询必须限定回看窗：{sql}"
            );
            assert!(sql.contains("limit $3"), "结果条数也要有上限：{sql}");
        }
        // 席位表混着品种汇总行，不排掉就会把「有汇总没逐合约」误报成数据到齐。
        assert!(SEAT_FRESHNESS_SQL.contains("not is_variety_total"));
    }

    #[test]
    fn a_day_the_seat_is_off_the_board_stays_null_not_zero() {
        // 交易所只发前二十名。某席位掉出榜单的那天，官方文件里没有他这一行——
        // 那是「不知道」，不是「零」。原来这里 coalesce 成 0，错误一路传下去：
        // 成本引擎读成平仓清掉成本，累计盈亏凭空多出一次巨额了结
        // （高盛 AU2610：2038 → 0 → 2416）。留 NULL，界面才标得出「掉榜 · 持仓未知」。
        let query = building_days_sql();
        assert!(
            !query.contains("coalesce(s.long_position, 0)")
                && !query.contains("coalesce(s.short_position, 0)"),
            "掉榜日的持仓不许补零：{query}"
        );
        assert!(
            query.contains("s.long_position::text") && query.contains("s.short_position::text"),
            "缺席的日子必须原样返回 NULL"
        );
    }

    #[test]
    fn the_exchanges_own_numbers_win_over_a_reseller() {
        // 同一天同一合约可能同时有交易所年度文件和新浪两行——身份键里带 source，
        // 两行都合法。原来按 source 字典序取，'sina' 排在 'dce_official_history'
        // 之后，于是官方数据被聚合源盖掉，而且看不出来。
        let ranking = OWN_SPREAD_POINTS_SQL
            .split("order by contract, trade_date,")
            .nth(1)
            .expect("去重的排序还在");
        let official = ranking.find("_official").expect("官方源要参与排序");
        let sina = ranking.find("'sina'").expect("新浪要参与排序");
        assert!(
            official < sina,
            "官方源必须排在新浪前面，否则官方数据会被盖掉"
        );
        assert!(
            !ranking.starts_with(" source desc"),
            "不能再按 source 字典序挑行"
        );
    }

    #[test]
    fn one_trading_day_yields_one_spread_point_even_with_two_sources() {
        // price_history 的身份键里带 source，回填与日更对同一天各写一行都是合法的。
        // 两条腿各自 join 一次，不先收敛成一行的话同一个交易日会在图上出现多次，
        // 而且是静悄悄的——点数变多，形状还像那么回事。
        assert!(
            OWN_SPREAD_POINTS_SQL.contains("distinct on (contract, trade_date)"),
            "两条腿必须先各自收敛到一天一行"
        );
        assert!(
            !OWN_SPREAD_POINTS_SQL.contains("join price_history"),
            "不能再直接 join 原表，那样就绕过了收敛"
        );
    }

    #[test]
    fn the_retention_sweep_does_not_reach_across_providers() {
        // 同一组腿在三禾和自研下 query_hash 相同，清理时不按 provider 分开
        // 就会把另一路的历史一并删掉——两边对不上账，还查不出是谁删的。
        assert!(
            !SERIES_RETENTION_SQL.contains("'sanhe'"),
            "留存清理不能写死 sanhe：{SERIES_RETENTION_SQL}"
        );
        assert_eq!(
            SERIES_RETENTION_SQL.matches("provider_code = $4").count(),
            2,
            "外层删除与内层子查询都要按 provider 限定"
        );
    }

    /// Every table this module deletes from, including the ones reached by
    /// `on delete cascade` from a table it deletes directly. A cascade is still
    /// a delete on the referencing table and is refused without the privilege.
    const DELETE_TARGETS: [&str; 7] = [
        "spread_provider_cache",
        "spread_provider_failures",
        "spread_provider_series",
        "spread_provider_observations",
        "spread_window_segments",
        "spread_favorites",
        "spread_template_notes",
    ];
    /// 手工产业备注表的建表迁移(DEC-069)。清空备注走的是 delete,
    /// 所以它也要进 grant 核对。
    const TEMPLATE_NOTES_MIGRATION: &str =
        include_str!("../../../migrations/202608180001_spread_template_notes.sql");

    #[test]
    fn every_table_this_module_deletes_from_is_granted_delete_to_the_runtime_role() {
        // The API connects as `futures_runtime`. Retention shipped without the
        // grants and failed inside the transaction that had just written the
        // series -- a 500 that rolled back two releases. It was invisible in
        // testing because `futures_app` is a superuser, for which every
        // privilege check passes whether the grant exists or not.
        let source = include_str!("spread_analytics.rs");
        let deleted: Vec<&str> = source
            .match_indices("delete from ")
            .map(|(at, marker)| {
                source[at + marker.len()..]
                    .split(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
                    .next()
                    .unwrap_or_default()
            })
            .filter(|table| table.starts_with("spread"))
            .collect();
        for table in &deleted {
            assert!(
                DELETE_TARGETS.contains(table),
                "{table} is deleted from but is not in DELETE_TARGETS, so nothing \
                 checks that the runtime role may delete from it"
            );
        }

        let grants = format!("{MIGRATION}\n{RETENTION_GRANTS}\n{TEMPLATE_NOTES_MIGRATION}");
        for table in DELETE_TARGETS {
            let granted = grants.lines().any(|line| {
                let line = line.trim();
                line.starts_with("grant ")
                    && line.contains("delete")
                    && line.contains(table)
                    && line.contains("futures_runtime")
            });
            assert!(
                granted,
                "no migration grants delete on {table} to futures_runtime"
            );
        }
    }

    #[test]
    fn migration_separates_system_cache_from_workspace_business_tables() {
        for table in [
            "spread_provider_series",
            "spread_provider_observations",
            "spread_window_segments",
            "spread_favorites",
        ] {
            assert!(MIGRATION.contains(&format!("alter table {table} enable row level security;")));
            assert!(MIGRATION.contains(&format!("alter table {table} force row level security;")));
        }
        assert!(!MIGRATION.contains("alter table spread_provider_cache enable row level security"));
        assert!(MIGRATION.contains("provider_code, endpoint_code, parameter_hash, business_date"));
    }

    #[test]
    fn migration_limits_endpoints_and_stable_errors() {
        for endpoint in ["all_varieties", "variety_contracts", "arbitrage_varieties"] {
            assert!(MIGRATION.contains(endpoint));
        }
        for code in [
            "spread_provider_unavailable",
            "spread_provider_rate_limited",
            "spread_provider_forbidden",
            "spread_provider_contract_changed",
        ] {
            assert!(MIGRATION.contains(code));
        }
        assert!(!MIGRATION.contains("broker_positions"));
    }

    #[test]
    fn migration_whitelists_only_the_confirmed_sanhe_connector_contract() {
        assert!(MIGRATION.contains("source_type = 'aggregator'"));
        assert!(MIGRATION.contains("authorization_status = 'user_authorized_readonly'"));
        assert!(MIGRATION.contains("connector_code = 'sanhe_spread_v1'"));
        assert!(MIGRATION.contains("connector_code in ('akshare_v1', 'sanhe_spread_v1')"));
        assert!(MIGRATION.contains("unique nulls not distinct"));
        assert!(MIGRATION.contains("relative_delivery_month_trading_day"));
    }

    #[test]
    fn repository_uses_cross_instance_singleflight_and_one_calendar_version() {
        let source = include_str!("spread_analytics.rs");
        assert!(source.contains("pg_advisory_xact_lock"));
        assert!(source.contains("selected_calendar.id"));
        assert!(source.contains("selected_calendar.effective_from desc"));
    }

    #[sqlx::test(migrations = false)]
    #[ignore = "requires PostgreSQL 17; CI runs this test explicitly"]
    async fn postgres_phase_5a_migration_cache_and_throttle_contract(admin_pool: PgPool) {
        sqlx::query("create database futures_platform")
            .execute(&admin_pool)
            .await
            .unwrap();
        let options = PgConnectOptions::from_str(
            &std::env::var("DATABASE_URL").expect("CI provides DATABASE_URL"),
        )
        .unwrap()
        .database("futures_platform");
        let pool = PgPoolOptions::new()
            .max_connections(5)
            .connect_with(options)
            .await
            .unwrap();
        sqlx::migrate!("../../migrations").run(&pool).await.unwrap();

        let business_date = Date::from_calendar_date(2026, time::Month::August, 5).unwrap();
        let fetched_at = OffsetDateTime::now_utc()
            .replace_nanosecond(123_456_789)
            .unwrap();
        let parameters = json!({});
        let payload = json!({"code": 0, "data": []});
        let parameter_hash = "0".repeat(64);
        let payload_hash = "1".repeat(64);

        let canonical = store_cache(
            &pool,
            &NewProviderCache {
                endpoint: ProviderEndpoint::AllVarieties,
                parameter_hash: &parameter_hash,
                parameters: &parameters,
                business_date,
                fetched_at,
                http_status: 200,
                business_code: 0,
                payload: &payload,
                result_kind: "empty",
                payload_hash: &payload_hash,
            },
        )
        .await
        .unwrap();
        let cached = get_cache(
            &pool,
            ProviderEndpoint::AllVarieties,
            &parameter_hash,
            business_date,
        )
        .await
        .unwrap()
        .expect("legal empty result is persisted");
        assert_eq!(cached.result_kind, "empty");
        assert_eq!(cached.payload, payload);
        assert_ne!(canonical.fetched_at, fetched_at);
        assert_eq!(canonical.fetched_at, cached.fetched_at);
        assert_eq!(canonical.payload, cached.payload);
        assert_eq!(canonical.payload_hash, cached.payload_hash);

        let conflicting_payload = json!({"code": 0, "data": ["must-not-replace-cache"]});
        let conflicting_fetched_at = fetched_at + Duration::seconds(30);
        let conflicting_payload_hash = "2".repeat(64);
        let conflict_result = store_cache(
            &pool,
            &NewProviderCache {
                endpoint: ProviderEndpoint::AllVarieties,
                parameter_hash: &parameter_hash,
                parameters: &parameters,
                business_date,
                fetched_at: conflicting_fetched_at,
                http_status: 200,
                business_code: 0,
                payload: &conflicting_payload,
                result_kind: "ok",
                payload_hash: &conflicting_payload_hash,
            },
        )
        .await
        .unwrap();
        assert_eq!(conflict_result.fetched_at, cached.fetched_at);
        assert_eq!(conflict_result.payload, cached.payload);
        assert_eq!(conflict_result.result_kind, cached.result_kind);
        assert_eq!(conflict_result.payload_hash, cached.payload_hash);

        // Retention: the cache is keyed by business date, so a combination
        // queried daily stores the whole series again every day. Without a
        // bound that is gigabytes a year of identical history.
        for offset in 1..=3 {
            let later = business_date + Duration::days(offset);
            store_cache(
                &pool,
                &NewProviderCache {
                    endpoint: ProviderEndpoint::AllVarieties,
                    parameter_hash: &parameter_hash,
                    parameters: &parameters,
                    business_date: later,
                    fetched_at: fetched_at + Duration::days(offset),
                    http_status: 200,
                    business_code: 0,
                    payload: &json!({"code": 0, "data": [offset]}),
                    result_kind: "ok",
                    payload_hash: &format!("{offset}").repeat(64),
                },
            )
            .await
            .unwrap();
        }
        let kept: Vec<Date> = sqlx::query_scalar(
            "select business_date from spread_provider_cache
              where parameter_hash = $1 and endpoint_code = $2
              order by business_date desc",
        )
        .bind(&parameter_hash)
        .bind(ProviderEndpoint::AllVarieties.code())
        .fetch_all(&pool)
        .await
        .unwrap();
        assert_eq!(
            kept,
            vec![
                business_date + Duration::days(3),
                business_date + Duration::days(2)
            ],
            "only the newest business dates survive a write"
        );

        // The stale-serving path reads whatever is newest, whatever date it is
        // from: it runs when the upstream refuses, so today's row is exactly
        // what is missing.
        let latest = get_latest_cache(&pool, ProviderEndpoint::AllVarieties, &parameter_hash)
            .await
            .unwrap()
            .expect("a stored payload survives for the refusal fallback");
        assert_eq!(latest.payload, json!({"code": 0, "data": [3]}));
        assert!(
            get_latest_cache(&pool, ProviderEndpoint::VarietyContracts, &parameter_hash)
                .await
                .unwrap()
                .is_none(),
            "the fallback must not reach across endpoints"
        );

        let first_pool = pool.clone();
        let second_pool = pool.clone();
        let start = Instant::now();
        let (first, second) = tokio::join!(
            async move {
                let wait = reserve_request_slot(&first_pool).await.unwrap();
                tokio::time::sleep(wait).await;
                start.elapsed()
            },
            async move {
                let wait = reserve_request_slot(&second_pool).await.unwrap();
                tokio::time::sleep(wait).await;
                start.elapsed()
            }
        );
        let separation = first.abs_diff(second);
        assert!(
            separation >= StdDuration::from_millis(1_500),
            "reserved provider starts were separated by only {separation:?}"
        );

        let derivation_column_exists: bool = sqlx::query_scalar(
            "select exists (
                select 1 from information_schema.columns
                 where table_schema = 'public'
                   and table_name = 'spread_provider_series'
                   and column_name = 'derivation_hash'
            )",
        )
        .fetch_one(&pool)
        .await
        .unwrap();
        assert!(derivation_column_exists);
    }
}

/// 我们自己有行情的一个品种。字段名与三禾那条的返回对齐，好让前端一个分支都不用加。
#[derive(Debug, Clone, Serialize)]
pub struct OwnVariety {
    pub market: String,
    pub name: String,
    pub symbol: String,
}

/// 自建价差引擎的一个点。价差以文本承载，由调用方解析成精确小数——
/// 见 `load_own_spread_points` 里那条注释。
#[derive(Debug, Clone)]
pub struct OwnSpreadPoint {
    pub trade_date: Date,
    pub value: String,
    pub front: String,
    pub back: String,
}

/// 从我们自己的行情算价差：同一交易日，先到期的腿的收盘价减后到期的腿的收盘价。
///
/// 用收盘价而不是结算价，是运营者定的口径：价差看的是两个合约的价格关系，收盘价是
/// 市场公认的那个时点；结算价留给席位成本。
///
/// 我们自己有行情的品种，连同交易所和中文名。
///
/// 以 `price_history` 为准而不是 `instruments`：能不能算价差取决于有没有价格，
/// 品种表里登记过但一根 K 线都没有的，列出来只会让人点了报空。
pub async fn own_varieties(
    pool: &PgPool,
    workspace_id: Uuid,
) -> Result<Vec<OwnVariety>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(
        // 名字取自品种范围表而不是 instruments：那张表是采集侧按上游给的名字填的，
        // 眼下就不一致——焦煤是「焦煤」，玻璃是「平板玻璃期货」，黄金白银存的是代码。
        //
        // 范围与「有没有数据」取交集：范围里有但一根 K 线都没有的品种列出来，
        // 点进去是空图，而空图比没有这个选项更糟，它看起来像是数据坏了。
        "select s.instrument, s.exchange, s.display_name as name
           from product_instrument_scope s
          where s.workspace_id = $1
            and exists (
                select 1 from price_history p
                 where p.workspace_id = s.workspace_id and p.instrument = s.instrument
            )
          order by s.instrument",
    )
    .bind(workspace_id)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| OwnVariety {
            market: row.get("exchange"),
            name: row.get("name"),
            symbol: row.get("instrument"),
        })
        .collect())
}

/// 某品种当前挂牌的合约月份，两位数字。
///
/// 只看最近一年：苹果、生猪这类不是月月挂牌的品种，历史上出现过而现在早已不挂的
/// 月份摆进下拉框，选了也算不出东西。窗口以该品种自己的最后一个交易日为基准而不是
/// 今天，否则数据一停更下拉框就空了。
pub async fn own_contract_months(
    pool: &PgPool,
    workspace_id: Uuid,
    instrument: &str,
) -> Result<Vec<String>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query_scalar::<_, String>(
        "select distinct right(contract, 2) as month
           from price_history
          where workspace_id = $1 and instrument = $2
            and trade_date >= (
                select max(trade_date) - interval '400 days'
                  from price_history
                 where workspace_id = $1 and instrument = $2
            )
          order by month",
    )
    .bind(workspace_id)
    .bind(instrument)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows)
}

/// 自建价差的取数 SQL。提成常量是为了让测试断言这一段本身——
/// 让测试去 include_str! 自己的源码找锚点，测试里的字面量会先被匹配到，
/// 结果是什么都没断言到还显示通过。已经踩过一次。
const OWN_SPREAD_POINTS_SQL: &str = "with years as (
             select generate_series(
                 (select min(extract(year from trade_date))::int from price_history
                   where workspace_id = $1 and instrument = $2),
                 (select max(extract(year from trade_date))::int + 1 from price_history
                   where workspace_id = $1 and instrument = $2)
             ) as y
         ), legs as (
             select $2 || lpad((y % 100)::text, 2, '0') || lpad($3::text, 2, '0') as front,
                    $2 || lpad(((case when $4 > $3 then y else y + 1 end) % 100)::text, 2, '0')
                       || lpad($4::text, 2, '0') as back
               from years
         ), one_row_per_day as (
             -- 同一合约同一天可能有不止一行：回填按交易所年度文件写，日更按每日接口
             -- 写，两者 source 不同，而 price_history 的身份键里带 source，所以两行
             -- 都合法地存在。不收敛成一行的话，下面两次 join 会把同一天算成多个点，
             -- 一个交易日在图上出现两次。
             --
             -- 挑哪一行不是任意的：交易所自己发布的压过转手的聚合源。原来按 source
             -- 字典序取，那只是「确定」而不是「对」——'sina' 排在 'dce_official_history'
             -- 之后，官方数据反而会被盖掉。以后大商所官方通了、同一天两个来源都有时，
             -- 官方的那行要自动胜出。
             select distinct on (contract, trade_date)
                    contract, trade_date, close_price
               from price_history
              where workspace_id = $1 and instrument = $2 and close_price is not null
              order by contract, trade_date,
                       case
                           when source like '%_official%' then 0  -- 交易所年度文件
                           when source = 'akshare_v1' then 1      -- 交易所公开接口的封装
                           when source like 'eastmoney%' then 2
                           when source = 'sina' then 3
                           else 4
                       end,
                       source
         )
         select a.trade_date,
                -- ::text 而非直接取 numeric：sqlx 未开 decimal feature，
                -- 而 numeric 转文本是无损的，精度不会在这里丢。
                (a.close_price - b.close_price)::text as value,
                l.front, l.back
           from legs l
           join one_row_per_day a on a.contract = l.front
           join one_row_per_day b on b.contract = l.back and b.trade_date = a.trade_date
          order by a.trade_date";

/// 腿的年份由月份定：后腿月份大于前腿的，是同一年（01-05）；否则是次年（09-01 的
/// 01 腿是次年的，否则先到期的就成了 01 腿，那是反向组合）。
///
/// 这里只产出原始价差点，散户窗口的裁剪、分段、季节与月度全部交给
/// `calculate_windowed_analytics`——那套逻辑已经有测试，不该有第二份。
pub async fn load_own_spread_points(
    pool: &PgPool,
    workspace_id: Uuid,
    instrument: &str,
    front_month: u8,
    back_month: u8,
) -> Result<Vec<OwnSpreadPoint>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(OWN_SPREAD_POINTS_SQL)
        .bind(workspace_id)
        .bind(instrument)
        .bind(i32::from(front_month))
        .bind(i32::from(back_month))
        .fetch_all(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| OwnSpreadPoint {
            trade_date: row.get("trade_date"),
            value: row.get::<String, _>("value"),
            front: row.get("front"),
            back: row.get("back"),
        })
        .collect())
}

/// 席位每日持仓的一行。字段与 `seat_history` 一一对应，不做加工——
/// 加工留给读它的人，这里只负责如实取出来。
#[derive(Debug, Clone, Serialize)]
pub struct SeatPositionRow {
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

/// 会员名归一：去掉尾部括号里的限定词。
///
/// 同一家机构在不同数据源下写法不同——大商所 08-05 由 akshare 采，写「国泰君安」；
/// 08-06 起 akshare 那条路断了改由东财采，写「国泰君安（代客）」。同一家、同样的
/// 行数、只是不同日子由不同源采的。不归一的话，页面上会把它当成两个会员，
/// 同一家机构的持仓变化被从中间劈开，建仓过程页直接断掉。
///
/// **这个括号与自营/代客无关。** 郑商所两个源都写「（代客）」、89 家全带，
/// 上期所两个源都不带、86 家全不带——一个交易所全带一个全不带，那不是业务区分，
/// 是各家数据源的命名习惯。曾经按「不带括号=自营」理解过，是错的。
///
/// `member` 原文一个字不改，归一只发生在查询时：交易所与数据源发布的名字是事实，
/// 我们对它的理解是解释，两者不该混在一列里。
/// **只许用于小结果集上的展示与去重键，绝不许出现在 WHERE 过滤里。**
///
/// 教训（2026-08-11 生产事故）：把它放进过滤条件后，futures_app 下 121 毫秒的查询在
/// futures_runtime + RLS 下 60 秒超时——`regexp_replace` 不是 leakproof 函数，RLS 之下
/// 优化器拒绝把它下推成索引条件，每次求值都退化成全表扫。席位页会员下拉因此整个空掉。
/// 性能测试当时用 futures_app 跑的，绕过了 RLS，测的不是生产真实路径。
///
/// 过滤一律走原始 `member` 列（纯比较运算，RLS 友好），归一化在 Rust 里做
/// （`normalize_member`，与本正则同语义，有测试盯两边一致）。
const MEMBER_KEY: &str = "regexp_replace(member, '[（(][^）)]*[）)]$', '')";

/// 会员更名别名：老名字 → 现用名。剥完括号之后再套用。
///
/// 这些不是猜的：每一对都在库里查过起止日期、且经运营者确认是同一家——
/// 乾坤期货 2026-05-26 更名高盛期货（选「高盛期货」曾只看得到 55 天，
/// 前面十七年都在旧名下）；浙江永安→永安期货、上海东证→东证期货、
/// 国投安信/国投安信期货→国投期货同理。
/// 「申银万国」与「申万期货」并存十四年是源命名差异，运营者 2026-08-12 确认同一家。
///
/// **SQL 侧的 `member_key_sql()` 与 Rust 侧的 `normalize_member` 都从这张表生成**，
/// 别名只在这里改——高盛更名修复的前一课（dadbeda）就是同一个事实两处维护、
/// 只改了一处。
const MEMBER_ALIASES: &[(&str, &str)] = &[
    ("乾坤期货", "高盛期货"),
    ("浙江永安", "永安期货"),
    ("上海东证", "东证期货"),
    ("国投安信", "国投期货"),
    ("国投安信期货", "国投期货"),
    ("申银万国", "申万期货"),
];

/// 展示与去重键的完整 SQL 表达式：剥括号 + 套别名。
/// 只许出现在 SELECT / distinct on / order by 里，绝不许进 WHERE——
/// 理由见 `MEMBER_KEY` 上的 RLS 教训。
fn member_key_sql() -> String {
    let mut sql = format!("case {MEMBER_KEY}");
    for (old, new) in MEMBER_ALIASES {
        sql.push_str(&format!(" when '{old}' then '{new}'"));
    }
    sql.push_str(&format!(" else {MEMBER_KEY} end"));
    sql
}

/// 原始会员名的跳跃扫描：沿 (workspace_id, member, …) 索引一个名字跳一次。
/// 纯列比较，RLS 下照常走索引——这正是它存在的理由，见 `MEMBER_KEY` 上的教训。
const RAW_MEMBER_WALK_SQL: &str = "with recursive walk as (
     select min(member) as member from seat_history
      where workspace_id = $1 and ($2::text is null or instrument = $2)
     union all
     select (select min(member) from seat_history
              where workspace_id = $1 and ($2::text is null or instrument = $2)
                and member > walk.member)
       from walk where walk.member is not null
 )
 select member from walk where member is not null order by member limit 2000";

/// 与 `MEMBER_KEY` 的正则**同语义**：剥掉结尾的一组括号限定词。
/// 正则是 `[（(][^）)]*[）)]$`——匹配起点是「其后直到结尾都没有闭括号」的最左一个
/// 开括号，也就是正文里最后一个闭括号之后的第一个开括号。改任何一边必须同步另一边，
/// `rust_normalisation_matches_the_sql_member_key` 盯着。
pub(crate) fn normalize_member(raw: &str) -> String {
    let stripped = strip_member_qualifier(raw);
    for (old, new) in MEMBER_ALIASES {
        if stripped == *old {
            return (*new).to_string();
        }
    }
    stripped
}

fn strip_member_qualifier(raw: &str) -> String {
    if !(raw.ends_with('）') || raw.ends_with(')')) {
        return raw.to_string();
    }
    let close_len = raw.chars().next_back().map_or(0, char::len_utf8);
    let body = &raw[..raw.len() - close_len];
    let after_last_close = body
        .char_indices()
        .rev()
        .find(|(_, c)| matches!(c, '）' | ')'))
        .map_or(0, |(i, c)| i + c.len_utf8());
    match body[after_last_close..]
        .char_indices()
        .find(|(_, c)| matches!(c, '（' | '('))
    {
        Some((offset, _)) => body[..after_last_close + offset].to_string(),
        None => raw.to_string(),
    }
}

/// 一个归一化名字对应的全部原始写法（「国泰君安」→「国泰君安」「国泰君安（代客）」）。
/// 过滤要拿原始写法做 `member = any(...)`，理由见 `MEMBER_KEY`。
/// 名字不认识时原样返回：等值过滤自然得到空集，不须特判。
async fn seat_member_variants(
    tx: &mut sqlx::PgConnection,
    workspace_id: Uuid,
    normalized: &str,
) -> Result<Vec<String>, sqlx::Error> {
    let raw = sqlx::query_scalar::<_, String>(RAW_MEMBER_WALK_SQL)
        .bind(workspace_id)
        .bind(None::<&str>)
        .fetch_all(&mut *tx)
        .await?;
    let mut variants: Vec<String> = raw
        .into_iter()
        .filter(|name| normalize_member(name) == normalized)
        .collect();
    if variants.is_empty() {
        variants.push(normalized.to_string());
    }
    Ok(variants)
}

/// 席位来源的可信度。同一天同一榜同一会员可能有不止一个源——郑商所 08-07 就同时有
/// czce_official 与 akshare_v1，两边数字未必一致。不排序去重的话，席位页会把同一天
/// 的同一家显示两次。
///
/// 交易所自己发布的压过封装，封装压过转手的聚合源。三禾排最后但不会被无谓丢弃：
/// 它覆盖的是全部会员而非前二十，只在同一会员同时出现在官方榜上时才让位。
const SEAT_SOURCE_RANK: &str = "case
                when source like '%_official%' then 0
                when source = 'akshare_v1' then 1
                when source like 'eastmoney%' then 2
                when source = 'sanhe' then 3
                else 4
            end";

/// 行情来源的可信度，同 [`SEAT_SOURCE_RANK`] 的道理：同一合约同一天日更源与官方
/// 历史源各有一行（生产 AU/AG 实测 140 组），不按可信度收敛成一行，成本就取决于
/// 行序这种没人保证的东西。
const PRICE_SOURCE_RANK: &str = "case
                when source like '%_official%' then 0
                when source = 'akshare_v1' then 1
                when source like 'eastmoney%' then 2
                when source like 'sina%' then 3
                else 4
            end";

/// 某品种某交易日的全部席位行。
///
/// 数量用文本承载而不是浮点：这些数字后面要拿去算持仓成本，一路保持精确比在
/// 边界上来回转换安全。
/// 该会员(按归一名,含全部历史写法)历史上持有过的全部品种。
///
/// 给建仓过程的品种下拉用:那是历史序列,品种列表不该跟着「所选日期当天是否在榜」
/// 变——高盛 2026-08-17 掉出金榜,黄金就从下拉里消失了,而他 691 天的建仓过程明明
/// 都在(运营者当日报的 bug)。反推行不计入:一个品种若只有反推行,说明真行从未
/// 出现过,那是不可能的,防御性排除而已。
/// 一行现货基差:某品种在某交易日的现货价、主力基差,以及**该基差在历年同品种
/// 里的百分位**(DEC-074)。
///
/// 分位是这张表的核心用法:基差的绝对点数在品种之间没有可比性(生猪几千点、
/// 玻璃几十点),「现在处在历史什么位置」才有。取当日最近一条(源偶尔缺日,
/// 用 `<= trade_date` 往前找,最多回看 7 天——再远就不代表当下了)。
pub struct SpotBasisRow {
    pub instrument: String,
    pub trade_date: Date,
    pub spot_price: String,
    pub dominant_basis: Option<String>,
    pub dominant_basis_rate: Option<String>,
    /// 主力基差率在该品种全部历史里的百分位(0~1);样本不足 60 天时为 None。
    pub basis_percentile: Option<String>,
}

/// 监控页要的那一批:每个品种取截至该日的最近一条。
pub async fn load_spot_basis(
    pool: &PgPool,
    workspace_id: Uuid,
    trade_date: Date,
) -> Result<Vec<SpotBasisRow>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(
        "with latest as (
             select distinct on (instrument) instrument, trade_date, spot_price,
                    dominant_basis, dominant_basis_rate
               from spot_basis_history
              where workspace_id = $1 and trade_date <= $2
                and trade_date >= $2 - interval '7 days'
              order by instrument, trade_date desc
         ),
         ranked as (
             select h.instrument,
                    percent_rank() over (partition by h.instrument
                                         order by h.dominant_basis_rate) pr,
                    h.dominant_basis_rate rate,
                    count(*) over (partition by h.instrument) n
               from spot_basis_history h
              where h.workspace_id = $1 and h.dominant_basis_rate is not null
                and h.trade_date <= $2
         )
         select l.instrument, l.trade_date, l.spot_price::text spot_price,
                l.dominant_basis::text dominant_basis,
                l.dominant_basis_rate::text dominant_basis_rate,
                (select case when max(r.n) >= 60 then
                          round(max(r.pr) filter (where r.rate = l.dominant_basis_rate)::numeric, 4)
                        end
                   from ranked r where r.instrument = l.instrument)::text basis_percentile
           from latest l
          order by l.instrument",
    )
    .bind(workspace_id)
    .bind(trade_date)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| SpotBasisRow {
            instrument: row.get("instrument"),
            trade_date: row.get("trade_date"),
            spot_price: row.get("spot_price"),
            dominant_basis: row.get("dominant_basis"),
            dominant_basis_rate: row.get("dominant_basis_rate"),
            basis_percentile: row.get("basis_percentile"),
        })
        .collect())
}

/// 报告表所依赖数据的「版本」:该交易日席位与行情最近一次被装载触碰的时刻。
///
/// 用它做缓存键,报告表就只在**数据真的变过**之后才重算——按时间过期的做法在
/// 空闲时段会反复重算一模一样的东西,到点又必然让人撞上那十几秒(运营者
/// 2026-08-18 指出这一点)。
///
/// 取 `updated_at` 不是 `loaded_at`:后者自 2026-08-16 起固定为「首次到达时刻」、
/// upsert 不再刷新(见迁移 202608160001),晚间补采改了数也不会动它——拿它当
/// 版本号会漏掉补采。`updated_at` 则是 insert/upsert 都刷新的「最近触碰时刻」,
/// 正是这里要的语义。
///
/// 只看**该交易日**的行:采集写的就是当天(补采回看几天也会落在自己的 trade_date
/// 上,那些日子的报告表另有各自的缓存键)。全表 max 要扫六百万行,不值当。
/// 存量行的 `updated_at` 为 null(加列前入库),`max` 会忽略它们——那些日子的
/// 数据早已不再变化,版本恒为 null 也就恒定命中,正合适。
pub async fn report_data_version(
    pool: &PgPool,
    workspace_id: Uuid,
    trade_date: Date,
) -> Result<Option<OffsetDateTime>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let version: Option<OffsetDateTime> = sqlx::query_scalar(
        "select greatest(
                  (select max(updated_at) from seat_history
                    where workspace_id = $1 and trade_date = $2),
                  (select max(updated_at) from price_history
                    where workspace_id = $1 and trade_date = $2))",
    )
    .bind(workspace_id)
    .bind(trade_date)
    .fetch_one(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(version)
}

/// 全部模板备注,一次取回(单人面板,总量个位数到几十条)。
pub async fn load_template_notes(
    pool: &PgPool,
    workspace_id: Uuid,
) -> Result<Vec<(String, i32, String, i32, String)>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(
        "select instrument_1, month_1, instrument_2, month_2, note
           from spread_template_notes where workspace_id = $1",
    )
    .bind(workspace_id)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| {
            (
                row.get("instrument_1"),
                row.get("month_1"),
                row.get("instrument_2"),
                row.get("month_2"),
                row.get("note"),
            )
        })
        .collect())
}

/// 写一条模板备注;note 为空 = 删除(清空即撤,不留空行)。
pub async fn save_template_note(
    pool: &PgPool,
    workspace_id: Uuid,
    instrument_1: &str,
    month_1: i32,
    instrument_2: &str,
    month_2: i32,
    note: &str,
) -> Result<(), sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    if note.is_empty() {
        sqlx::query(
            "delete from spread_template_notes
              where workspace_id = $1 and instrument_1 = $2 and month_1 = $3
                and instrument_2 = $4 and month_2 = $5",
        )
        .bind(workspace_id)
        .bind(instrument_1)
        .bind(month_1)
        .bind(instrument_2)
        .bind(month_2)
        .execute(&mut *tx)
        .await?;
    } else {
        sqlx::query(
            "insert into spread_template_notes
                 (workspace_id, instrument_1, month_1, instrument_2, month_2, note)
             values ($1, $2, $3, $4, $5, $6)
             on conflict (workspace_id, instrument_1, month_1, instrument_2, month_2)
             do update set note = excluded.note, updated_at = now()",
        )
        .bind(workspace_id)
        .bind(instrument_1)
        .bind(month_1)
        .bind(instrument_2)
        .bind(month_2)
        .bind(note)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(())
}

pub async fn load_member_instruments(
    pool: &PgPool,
    workspace_id: Uuid,
    member: &str,
) -> Result<Vec<String>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let variants = seat_member_variants(&mut tx, workspace_id, member).await?;
    let rows = sqlx::query_scalar::<_, String>(
        "select distinct instrument from seat_history
          where workspace_id = $1 and member = any($2::text[])
            and not is_variety_total and rank_type in ('long', 'short')
            and source <> 'reboard_inferred'
          order by instrument",
    )
    .bind(workspace_id)
    .bind(variants)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows)
}

pub async fn load_seat_positions(
    pool: &PgPool,
    workspace_id: Uuid,
    member: Option<&str>,
    instrument: Option<&str>,
    trade_date: Date,
) -> Result<Vec<SeatPositionRow>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let member_variants = match member {
        Some(name) => Some(seat_member_variants(&mut tx, workspace_id, name).await?),
        None => None,
    };
    let rows = sqlx::query(&format!(
        "select exchange, instrument, contract, is_variety_total, variety_total_is_computed,
                rank_type, rank, member, quantity, change, source
           from (
             -- 同一天同一榜同一会员只留一个源，见 SEAT_SOURCE_RANK。
             select distinct on (exchange, instrument, contract, is_variety_total,
                                 rank_type, {member_key})
                    exchange, instrument, contract, is_variety_total,
                    variety_total_is_computed, rank_type, rank,
                    -- 归一后的名字才是给人看的那个；原文留在 source 那一路可查。
                    {member_key} as member,
                    quantity::text as quantity, change::text as change, source
               from seat_history
              where workspace_id = $1 and trade_date = $2
                -- 过滤走原始列（等值、索引可用）；归一只出现在展示与去重键上。
                and ($3::text[] is null or member = any($3))
                and ($4::text is null or instrument = $4)
              order by exchange, instrument, contract, is_variety_total,
                       rank_type, {member_key}, {source_rank}, source
           ) picked
          order by instrument, is_variety_total desc, contract nulls first,
                   rank_type, rank nulls last, member",
        member_key = member_key_sql(),
        source_rank = SEAT_SOURCE_RANK,
    ))
    .bind(workspace_id)
    .bind(trade_date)
    .bind(member_variants)
    .bind(instrument)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| SeatPositionRow {
            exchange: row.get("exchange"),
            instrument: row.get("instrument"),
            contract: row.get("contract"),
            is_variety_total: row.get("is_variety_total"),
            variety_total_is_computed: row.get("variety_total_is_computed"),
            rank_type: row.get("rank_type"),
            rank: row.get("rank"),
            member: row.get("member"),
            quantity: row.get("quantity"),
            change: row.get("change"),
            source: row.get("source"),
        })
        .collect())
}

/// 某品种有席位数据的交易日，最新在前。界面的日期选择器要靠它——
/// 让人去选一个根本没有数据的日子，然后看到一张空表，是最没必要的一种困惑。
pub async fn seat_trade_dates(
    pool: &PgPool,
    workspace_id: Uuid,
    member: Option<&str>,
    limit: i64,
) -> Result<Vec<Date>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let member_variants = match member {
        Some(name) => Some(seat_member_variants(&mut tx, workspace_id, name).await?),
        None => None,
    };
    let rows = sqlx::query_scalar::<_, Date>(
        // 过滤走原始列，理由见 MEMBER_KEY 上的教训。
        "select distinct trade_date from seat_history
              where workspace_id = $1 and ($2::text[] is null or member = any($2))
              order by trade_date desc limit $3",
    )
    .bind(workspace_id)
    .bind(member_variants)
    .bind(limit)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows)
}

/// 建仓过程的取数 SQL。提成函数是为了让测试断言这一段本身——
/// 让测试 include_str! 自己的源码找锚点，测试里的字面量会先被匹配到，
/// 什么都没断言到还显示通过。同一个坑已经踩到第四次。
fn building_days_sql() -> String {
    format!(
        "with picked as (
             -- 先按来源去重再求和。这里最要紧：这条是按会员逐日汇总的，同一天同一榜
             -- 若有两个源各一行，持仓会被直接算成两倍，而图上看不出任何异常。
             select distinct on (trade_date, contract, is_variety_total, rank_type, {member_key})
                    trade_date, rank_type, quantity, source
               from seat_history
              where workspace_id = $1 and instrument = $2 and member = any($3::text[])
                and ($4::text is null or contract = $4)
                -- 选了具体合约就只看逐合约行；没选就是品种汇总，
                -- 那要把该席位在各合约上的持仓加起来，而不是混进汇总行。
                and is_variety_total = ($4::text is null)
                -- **成交量榜不是持仓榜。** 不排掉它，某席位某天只上了成交量榜
                -- 的那些日子会进 seats 分组，两个 sum 双双得零，于是被读成
                -- 「持仓 0 = 平仓」：成本清零、当日盈亏算出一次假的巨额了结。
                -- 生产实测 AU/AG 共 166,247 个席位-合约-日属于这种情形
                -- （实例：中信建投 AU2608 2026-08-05）。真相是那天他不在多空
                -- 前二十，持仓未知——排掉之后这天没有 seats 行，如实报 NULL。
                and rank_type in ('long', 'short')
              order by trade_date, contract, is_variety_total, rank_type, {member_key},
                       {source_rank}, source
         ), seats as (
             select trade_date,
                    sum(case when rank_type = 'long' then quantity else 0 end) as long_position,
                    sum(case when rank_type = 'short' then quantity else 0 end) as short_position,
                    -- 该日任一腿来自回榜反推(reboard_inferred)即标推算——那天他
                    -- **实际未上榜**,数字是从回榜日的增减倒推出来的(运营者
                    -- 2026-08-17:所有席位的推算持仓都要打上标记)。
                    bool_or(source = 'reboard_inferred') as inferred
               from picked
              group by trade_date
         ), prices as (
             -- 行情也要按来源去重，跟席位同一条纪律。同一合约同一天日更源与官方
             -- 历史源各有一行（生产 AU/AG 实测 140 组），不去重则同一交易日进
             -- 成本引擎两次：页面出重复日期与多余盈亏柱，两源结算不同时成本
             -- 还取决于行序这种没人保证的东西。
             select distinct on (trade_date)
                    trade_date, open_price, high_price, low_price, close_price, settlement_price
               from price_history
              where workspace_id = $1 and $4::text is not null and contract = $4
              order by trade_date, {price_rank}, source
         )
         select coalesce(s.trade_date, p.trade_date) as trade_date,
                p.open_price::text, p.high_price::text, p.low_price::text,
                p.close_price::text, p.settlement_price::text,
                -- **不要 coalesce 成 0。** 那天没有他的行，意思是他掉出了前二十，
                -- 不是他清仓了。补一个 0 下去，成本引擎会读成平仓、清掉成本，
                -- 累计盈亏跟着算出一次不存在的巨额了结（净持仓 2038 → 0 → 2416）。
                -- 留 NULL，一路传到界面上如实标成「掉榜 · 持仓未知」。
                s.long_position::text as long_position,
                s.short_position::text as short_position,
                coalesce(s.inferred, false) as inferred
           from seats s
           full outer join prices p on p.trade_date = s.trade_date
          where coalesce(s.trade_date, p.trade_date) is not null
          order by 1",
        member_key = member_key_sql(),
        source_rank = SEAT_SOURCE_RANK,
        price_rank = PRICE_SOURCE_RANK,
    )
}

/// 品种汇总的一天一合约：某席位在某个合约上的持仓，配上**那个合约自己的**结算价。
#[derive(Debug, Clone)]
pub struct VarietyBuildingRow {
    pub contract: String,
    pub trade_date: Date,
    pub long_position: Option<String>,
    pub short_position: Option<String>,
    /// 该合约该日的持仓来自回榜反推:实际未上该合约的前二十。
    pub inferred: bool,
    pub settlement_price: Option<String>,
}

/// 净持仓页的一行：某个席位在某个合约上某天的多空持仓。
///
/// 与建仓过程那条路的区别只在于多了 `member_key` 一维——净持仓页一次看好几家。
/// 合计、分腿、掉榜判定都在 Rust 侧做，这里只负责如实取数。
#[derive(Debug, Clone)]
pub struct SeatNetPositionRow {
    /// 归一后的会员名，与调用方传进来的写法一致。
    pub member_key: String,
    pub contract: String,
    pub trade_date: Date,
    pub long_position: String,
    pub short_position: String,
    /// 该日该合约的持仓来自回榜反推:实际未上榜,数字由回榜日增减倒推。
    pub inferred: bool,
    /// 该合约当日结算价。盈亏用它算——收盘价在无成交日是 0(DEC-073),
    /// 结算价才是当日代表价。没有则那天的盈亏不可知。
    pub settlement: Option<String>,
}

/// 净持仓页的取数 SQL。提成函数与 [`building_days_sql`] 同一个理由（测试要断言它本身）。
fn seat_net_positions_sql() -> String {
    format!(
        "with picked as (
             -- 去重纪律同 building_days_sql：先按来源收敛，再求和。不收敛的话
             -- 日更源与官方历史源两行都会进来，同一家的持仓会被加两遍。
             select distinct on (trade_date, contract, rank_type, {member_key})
                    trade_date, contract, rank_type, quantity, source,
                    {member_key} as member_key
               from seat_history
              where workspace_id = $1 and instrument = $2 and member = any($3::text[])
                and not is_variety_total and contract is not null
                -- 成交量榜不是持仓榜，理由见 building_days_sql 里的同一条。
                and rank_type in ('long', 'short')
                and ($4::text is null or contract = $4)
              order by trade_date, contract, rank_type, {member_key}, {source_rank}, source
         ),
         px as (
             -- 每个合约每天一个结算价:盈亏要用它算(收盘价在无成交日是 0,
             -- 结算价才是当日代表价,DEC-073)。多源同样先按可信度收敛成一行。
             select distinct on (contract, trade_date)
                    contract, trade_date, settlement_price
               from price_history
              where workspace_id = $1 and instrument = $2
                and settlement_price is not null and settlement_price > 0
                and ($4::text is null or contract = $4)
              order by contract, trade_date, {price_rank}, source
         )
         select p.member_key, p.contract, p.trade_date,
                sum(case when p.rank_type = 'long' then p.quantity else 0 end)::text
                    as long_position,
                sum(case when p.rank_type = 'short' then p.quantity else 0 end)::text
                    as short_position,
                -- 任一腿来自回榜反推即标推算:那天他实际未上榜,数字是倒推的
                -- (运营者 2026-08-17:所有席位的推算持仓都要打标)。
                bool_or(p.source = 'reboard_inferred') as inferred,
                max(px.settlement_price)::text as settlement
           from picked p
           left join px on px.contract = p.contract and px.trade_date = p.trade_date
          group by p.member_key, p.contract, p.trade_date
          order by p.trade_date, p.member_key, p.contract",
        member_key = member_key_sql(),
        source_rank = SEAT_SOURCE_RANK,
        price_rank = PRICE_SOURCE_RANK,
    )
}

/// 取所选席位在某品种（或某合约）上的逐日多空持仓。
///
/// `members` 传归一后的展示名；这里逐个展开成该名字的全部历史写法再过滤——
/// 用变体过滤走得到索引，用归一表达式过滤走不到。两边归一是否一致由
/// `rust_normalisation_matches_the_sql_member_key` 钉住：不一致的话，某一家会
/// 一行都匹配不上，在界面上表现为「他天天掉榜」，而不会报任何错。
pub async fn load_seat_net_positions(
    pool: &PgPool,
    workspace_id: Uuid,
    instrument: &str,
    members: &[String],
    contract: Option<&str>,
) -> Result<Vec<SeatNetPositionRow>, sqlx::Error> {
    if members.is_empty() {
        return Ok(Vec::new());
    }
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let mut variants: Vec<String> = Vec::new();
    for member in members {
        variants.extend(seat_member_variants(&mut tx, workspace_id, member).await?);
    }
    let rows = sqlx::query(&seat_net_positions_sql())
        .bind(workspace_id)
        .bind(instrument)
        .bind(variants)
        .bind(contract)
        .fetch_all(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| SeatNetPositionRow {
            member_key: row.get("member_key"),
            contract: row.get("contract"),
            trade_date: row.get("trade_date"),
            long_position: row.get("long_position"),
            short_position: row.get("short_position"),
            inferred: row.get("inferred"),
            settlement: row.get("settlement"),
        })
        .collect())
}

/// 一条席位组合收藏。
#[derive(Debug, Clone)]
pub struct SeatMemberFavorite {
    pub id: Uuid,
    pub name: String,
    pub members: Vec<String>,
}

pub async fn list_seat_member_favorites(
    pool: &PgPool,
    workspace_id: Uuid,
) -> Result<Vec<SeatMemberFavorite>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(
        "select id, name, members from seat_member_favorites
          where workspace_id = $1 order by created_at desc",
    )
    .bind(workspace_id)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| SeatMemberFavorite {
            id: row.get("id"),
            name: row.get("name"),
            members: row.get("members"),
        })
        .collect())
}

/// 建一条收藏。同名会被表上的唯一约束顶回来，由调用方翻译成 409。
pub async fn create_seat_member_favorite(
    pool: &PgPool,
    workspace_id: Uuid,
    id: Uuid,
    name: &str,
    members: &[String],
) -> Result<(), sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    sqlx::query(
        "insert into seat_member_favorites (id, workspace_id, name, members)
         values ($1, $2, $3, $4)",
    )
    .bind(id)
    .bind(workspace_id)
    .bind(name)
    .bind(members)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(())
}

/// 删一条收藏。返回是否真的删掉了——删一个不存在的东西该是 404，不是 200。
pub async fn delete_seat_member_favorite(
    pool: &PgPool,
    workspace_id: Uuid,
    id: Uuid,
) -> Result<bool, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let done = sqlx::query("delete from seat_member_favorites where workspace_id = $1 and id = $2")
        .bind(workspace_id)
        .bind(id)
        .execute(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(done.rows_affected() > 0)
}

/// 品种汇总蜡烛图的一天。
///
/// 与 [`VarietyBuildingRow`] 是**两路互不相干的数据**。那一路是这个席位在各合约上的
/// 持仓配上那个合约自己的结算价，喂给成本引擎；这一路是整个品种的合成行情，只喂
/// K 线。合成价**不参与任何成本与盈亏计算**——他持的是一个个具体合约，不是加权指数，
/// 拿指数价去算他的成本会算出一个不存在的数。
#[derive(Debug, Clone)]
pub struct VarietyCandleRow {
    pub trade_date: Date,
    pub open_price: String,
    pub high_price: String,
    pub low_price: String,
    pub close_price: String,
    /// 主连档：当天被选中的那个合约。换月时它会变。
    /// 加权档恒为 `None`——加权没有「是哪个合约」这回事。
    pub source_contract: Option<String>,
}

/// 合成行情的口径。运营者拍板：金银用加权，其余用主连。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum VarietyCandleMode {
    /// 持仓量加权：Σ(价×持仓量) / Σ持仓量。
    OpenInterestWeighted,
    /// 主力连续，**不复权**。
    DominantUnadjusted,
}

impl VarietyCandleMode {
    /// 金银用加权，其余用主连。
    pub fn for_instrument(instrument: &str) -> Self {
        match instrument {
            "AU" | "AG" => Self::OpenInterestWeighted,
            _ => Self::DominantUnadjusted,
        }
    }

    /// 透出给界面的口径名。与 [`Self::for_instrument`] 挨着放：判定改了名字跟着改，
    /// 免得界面上标着「加权」画的却是主连。
    pub fn as_str(self) -> &'static str {
        match self {
            Self::OpenInterestWeighted => "open_interest_weighted",
            Self::DominantUnadjusted => "dominant_unadjusted",
        }
    }
}

/// 合成行情的取数 SQL。提成函数与 [`building_days_sql`] 同一个理由（测试要断言它本身）。
///
/// 两档共用同一个 `picked`：先按来源可信度把同一合约同一天收敛成一行，再合成。不收敛
/// 的话日更源与官方历史源两行都会进加权，等于把同一个合约算了两遍。
fn variety_candles_sql(mode: VarietyCandleMode) -> String {
    // 四价齐全才纳入。加权的分子分母必须来自同一批合约——某个合约缺开盘价却把它的
    // 持仓量计进分母，算出来的开盘价会被系统性拉低。
    //
    // **完整性过滤必须在 distinct on 之前**（2026-08-18 修）。原先是先按源优先级
    // 挑出一行、再检查它合不合格，于是一行残缺的高优先级数据会把同一天同一合约
    // 的完整行**顶掉**，那天整根蜡烛就没了——而好数据明明就躺在旁边。
    //
    // 实际咬到的是大商所：通道退役前后留下一批 `akshare_v1` 行（2026-07-31~08-12），
    // 只有收盘价与结算价，开高低和持仓量全空。郑商所与上期所有官方源（优先级 0）
    // 压着不受影响，大商所只有新浪（优先级 3），被 akshare（1）压住，于是
    // JM/LH/JD 三个品种在 07-31、08-03、08-05 三天的品种汇总 K 线整根消失。
    // 单合约 K 线（`load_contract_candles`）本来就是先过滤再挑，没有这个毛病，
    // 这里与它对齐。
    let picked = format!(
        "with usable as (
             select distinct on (contract, trade_date)
                    contract, trade_date,
                    open_price, high_price, low_price, close_price, open_interest
               from price_history
              where workspace_id = $1 and instrument = $2
                and open_interest > 0
                and open_price is not null and high_price is not null
                and low_price is not null and close_price is not null
              order by contract, trade_date, {price_rank}, source
         )",
        price_rank = PRICE_SOURCE_RANK,
    );
    match mode {
        // 注意这里的 high/low 是「各合约当日最高价的加权平均」，不是「加权价格序列
        // 当日的最高点」。后者要日内逐笔才算得出，我们只有日频。差别在振幅上：
        // 各合约不会同时摸到各自的高点，所以合成出来的振幅略窄于真实加权指数。
        VarietyCandleMode::OpenInterestWeighted => format!(
            "{picked}
             select trade_date,
                    (sum(open_price * open_interest) / sum(open_interest))::text as open_price,
                    (sum(high_price * open_interest) / sum(open_interest))::text as high_price,
                    (sum(low_price * open_interest) / sum(open_interest))::text as low_price,
                    (sum(close_price * open_interest) / sum(open_interest))::text as close_price,
                    null::text as source_contract
               from usable
              group by trade_date
              order by trade_date"
        ),
        // 持仓量最大的那个合约。并列时取合约代码小的（近月），只为让结果稳定——
        // 同一天两次查询不该给出不同的主力。
        VarietyCandleMode::DominantUnadjusted => format!(
            "{picked}
             select distinct on (trade_date)
                    trade_date,
                    open_price::text, high_price::text,
                    low_price::text, close_price::text,
                    contract as source_contract
               from usable
              order by trade_date, open_interest desc, contract"
        ),
    }
}

/// 单个合约自己的 K 线。
///
/// 与 [`load_variety_candles`] 共用返回类型，但这里是**真实行情**，没有任何合成：
/// 选定了具体合约就该看那个合约实际成交出来的价，加权与主连都不适用。
pub async fn load_contract_candles(
    pool: &PgPool,
    workspace_id: Uuid,
    instrument: &str,
    contract: &str,
) -> Result<Vec<VarietyCandleRow>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let sql = format!(
        "select distinct on (trade_date)
                trade_date,
                open_price::text as open_price, high_price::text as high_price,
                low_price::text as low_price, close_price::text as close_price,
                contract as source_contract
           from price_history
          where workspace_id = $1 and instrument = $2 and contract = $3
            and open_price is not null and high_price is not null
            and low_price is not null and close_price is not null
          order by trade_date, {price_rank}, source",
        price_rank = PRICE_SOURCE_RANK,
    );
    let rows = sqlx::query(&sql)
        .bind(workspace_id)
        .bind(instrument)
        .bind(contract)
        .fetch_all(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| VarietyCandleRow {
            trade_date: row.get("trade_date"),
            open_price: row.get("open_price"),
            high_price: row.get("high_price"),
            low_price: row.get("low_price"),
            close_price: row.get("close_price"),
            source_contract: row.get("source_contract"),
        })
        .collect())
}

/// 品种汇总的合成 K 线。**只用于显示**，调用方不得把它喂进成本或盈亏计算。
pub async fn load_variety_candles(
    pool: &PgPool,
    workspace_id: Uuid,
    instrument: &str,
) -> Result<Vec<VarietyCandleRow>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(&variety_candles_sql(VarietyCandleMode::for_instrument(
        instrument,
    )))
    .bind(workspace_id)
    .bind(instrument)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| VarietyCandleRow {
            trade_date: row.get("trade_date"),
            open_price: row.get("open_price"),
            high_price: row.get("high_price"),
            low_price: row.get("low_price"),
            close_price: row.get("close_price"),
            source_contract: row.get("source_contract"),
        })
        .collect())
}

/// 品种汇总的取数 SQL。提成函数与 [`building_days_sql`] 同一个理由（测试要断言它本身）。
fn variety_building_sql() -> String {
    format!(
        "with picked as (
             -- 去重纪律同 building_days_sql：先按来源收敛，再求和。
             select distinct on (trade_date, contract, rank_type, {member_key})
                    trade_date, contract, rank_type, quantity, source
               from seat_history
              where workspace_id = $1 and instrument = $2 and member = any($3::text[])
                and not is_variety_total and contract is not null
                -- 成交量榜不是持仓榜，理由见 building_days_sql 里的同一条。
                and rank_type in ('long', 'short')
              order by trade_date, contract, rank_type, {member_key}, {source_rank}, source
         ), seats as (
             select contract, trade_date,
                    sum(case when rank_type = 'long' then quantity else 0 end) as long_position,
                    sum(case when rank_type = 'short' then quantity else 0 end) as short_position,
                    bool_or(source = 'reboard_inferred') as inferred
               from picked
              group by contract, trade_date
         ), prices as (
             select distinct on (contract, trade_date)
                    contract, trade_date, settlement_price
               from price_history
              where workspace_id = $1 and instrument = $2
              order by contract, trade_date, {price_rank}, source
         )
         -- left join：某合约某天有持仓却没有行情（零成交），那天成本不可知，
         -- 由成本引擎如实标注。反过来「有行情没持仓」在汇总口径下没有意义——
         -- 那可能是这个合约还没挂牌、已经到期，或者他不在这个合约的前二十，
         -- 三者从数据上分不开，一律当作那天他不在这个合约上。
         select s.contract, s.trade_date,
                s.long_position::text, s.short_position::text,
                s.inferred,
                p.settlement_price::text
           from seats s
           left join prices p on p.contract = s.contract and p.trade_date = s.trade_date
          order by s.contract, s.trade_date",
        member_key = member_key_sql(),
        source_rank = SEAT_SOURCE_RANK,
        price_rank = PRICE_SOURCE_RANK,
    )
}

/// 某席位在某品种下**每个合约**的逐日持仓与该合约结算价，按合约、交易日升序。
///
/// 品种汇总走这条而不是 [`load_building_days`]：成本是按合约推的，把各合约的成本
/// 序列算完再合并，才分得出「净多的那几个合约均价多少、净空的那几个多少」。
/// 交易所的品种汇总榜只有一个总手数，推不出成本——那条路上页面连结算价都取不到，
/// 于是成本、当日盈亏、累计盈亏三张图全是空的（2026-08-15 运营者报的就是这个）。
pub async fn load_variety_building_days(
    pool: &PgPool,
    workspace_id: Uuid,
    instrument: &str,
    member: &str,
) -> Result<Vec<VarietyBuildingRow>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let member_variants = seat_member_variants(&mut tx, workspace_id, member).await?;
    let rows = sqlx::query(&variety_building_sql())
        .bind(workspace_id)
        .bind(instrument)
        .bind(member_variants)
        .fetch_all(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| VarietyBuildingRow {
            contract: row.get("contract"),
            trade_date: row.get("trade_date"),
            long_position: row.get("long_position"),
            short_position: row.get("short_position"),
            inferred: row.get("inferred"),
            settlement_price: row.get("settlement_price"),
        })
        .collect())
}

/// 报告表里一家席位在一个品种上的一天。
#[derive(Debug, Clone)]
pub struct ReportNetRow {
    pub member: String,
    pub instrument: String,
    pub net_position: String,
    /// 由当日各腿「持仓 − 增减」反推出的前一日净仓。只在当日行上有意义;
    /// 任何一条腿的增减缺失则整个反推作废(见 SQL 里的吸收律注释)。
    pub inferred_prev: Option<String>,
    pub inferable: bool,
}

/// 报告表要用的「昨 / 今净持仓」。
///
/// 「昨」逐品种取**该品种上一个有席位数据的交易日**，不是简单地减一天，也不是全局
/// 统一取一个日子：上期所与郑商所到货时间不同，一刀切会把某个品种的昨仓取成前天，
/// 而表上看不出来——只会显示成「这家昨天没动」。
pub async fn load_report_nets(
    pool: &PgPool,
    workspace_id: Uuid,
    instruments: &[String],
    members: &[String],
    trade_date: Date,
) -> Result<(Vec<ReportNetRow>, Vec<ReportNetRow>), sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let sql = format!(
        "with prev as (
             select instrument, max(trade_date) as trade_date
               from seat_history
              where workspace_id = $1 and instrument = any($2::text[])
                and trade_date < $4 and not is_variety_total
              group by instrument
         ), picked as (
             -- 同一条身份键多个来源只取最可信的那个，纪律同 building_days_sql。
             select distinct on (s.instrument, {member_key}, s.contract, s.trade_date, s.rank_type)
                    s.instrument, {member_key} as member_key, s.trade_date,
                    s.rank_type, s.quantity, s.change
               from seat_history s
               left join prev p on p.instrument = s.instrument
              where s.workspace_id = $1 and s.instrument = any($2::text[])
                and not s.is_variety_total and s.contract is not null
                and s.rank_type in ('long', 'short')
                and (s.trade_date = $4 or s.trade_date = p.trade_date)
                and {member_key} = any($3::text[])
              order by s.instrument, {member_key}, s.contract, s.trade_date, s.rank_type,
                       {source_rank}, s.source
         )
         select member_key as member, instrument, trade_date,
                (sum(case when rank_type = 'long' then quantity else 0 end)
                 - sum(case when rank_type = 'short' then quantity else 0 end))::text as net,
                -- 反推的昨净仓:交易所的 change 相对**会员全量仓**算(回榜反推的既有
                -- 口径,龙虎榜铁律),所以 quantity − change 就是该腿前一日的真实仓,
                -- 哪怕前一日掉榜。sum 会跳过 change 为空的行,所以必须配着 inferable
                -- 用——有一条腿推不出,整个反推作废(吸收律)。
                (sum(case when rank_type = 'long' then quantity - change
                          else -(quantity - change) end))::text as inferred_prev,
                (count(*) filter (where change is null) = 0) as inferable
           from picked
          group by member_key, instrument, trade_date",
        member_key = member_key_sql(),
        source_rank = SEAT_SOURCE_RANK,
    );
    let rows = sqlx::query(&sql)
        .bind(workspace_id)
        .bind(instruments)
        .bind(members)
        .bind(trade_date)
        .fetch_all(&mut *tx)
        .await?;
    tx.commit().await?;

    let mut today = Vec::new();
    let mut yesterday = Vec::new();
    for row in rows {
        let item = ReportNetRow {
            member: row.get("member"),
            instrument: row.get("instrument"),
            net_position: row.get("net"),
            inferred_prev: row.get("inferred_prev"),
            inferable: row.get("inferable"),
        };
        if row.get::<Date, _>("trade_date") == trade_date {
            today.push(item);
        } else {
            yesterday.push(item);
        }
    }
    Ok((today, yesterday))
}

/// 报告表里「筹码」一列的取数：只捞**当日仍有持仓的那些合约**的完整历史。
///
/// 为什么可以只捞这些：筹码是当日各腿的加权成本，而某个合约的成本只取决于它自己
/// 那一段历史——当日没有持仓的合约对当日的两腿没有任何贡献。所以结果与席位页
/// 逐字相同（两边喂的都是 `build_variety_series`），代价却小得多：永安黄金全史
/// 81 个合约 10,125 行，当日在手的只有 3 个合约。
pub async fn load_report_cost_rows(
    pool: &PgPool,
    workspace_id: Uuid,
    instruments: &[String],
    members: &[String],
    trade_date: Date,
) -> Result<Vec<(String, String, VarietyBuildingRow)>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let sql = format!(
        "with held as (
             select distinct {member_key} as member_key, instrument, contract
               from seat_history
              where workspace_id = $1 and instrument = any($2::text[])
                and trade_date = $4 and not is_variety_total and contract is not null
                and rank_type in ('long', 'short')
                and {member_key} = any($3::text[])
         ), picked as (
             select distinct on (s.instrument, {member_key}, s.contract, s.trade_date, s.rank_type)
                    s.instrument, {member_key} as member_key, s.contract, s.trade_date,
                    s.rank_type, s.quantity
               from seat_history s
               join held h
                 on h.member_key = {member_key}
                and h.instrument = s.instrument
                and h.contract = s.contract
              where s.workspace_id = $1 and not s.is_variety_total
                and s.rank_type in ('long', 'short')
                -- 未来的行不能进来：报告要能翻回历史某一天，那天之后发生的事
                -- 不该影响那天的成本。
                and s.trade_date <= $4
              order by s.instrument, {member_key}, s.contract, s.trade_date, s.rank_type,
                       {source_rank}, s.source
         ), seats as (
             select instrument, member_key, contract, trade_date,
                    sum(case when rank_type = 'long' then quantity else 0 end) as long_position,
                    sum(case when rank_type = 'short' then quantity else 0 end) as short_position
               from picked
              group by instrument, member_key, contract, trade_date
         ), prices as (
             select distinct on (contract, trade_date)
                    contract, trade_date, settlement_price
               from price_history
              where workspace_id = $1 and instrument = any($2::text[]) and trade_date <= $4
                -- 只取在手那几个合约的行情。少了这一条会把该品种全部历史行情
                -- （AU/AG 合计 23.9 万行）拖进来做 distinct on：生产实测
                -- RLS runtime 角色下 606 毫秒，加上之后 366 毫秒，结果一模一样。
                and contract in (select contract from held)
              order by contract, trade_date, {price_rank}, source
         )
         select s.member_key as member, s.instrument, s.contract, s.trade_date,
                s.long_position::text, s.short_position::text,
                p.settlement_price::text
           from seats s
           left join prices p on p.contract = s.contract and p.trade_date = s.trade_date
          order by s.member_key, s.instrument, s.contract, s.trade_date",
        member_key = member_key_sql(),
        source_rank = SEAT_SOURCE_RANK,
        price_rank = PRICE_SOURCE_RANK,
    );
    let rows = sqlx::query(&sql)
        .bind(workspace_id)
        .bind(instruments)
        .bind(members)
        .bind(trade_date)
        .fetch_all(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| {
            (
                row.get("member"),
                row.get("instrument"),
                VarietyBuildingRow {
                    contract: row.get("contract"),
                    trade_date: row.get("trade_date"),
                    long_position: row.get("long_position"),
                    short_position: row.get("short_position"),
                    // 报告表成本链用不到推算标(标记只服务展示),这条查询也没带该列。
                    inferred: false,
                    settlement_price: row.get("settlement_price"),
                },
            )
        })
        .collect())
}

/// 报告表上半部分（压力位网格）某一天的记录，连同它实际来自哪一天。
///
/// 那天没有记录就回落到**最近一个有记录的日子**：报告是逐日的，但压力位不是每天
/// 都变，让运营者每天从空表重敲一遍是纯粹的重复劳动。界面据 `source_date` 标明
/// 「这是 X 日填的，尚未确认」，不冒充当天已经填过。
pub async fn load_report_levels(
    pool: &PgPool,
    workspace_id: Uuid,
    trade_date: Date,
) -> Result<Option<(Date, Value)>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let row = sqlx::query(
        "select trade_date, cells from overview_report_levels
          where workspace_id = $1 and trade_date <= $2
          order by trade_date desc limit 1",
    )
    .bind(workspace_id)
    .bind(trade_date)
    .fetch_optional(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(row.map(|row| (row.get("trade_date"), row.get("cells"))))
}

pub async fn save_report_levels(
    pool: &PgPool,
    workspace_id: Uuid,
    trade_date: Date,
    cells: &Value,
) -> Result<(), sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    sqlx::query(
        "insert into overview_report_levels (workspace_id, trade_date, cells)
         values ($1, $2, $3)
         on conflict (workspace_id, trade_date)
         do update set cells = excluded.cells, updated_at = now()",
    )
    .bind(workspace_id)
    .bind(trade_date)
    .bind(cells)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(())
}

/// 四组席位名单。返回的是**归一后**的会员名，与席位页同一口径。
pub async fn load_report_seat_groups(
    pool: &PgPool,
    workspace_id: Uuid,
) -> Result<Vec<(String, Vec<String>)>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(
        "select group_key, members from overview_report_seat_groups
          where workspace_id = $1",
    )
    .bind(workspace_id)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| (row.get("group_key"), row.get("members")))
        .collect())
}

pub async fn save_report_seat_groups(
    pool: &PgPool,
    workspace_id: Uuid,
    groups: &[(String, Vec<String>)],
) -> Result<(), sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    for (key, members) in groups {
        sqlx::query(
            "insert into overview_report_seat_groups (workspace_id, group_key, members)
             values ($1, $2, $3)
             on conflict (workspace_id, group_key)
             do update set members = excluded.members, updated_at = now()",
        )
        .bind(workspace_id)
        .bind(key)
        .bind(members)
        .execute(&mut *tx)
        .await?;
    }
    tx.commit().await?;
    Ok(())
}

/// 某个交易日各交易所的数据到齐情况。
#[derive(Debug, Clone)]
pub struct DataFreshnessDay {
    pub trade_date: Date,
    /// 那天有数据的交易所，逗号分隔、已排序。
    pub exchanges: String,
    /// 各所首次入库时刻,形如 "CZCE@15:41,SHFE@17:36"(北京时间)。口径见
    /// SEAT_FRESHNESS_SQL 上的注释。
    pub arrivals: String,
}

/// 回看窗。10 个交易日通常是 14 个自然日，撞上春节可能拉到 4 周——45 天留够余量。
/// 窗口也是性能约束：不加它就是全表 group by，runtime 角色下实测 4.5 秒，
/// 一个首页等不起；限到 45 天走索引倒扫，149 毫秒。
const FRESHNESS_LOOKBACK_DAYS: i32 = 45;
const FRESHNESS_DAYS: i64 = 10;

// arrivals 列 = 各所首次入库时刻(北京时间 HH:MM),格式 "CZCE@15:41,SHFE@17:36"。
// loaded_at 自 2026-08-16 起不再被 upsert 刷新(装载侧四处同批改),因此
// min(loaded_at) 就是"该所当日数据首次到达"——运营者立项的采集源更新时刻画像。
// 在此之前入库的历史日子,时刻反映的是最后一轮补采,当参考即可。
// 反推行(reboard_inferred)不算到达,按 source 排除(反推行不得进统计的教训)。
const SEAT_FRESHNESS_SQL: &str = "
    select trade_date,
           string_agg(exchange, ',' order by exchange) as exchanges,
           string_agg(exchange || '@' ||
                      to_char(first_at at time zone 'Asia/Shanghai', 'HH24:MI'),
                      ',' order by exchange) as arrivals
      from (select trade_date, exchange, min(loaded_at) as first_at
              from seat_history
             where workspace_id = $1 and not is_variety_total
               and source <> 'reboard_inferred'
               and trade_date >= current_date - $2::int
             group by trade_date, exchange) g
     group by trade_date
     order by trade_date desc
     limit $3";

const PRICE_FRESHNESS_SQL: &str = "
    select trade_date,
           string_agg(exchange, ',' order by exchange) as exchanges,
           string_agg(exchange || '@' ||
                      to_char(first_at at time zone 'Asia/Shanghai', 'HH24:MI'),
                      ',' order by exchange) as arrivals
      from (select trade_date, exchange, min(loaded_at) as first_at
              from price_history
             where workspace_id = $1
               and trade_date >= current_date - $2::int
             group by trade_date, exchange) g
     group by trade_date
     order by trade_date desc
     limit $3";

/// 最近若干个交易日，席位与行情各自到了哪些交易所。
///
/// 首页用它回答「数据到齐了吗」。**只报事实，不判定「正常」**：哪几个所该有数据
/// 由界面按近期出现过的所来推，而不是在这里写死一份名单——写死的名单会在
/// 交易所增减或某家长期停采时变成一个没人记得改的谎。
pub async fn data_freshness(
    pool: &PgPool,
    workspace_id: Uuid,
) -> Result<(Vec<DataFreshnessDay>, Vec<DataFreshnessDay>), sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let mut out = Vec::with_capacity(2);
    for sql in [SEAT_FRESHNESS_SQL, PRICE_FRESHNESS_SQL] {
        let rows = sqlx::query(sql)
            .bind(workspace_id)
            .bind(FRESHNESS_LOOKBACK_DAYS)
            .bind(FRESHNESS_DAYS)
            .fetch_all(&mut *tx)
            .await?;
        out.push(
            rows.into_iter()
                .map(|row| DataFreshnessDay {
                    trade_date: row.get("trade_date"),
                    exchanges: row
                        .get::<Option<String>, _>("exchanges")
                        .unwrap_or_default(),
                    arrivals: row.get::<Option<String>, _>("arrivals").unwrap_or_default(),
                })
                .collect::<Vec<_>>(),
        );
    }
    tx.commit().await?;
    let prices = out.pop().unwrap_or_default();
    let seats = out.pop().unwrap_or_default();
    Ok((seats, prices))
}

/// 建仓过程一天的原始素材：K 线、该席位净持仓、当日结算价。
#[derive(Debug, Clone)]
pub struct BuildingDay {
    pub trade_date: Date,
    pub open_price: Option<String>,
    pub high_price: Option<String>,
    pub low_price: Option<String>,
    pub close_price: Option<String>,
    pub settlement_price: Option<String>,
    /// `None` = 那天该席位不在前二十榜上，持仓未知（不是零）。
    pub long_position: Option<String>,
    pub short_position: Option<String>,
    /// 该日持仓含回榜反推成分：他实际未上榜，数字由回榜日的增减倒推。
    pub inferred: bool,
}

/// 某席位在某合约（或某品种汇总）的逐日持仓与行情。
///
/// 行情按合约取；品种汇总没有单一合约的 K 线，所以那一档只出持仓与成本，
/// K 线留空——把某个合约的 K 线安在品种汇总上，是把两件事画成一件。
///
/// 用 full outer 的思路对齐两边：某天有行情没持仓（该席位掉出前 20）要出现，
/// 某天有持仓没行情（零成交）也要出现。任一边缺失都由界面如实断开，而不是插值。
pub async fn load_building_days(
    pool: &PgPool,
    workspace_id: Uuid,
    instrument: &str,
    member: &str,
    contract: Option<&str>,
) -> Result<Vec<BuildingDay>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let member_variants = seat_member_variants(&mut tx, workspace_id, member).await?;
    let rows = sqlx::query(&building_days_sql())
        .bind(workspace_id)
        .bind(instrument)
        .bind(member_variants)
        .bind(contract)
        .fetch_all(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| BuildingDay {
            trade_date: row.get("trade_date"),
            open_price: row.get("open_price"),
            high_price: row.get("high_price"),
            low_price: row.get("low_price"),
            close_price: row.get("close_price"),
            settlement_price: row.get("settlement_price"),
            long_position: row.get("long_position"),
            short_position: row.get("short_position"),
            inferred: row.get("inferred"),
        })
        .collect())
}

/// 某会员在某品种上**历史持有过的全部合约**，新月份在前。
///
/// 建仓过程页的合约选择器要靠它。原先那份列表是从「所选交易日当天的持仓行」推导的，
/// 于是只列得出当天还在榜的那两三个合约——运营者 2026-08-12 指出选了高盛之后
/// 挑不到 AU2608，正是这个原因：那天它已经不在榜上了，可它的建仓过程仍然值得看。
/// 合约列表本来就不该随日期变：换个日子就少几个选项，是把「今天在榜」误当成
/// 「存在过」。
///
/// 过滤三项全是原始列上的等值比较，RLS 下照常走索引（生产实测：高盛黄金 21 个
/// 合约 5.3 毫秒，永安黄金 82 个合约 23 毫秒）。理由见 `MEMBER_KEY` 上的教训。
pub async fn seat_member_contracts(
    pool: &PgPool,
    workspace_id: Uuid,
    member: &str,
    instrument: &str,
) -> Result<Vec<String>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let member_variants = seat_member_variants(&mut tx, workspace_id, member).await?;
    let rows = sqlx::query_scalar::<_, String>(
        "select distinct contract from seat_history
          where workspace_id = $1 and member = any($2) and instrument = $3
            and contract is not null
          order by contract desc limit 2000",
    )
    .bind(workspace_id)
    .bind(&member_variants)
    .bind(instrument)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows)
}

/// 某品种下有过持仓的会员，按最近一次出现的持仓量排序——界面上的会员选择器要靠它。
pub async fn seat_members(
    pool: &PgPool,
    workspace_id: Uuid,
    instrument: Option<&str>,
) -> Result<Vec<String>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    // 原始列跳跃扫描 + Rust 归一。曾把归一化正则放进 SQL 的分组与排序里，
    // futures_runtime + RLS 下 60 秒超时（futures_app 下 121 毫秒），席位页会员
    // 下拉整个空掉——教训全文见 MEMBER_KEY。生产实测本走法 RLS 下 235 毫秒。
    let raw = sqlx::query_scalar::<_, String>(RAW_MEMBER_WALK_SQL)
        .bind(workspace_id)
        .bind(instrument)
        .fetch_all(&mut *tx)
        .await?;
    tx.commit().await?;
    let mut names: Vec<String> = raw.iter().map(|name| normalize_member(name)).collect();
    names.sort();
    names.dedup();
    names.truncate(500);
    Ok(names)
}

/// 该品种的点值。盈亏必须乘它而不是合约单位——八个品种里只有鸡蛋两者不等。
pub async fn instrument_price_multiplier(
    pool: &PgPool,
    workspace_id: Uuid,
    instrument: &str,
) -> Result<Option<String>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let value = sqlx::query_scalar::<_, Option<String>>(
        "select price_multiplier::text from instruments
          where workspace_id = $1 and upper(code) = $2 limit 1",
    )
    .bind(workspace_id)
    .bind(instrument)
    .fetch_optional(&mut *tx)
    .await?
    .flatten();
    tx.commit().await?;
    Ok(value)
}

/// 套利监控的一组组合在某一天的位置。
///
/// 数字全部用文本承载：它们要原样送到前端画轨道，一路保持精确比在边界上来回转换安全，
/// 与席位页的持仓量同一个理由。
#[derive(Debug, Clone)]
pub struct SpreadMonitorRow {
    pub trade_date: Date,
    pub instrument_1: String,
    pub contract_1: String,
    pub instrument_2: String,
    pub contract_2: String,
    pub is_cross_variety: bool,
    pub spread: String,
    pub pair_days: i32,
    pub pair_low: String,
    pub pair_high: String,
    pub pair_position: Option<String>,
    pub years_days: Option<i32>,
    pub years_low: Option<String>,
    pub years_high: Option<String>,
    pub years_position: Option<String>,
    /// 前一交易日的两轨位置。存位置而不是「是不是段首日」，理由同阈值：判定留到
    /// 读的时候按当次阈值算，任何阈值都能在任何一天重判（迁移 202608170001）。
    pub prev_pair_position: Option<String>,
    pub prev_years_position: Option<String>,
    /// 当年轨位置在近 20 个交易日（含当日）的最高/最低。「已拐头」的事实素材
    /// （迁移 202608170004）：判定本身（报警带 0.97/0.03、回撤 10 个百分点）
    /// 留在 API 常量里，读时套。
    pub pair_pos_hi20: Option<String>,
    pub pair_pos_lo20: Option<String>,
    /// 近 20 个交易日的拐头穿线次数（迁移 202608170005）。≥2 = 拐头反复、
    /// 「信号差」降级标的素材。
    pub turn_crosses_high_20: Option<i32>,
    pub turn_crosses_low_20: Option<i32>,
    /// 该月份组合模板在**可交易窗口**内、按日历位置对齐的历年表现
    /// （迁移 202608170002）。四个数一组,与阈值无关——它只跟今天是几月几号、
    /// 以及报的是高位还是低位有关:
    ///   hit/n   历年里「曾经触及」回归的年数。剩余期一长就趋近 100%,只是下限。
    ///   move    最有利那一刻相对起点走了多少点。
    ///   drift   一直持到窗口止点的净变化,已标准化成「正数 = 朝回归走」。
    ///   days    历年剩余交易日中位,给上面三个数一个时间尺度。
    pub revert_high_hit: Option<i32>,
    pub revert_high_n: Option<i32>,
    pub revert_high_move: Option<String>,
    pub revert_high_drift: Option<String>,
    /// 历年 MAE:锚点后先朝不利方向走的最大幅度。中位=补仓参考,最大=风险预留
    /// (DEC-067;盈亏比分级已回测否决,只留分母)。
    pub revert_high_mae: Option<String>,
    pub revert_high_mae_max: Option<String>,
    pub revert_high_days: Option<i32>,
    pub revert_low_hit: Option<i32>,
    pub revert_low_n: Option<i32>,
    pub revert_low_move: Option<String>,
    pub revert_low_drift: Option<String>,
    pub revert_low_mae: Option<String>,
    pub revert_low_mae_max: Option<String>,
    pub revert_low_days: Option<i32>,
}

/// 两条查询共用的列清单。写成常量是因为它出现在两处 SQL 里，而 `monitor_row`
/// 按列名取值——三处漏改任何一处都是运行期才炸的 `ColumnNotFound`。
const MONITOR_COLUMNS: &str = "trade_date, instrument_1, contract_1, instrument_2, contract_2,
            is_cross_variety, spread::text, pair_days,
            pair_low::text, pair_high::text, pair_position::text,
            years_days, years_low::text, years_high::text, years_position::text,
            prev_pair_position::text, prev_years_position::text,
            pair_pos_hi20::text, pair_pos_lo20::text,
            turn_crosses_high_20, turn_crosses_low_20,
            revert_high_hit, revert_high_n, revert_high_move::text,
            revert_high_drift::text, revert_high_mae::text,
            revert_high_mae_max::text, revert_high_days,
            revert_low_hit, revert_low_n, revert_low_move::text,
            revert_low_drift::text, revert_low_mae::text,
            revert_low_mae_max::text, revert_low_days";

/// 监控页的当前快照：**每组组合各取自己最新的那一条**。
///
/// 不能写成「取最新日期的全部行」。大商所的行情比郑商所晚一天到（08-11 那天郑商所
/// 已经有数，大商所还停在 08-10），按最新日期一刀切会把焦煤鸡蛋生猪整片漏掉，
/// 而页面上看不出漏了——它只是少了几行。
///
/// `stale_days` 是兜底：合约到期后不再有新快照，它的最后一条会永远留在表里。
/// 超过这个天数没更新的组合不再当作「当前」。
static MONITOR_SNAPSHOT_SQL: std::sync::LazyLock<String> = std::sync::LazyLock::new(|| {
    format!(
        "with newest as (
         select max(trade_date) d from spread_monitor_daily where workspace_id = $1
     )
     select distinct on (contract_1, contract_2)
            {MONITOR_COLUMNS}
       from spread_monitor_daily, newest
      where workspace_id = $1
        and trade_date > newest.d - ($2 || ' days')::interval
      order by contract_1, contract_2, trade_date desc"
    )
});

pub async fn spread_monitor_snapshot(
    pool: &PgPool,
    workspace_id: Uuid,
    stale_days: i32,
) -> Result<Vec<SpreadMonitorRow>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(MONITOR_SNAPSHOT_SQL.as_str())
        .bind(workspace_id)
        .bind(stale_days.to_string())
        .fetch_all(&mut *tx)
        .await?;
    tx.commit().await?;
    Ok(rows.into_iter().map(monitor_row).collect())
}

/// 某一天的快照，供翻历史用。触发与否是读的时候按阈值算的，所以任何一天都能
/// 用任何阈值重新判定——这正是当初存位置而不存结论的目的。
pub async fn spread_monitor_on(
    pool: &PgPool,
    workspace_id: Uuid,
    trade_date: Date,
) -> Result<Vec<SpreadMonitorRow>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(&format!(
        "select {MONITOR_COLUMNS}
           from spread_monitor_daily
          where workspace_id = $1 and trade_date = $2
          order by instrument_1, contract_1, contract_2"
    ))
    .bind(workspace_id)
    .bind(trade_date)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows.into_iter().map(monitor_row).collect())
}

/// 全部快照日的所有行,新日期在前(DEC-070「历史信号」视图)。
///
/// 量级:47 组合 × 已存快照日,几千行的水平;读时判定与单日路径完全同一套,
/// 不另写口径。前端在历史模式下只渲染进场行,过滤放在前端是为了让
/// 「什么算进场」只有一处定义(isEntry)。
pub async fn spread_monitor_history(
    pool: &PgPool,
    workspace_id: Uuid,
) -> Result<Vec<SpreadMonitorRow>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(&format!(
        "select {MONITOR_COLUMNS}
           from spread_monitor_daily
          where workspace_id = $1
          order by trade_date desc, instrument_1, contract_1, contract_2"
    ))
    .bind(workspace_id)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows.into_iter().map(monitor_row).collect())
}

/// 有快照的交易日，最新在前。历史页的日期选择器要靠它。
pub async fn spread_monitor_dates(
    pool: &PgPool,
    workspace_id: Uuid,
    limit: i64,
) -> Result<Vec<Date>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query_scalar::<_, Date>(
        "select distinct trade_date from spread_monitor_daily
          where workspace_id = $1 order by trade_date desc limit $2",
    )
    .bind(workspace_id)
    .bind(limit)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows)
}

fn monitor_row(row: sqlx::postgres::PgRow) -> SpreadMonitorRow {
    SpreadMonitorRow {
        trade_date: row.get("trade_date"),
        instrument_1: row.get("instrument_1"),
        contract_1: row.get("contract_1"),
        instrument_2: row.get("instrument_2"),
        contract_2: row.get("contract_2"),
        is_cross_variety: row.get("is_cross_variety"),
        spread: row.get("spread"),
        pair_days: row.get("pair_days"),
        pair_low: row.get("pair_low"),
        pair_high: row.get("pair_high"),
        pair_position: row.get("pair_position"),
        years_days: row.get("years_days"),
        years_low: row.get("years_low"),
        years_high: row.get("years_high"),
        years_position: row.get("years_position"),
        prev_pair_position: row.get("prev_pair_position"),
        prev_years_position: row.get("prev_years_position"),
        pair_pos_hi20: row.get("pair_pos_hi20"),
        pair_pos_lo20: row.get("pair_pos_lo20"),
        turn_crosses_high_20: row.get("turn_crosses_high_20"),
        turn_crosses_low_20: row.get("turn_crosses_low_20"),
        revert_high_hit: row.get("revert_high_hit"),
        revert_high_n: row.get("revert_high_n"),
        revert_high_move: row.get("revert_high_move"),
        revert_high_drift: row.get("revert_high_drift"),
        revert_high_mae: row.get("revert_high_mae"),
        revert_high_mae_max: row.get("revert_high_mae_max"),
        revert_high_days: row.get("revert_high_days"),
        revert_low_hit: row.get("revert_low_hit"),
        revert_low_n: row.get("revert_low_n"),
        revert_low_move: row.get("revert_low_move"),
        revert_low_drift: row.get("revert_low_drift"),
        revert_low_mae: row.get("revert_low_mae"),
        revert_low_mae_max: row.get("revert_low_mae_max"),
        revert_low_days: row.get("revert_low_days"),
    }
}
