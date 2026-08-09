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
}

#[derive(Debug, Clone)]
pub struct NewFavorite<'a> {
    pub workspace_id: Uuid,
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

pub async fn save_series(
    pool: &PgPool,
    input: &SeriesPersistence<'_>,
) -> Result<Uuid, SpreadRepositoryError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, input.workspace_id).await?;
    let source_id = ensure_sanhe_source_in_tx(&mut tx, input.workspace_id).await?;
    let new_id = Uuid::now_v7();
    let inserted = sqlx::query_scalar::<_, Uuid>(
        "insert into spread_provider_series
            (id, workspace_id, provider_code, source_id, query_hash, business_date,
             query_json, fetched_at, data_cutoff_at, payload_hash, derivation_hash, price_basis,
             window_algorithm_version, statistics_algorithm_version, rule_version, created_by)
         values ($1, $2, 'sanhe', $3, $4, $5, $6, $7, $8, $9, $10,
                 'upstream_spread', $11, $12, $13, $14)
         on conflict (workspace_id, provider_code, query_hash, business_date, derivation_hash)
         do nothing
         returning id",
    )
    .bind(new_id)
    .bind(input.workspace_id)
    .bind(source_id)
    .bind(input.query_hash)
    .bind(input.business_date)
    .bind(input.query_json)
    .bind(input.fetched_at)
    .bind(input.data_cutoff_at)
    .bind(input.payload_hash)
    .bind(input.derivation_hash)
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
                     jsonb_build_object('series_id', $5::text, 'provider', 'sanhe'))",
        )
        .bind(Uuid::now_v7())
        .bind(input.workspace_id)
        .bind(input.actor_user_id)
        .bind(input.request_id)
        .bind(id)
        .execute(&mut *tx)
        .await?;
        id
    } else {
        sqlx::query_scalar::<_, Uuid>(
            "select id from spread_provider_series
              where workspace_id = $1 and provider_code = 'sanhe'
                and query_hash = $2 and business_date = $3 and derivation_hash = $4",
        )
        .bind(input.workspace_id)
        .bind(input.query_hash)
        .bind(input.business_date)
        .bind(input.derivation_hash)
        .fetch_one(&mut *tx)
        .await?
    };
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
         values ($1, $2, $3, 'sanhe', $4, $5, $6, $7)
         returning created_at",
    )
    .bind(id)
    .bind(input.workspace_id)
    .bind(input.name)
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
