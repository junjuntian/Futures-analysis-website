use application::spread_analytics::{
    ProviderContractMonths, ProviderEndpoint, ProviderFetch, ProviderResultKind, ProviderSeries,
    ProviderVariety, SpreadProviderError, SpreadProviderErrorKind, SpreadSeriesProvider,
};
use async_trait::async_trait;
use domain::spread_analytics::RawSpreadPoint;
use reqwest::{Client, StatusCode, header};
use rust_decimal::{Decimal, RoundingStrategy};
use serde_json::Value;
use std::{
    net::{IpAddr, Ipv4Addr, Ipv6Addr, SocketAddr},
    time::Duration,
};
use time::{Date, OffsetDateTime, macros::format_description};
use tokio::net::lookup_host;
use tracing::warn;

const BASE_URL: &str = "https://www.sanheshuju.com";
const ALLOWED_HOST: &str = "www.sanheshuju.com";
const MAX_RESPONSE_BYTES: usize = 5 * 1024 * 1024;

#[derive(Debug, Clone, Default)]
pub struct SanheSpreadSeriesProvider;

impl SanheSpreadSeriesProvider {
    pub fn new() -> Self {
        Self
    }

    async fn pinned_client() -> Result<Client, SpreadProviderError> {
        let addresses: Vec<SocketAddr> = lookup_host((ALLOWED_HOST, 443))
            .await
            .map_err(|_| SpreadProviderError::new(SpreadProviderErrorKind::Unavailable))?
            .collect();
        if addresses.is_empty() || addresses.iter().any(|address| !is_public_ip(address.ip())) {
            return Err(SpreadProviderError::new(
                SpreadProviderErrorKind::Unavailable,
            ));
        }
        Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .https_only(true)
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(20))
            .user_agent("futures-analysis-sanhe-readonly/1.0")
            .default_headers(default_headers())
            .resolve_to_addrs(ALLOWED_HOST, &addresses)
            .build()
            .map_err(|_| SpreadProviderError::new(SpreadProviderErrorKind::Unavailable))
    }

    async fn post(
        &self,
        endpoint: ProviderEndpoint,
        form: &[(&str, &str)],
    ) -> Result<(Value, OffsetDateTime, u16), SpreadProviderError> {
        let url = endpoint_url(endpoint)?;
        let response = Self::pinned_client()
            .await?
            .post(url)
            .form(form)
            .send()
            .await
            .map_err(|_| SpreadProviderError::new(SpreadProviderErrorKind::Unavailable))?;
        let status = response.status();
        if status == StatusCode::UNAUTHORIZED || status == StatusCode::FORBIDDEN {
            return Err(SpreadProviderError::new(SpreadProviderErrorKind::Forbidden));
        }
        if status == StatusCode::TOO_MANY_REQUESTS {
            let retry_after = response
                .headers()
                .get(header::RETRY_AFTER)
                .and_then(|value| value.to_str().ok())
                .and_then(|value| value.parse::<u64>().ok())
                .unwrap_or(60);
            return Err(SpreadProviderError::with_retry_after(
                SpreadProviderErrorKind::RateLimited,
                retry_after,
            ));
        }
        if !status.is_success() {
            return Err(SpreadProviderError::new(
                SpreadProviderErrorKind::Unavailable,
            ));
        }
        let content_type_is_json = response
            .headers()
            .get(header::CONTENT_TYPE)
            .and_then(|value| value.to_str().ok())
            .is_some_and(|value| {
                value.starts_with("application/json")
                    || value.starts_with("text/json")
                    || value.starts_with("text/javascript")
            });
        if !content_type_is_json {
            return Err(SpreadProviderError::new(
                SpreadProviderErrorKind::ContractChanged,
            ));
        }
        if response
            .content_length()
            .is_some_and(|length| length > MAX_RESPONSE_BYTES as u64)
        {
            return Err(SpreadProviderError::new(
                SpreadProviderErrorKind::ContractChanged,
            ));
        }
        let mut body = Vec::new();
        let mut response = response;
        while let Some(chunk) = response
            .chunk()
            .await
            .map_err(|_| SpreadProviderError::new(SpreadProviderErrorKind::Unavailable))?
        {
            if body.len().saturating_add(chunk.len()) > MAX_RESPONSE_BYTES {
                return Err(SpreadProviderError::new(
                    SpreadProviderErrorKind::ContractChanged,
                ));
            }
            body.extend_from_slice(&chunk);
        }
        let payload: Value = serde_json::from_slice(&body)
            .map_err(|_| SpreadProviderError::new(SpreadProviderErrorKind::ContractChanged))?;
        let code = business_code(&payload)?;
        if code == 1001 {
            return Err(SpreadProviderError::new(SpreadProviderErrorKind::Forbidden));
        }
        if code != 0 {
            warn!(
                provider = "sanhe",
                endpoint = endpoint.code(),
                code,
                "provider business error"
            );
            return Err(SpreadProviderError::new(
                SpreadProviderErrorKind::Unavailable,
            ));
        }
        Ok((payload, OffsetDateTime::now_utc(), status.as_u16()))
    }

    pub fn parse_varieties(
        payload: &Value,
    ) -> Result<(Vec<ProviderVariety>, ProviderResultKind), SpreadProviderError> {
        validate_success(payload)?;
        let Some(data) = payload.get("data") else {
            return Ok((Vec::new(), ProviderResultKind::Empty));
        };
        if data.is_null() || data.as_array().is_some_and(Vec::is_empty) {
            return Ok((Vec::new(), ProviderResultKind::Empty));
        }
        let rows = data
            .as_array()
            .or_else(|| data.get("varieties").and_then(Value::as_array))
            .ok_or_else(contract_changed)?;
        let mut varieties = Vec::with_capacity(rows.len());
        for row in rows {
            let market = required_string(row, "market")?;
            let name = required_string(row, "name")?;
            let symbol = required_string(row, "symbol")?;
            varieties.push(ProviderVariety {
                market,
                name,
                symbol,
            });
        }
        varieties.sort_by(|left, right| {
            left.market
                .cmp(&right.market)
                .then(left.name.cmp(&right.name))
        });
        let kind = if varieties.is_empty() {
            ProviderResultKind::Empty
        } else {
            ProviderResultKind::Ok
        };
        Ok((varieties, kind))
    }

    pub fn parse_contract_months(
        variety: &str,
        payload: &Value,
    ) -> Result<(ProviderContractMonths, ProviderResultKind), SpreadProviderError> {
        validate_success(payload)?;
        let data = payload.get("data").filter(|value| !value.is_null());
        let Some(data) = data else {
            return Ok((
                ProviderContractMonths {
                    variety: variety.to_string(),
                    months: Vec::new(),
                    basis: None,
                },
                ProviderResultKind::Empty,
            ));
        };
        let contracts = data
            .get("main_contracts")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let mut months = Vec::new();
        for month in contracts
            .split(',')
            .map(str::trim)
            .filter(|value| !value.is_empty())
        {
            if month.len() != 2
                || !month.chars().all(|character| character.is_ascii_digit())
                || !("01"..="12").contains(&month)
            {
                return Err(contract_changed());
            }
            if !months.iter().any(|existing| existing == month) {
                months.push(month.to_string());
            }
        }
        let basis = data.get("basis").and_then(Value::as_i64);
        let kind = if months.is_empty() {
            ProviderResultKind::Empty
        } else {
            ProviderResultKind::Ok
        };
        Ok((
            ProviderContractMonths {
                variety: variety.to_string(),
                months,
                basis,
            },
            kind,
        ))
    }

    pub fn parse_series(
        payload: &Value,
    ) -> Result<(ProviderSeries, ProviderResultKind), SpreadProviderError> {
        validate_success(payload)?;
        let Some(data) = payload.get("data").filter(|value| !value.is_null()) else {
            return Ok((
                ProviderSeries { points: Vec::new() },
                ProviderResultKind::Empty,
            ));
        };
        let dates = data
            .get("dates")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        let spreads = data
            .get("spreads")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        if dates.is_empty() && spreads.is_empty() {
            return Ok((
                ProviderSeries { points: Vec::new() },
                ProviderResultKind::Empty,
            ));
        }
        if dates.len() != spreads.len() {
            return Err(contract_changed());
        }
        let mut points = Vec::with_capacity(dates.len());
        for (date, spread) in dates.iter().zip(&spreads) {
            let trade_date = Date::parse(
                date.as_str().ok_or_else(contract_changed)?,
                &format_description!("[year]-[month]-[day]"),
            )
            .map_err(|_| contract_changed())?;
            let value = spread
                .get("value")
                .filter(|value| value.is_number())
                .ok_or_else(contract_changed)?
                .to_string()
                .parse::<Decimal>()
                .map_err(|_| contract_changed())?;
            if value.abs() >= Decimal::from(1_000_000_000_000_i64) {
                return Err(contract_changed());
            }
            let value = value.round_dp_with_strategy(8, RoundingStrategy::MidpointNearestEven);
            let from_code = required_string(spread, "from_code")?;
            let to_code = required_string(spread, "to_code")?;
            points.push(RawSpreadPoint {
                trade_date,
                value,
                from_code,
                to_code,
            });
        }
        for pair in points.windows(2) {
            if pair[1].trade_date <= pair[0].trade_date {
                return Err(contract_changed());
            }
        }
        Ok((ProviderSeries { points }, ProviderResultKind::Ok))
    }
}

fn endpoint_url(endpoint: ProviderEndpoint) -> Result<reqwest::Url, SpreadProviderError> {
    let url = reqwest::Url::parse(&format!("{BASE_URL}{}", endpoint.path()))
        .map_err(|_| contract_changed())?;
    validate_endpoint_url(&url, endpoint)?;
    Ok(url)
}

fn validate_endpoint_url(
    url: &reqwest::Url,
    endpoint: ProviderEndpoint,
) -> Result<(), SpreadProviderError> {
    if url.scheme() != "https"
        || url.host_str() != Some(ALLOWED_HOST)
        || url.port().is_some()
        || url.path() != endpoint.path()
        || url.query().is_some()
        || url.fragment().is_some()
        || !url.username().is_empty()
        || url.password().is_some()
    {
        return Err(contract_changed());
    }
    Ok(())
}

fn is_public_ip(ip: IpAddr) -> bool {
    match ip {
        IpAddr::V4(ip) => is_public_ipv4(ip),
        IpAddr::V6(ip) => ip
            .to_ipv4_mapped()
            .map_or_else(|| is_public_ipv6(ip), is_public_ipv4),
    }
}

fn is_public_ipv4(ip: Ipv4Addr) -> bool {
    let [first, second, _, _] = ip.octets();
    !ip.is_private()
        && !ip.is_loopback()
        && !ip.is_link_local()
        && !ip.is_broadcast()
        && !ip.is_documentation()
        && !ip.is_unspecified()
        && !ip.is_multicast()
        && first != 0
        && !(first == 100 && (64..=127).contains(&second))
        && !(first == 192 && second == 0)
        && !(first == 198 && (18..=19).contains(&second))
        && first < 240
}

fn is_public_ipv6(ip: Ipv6Addr) -> bool {
    !ip.is_loopback()
        && !ip.is_unspecified()
        && !ip.is_multicast()
        && !ip.is_unique_local()
        && !ip.is_unicast_link_local()
}

#[async_trait]
impl SpreadSeriesProvider for SanheSpreadSeriesProvider {
    async fn list_varieties(
        &self,
    ) -> Result<ProviderFetch<Vec<ProviderVariety>>, SpreadProviderError> {
        let (payload, fetched_at, status) = self.post(ProviderEndpoint::AllVarieties, &[]).await?;
        let (data, result_kind) = Self::parse_varieties(&payload)?;
        Ok(ProviderFetch {
            data,
            raw_payload: payload,
            fetched_at,
            http_status: status,
            business_code: 0,
            result_kind,
        })
    }

    async fn list_contract_months(
        &self,
        variety: &str,
    ) -> Result<ProviderFetch<ProviderContractMonths>, SpreadProviderError> {
        let (payload, fetched_at, status) = self
            .post(ProviderEndpoint::VarietyContracts, &[("variety", variety)])
            .await?;
        let (data, result_kind) = Self::parse_contract_months(variety, &payload)?;
        Ok(ProviderFetch {
            data,
            raw_payload: payload,
            fetched_at,
            http_status: status,
            business_code: 0,
            result_kind,
        })
    }

    async fn load_series(
        &self,
        variety1: &str,
        code1: &str,
        variety2: &str,
        code2: &str,
    ) -> Result<ProviderFetch<ProviderSeries>, SpreadProviderError> {
        let form = [
            ("variety1", variety1),
            ("code1", code1),
            ("variety2", variety2),
            ("code2", code2),
        ];
        let (payload, fetched_at, status) = self
            .post(ProviderEndpoint::ArbitrageVarieties, &form)
            .await?;
        let (data, result_kind) = Self::parse_series(&payload)?;
        Ok(ProviderFetch {
            data,
            raw_payload: payload,
            fetched_at,
            http_status: status,
            business_code: 0,
            result_kind,
        })
    }
}

fn default_headers() -> header::HeaderMap {
    let mut headers = header::HeaderMap::new();
    headers.insert(
        header::ACCEPT,
        header::HeaderValue::from_static("application/json, text/javascript, */*; q=0.01"),
    );
    headers.insert(
        "x-requested-with",
        header::HeaderValue::from_static("XMLHttpRequest"),
    );
    headers.insert(header::ORIGIN, header::HeaderValue::from_static(BASE_URL));
    headers.insert(
        header::REFERER,
        header::HeaderValue::from_static(
            "https://www.sanheshuju.com/#/fundamental/arbitrage/varieties/",
        ),
    );
    headers
}

fn validate_success(payload: &Value) -> Result<(), SpreadProviderError> {
    let code = business_code(payload)?;
    if code == 0 {
        Ok(())
    } else if code == 1001 {
        Err(SpreadProviderError::new(SpreadProviderErrorKind::Forbidden))
    } else {
        Err(SpreadProviderError::new(
            SpreadProviderErrorKind::Unavailable,
        ))
    }
}

fn business_code(payload: &Value) -> Result<i64, SpreadProviderError> {
    payload
        .as_object()
        .and_then(|object| object.get("code"))
        .and_then(Value::as_i64)
        .ok_or_else(contract_changed)
}

fn required_string(value: &Value, key: &str) -> Result<String, SpreadProviderError> {
    value
        .get(key)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
        .ok_or_else(contract_changed)
}

fn contract_changed() -> SpreadProviderError {
    SpreadProviderError::new(SpreadProviderErrorKind::ContractChanged)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use time::macros::date;

    #[test]
    fn parses_varieties_and_legal_empty() {
        let (items, kind) = SanheSpreadSeriesProvider::parse_varieties(&json!({
            "code": 0,
            "data": [{"market":"大商所","name":"焦煤","symbol":"JM"}]
        }))
        .unwrap();
        assert_eq!(kind, ProviderResultKind::Ok);
        assert_eq!(items[0].symbol, "JM");

        let (items, kind) = SanheSpreadSeriesProvider::parse_varieties(&json!({
            "code": 0,
            "data": []
        }))
        .unwrap();
        assert!(items.is_empty());
        assert_eq!(kind, ProviderResultKind::Empty);
    }

    #[test]
    fn parses_months_without_interpreting_basis() {
        let (months, kind) = SanheSpreadSeriesProvider::parse_contract_months(
            "玻璃",
            &json!({"code":0,"data":{"main_contracts":"01,05,09","basis":1}}),
        )
        .unwrap();
        assert_eq!(months.months, vec!["01", "05", "09"]);
        assert_eq!(months.basis, Some(1));
        assert_eq!(kind, ProviderResultKind::Ok);
    }

    #[test]
    fn ignores_upstream_statistics_and_parses_only_dates_and_spreads() {
        let (series, kind) = SanheSpreadSeriesProvider::parse_series(&json!({
            "code": 0,
            "data": {
                "dates": ["2025-01-02", "2025-01-03"],
                "spreads": [
                    {"value": -10, "from_code": "fg2505", "to_code": "sa2505"},
                    {"value": 4.5, "from_code": "fg2505", "to_code": "sa2505"}
                ],
                "year_data": {"2025": [999999]},
                "stat_ret": [{"year": 2025, "m1": 999999}],
                "stat_rate": {"m1": "100%"}
            }
        }))
        .unwrap();
        assert_eq!(kind, ProviderResultKind::Ok);
        assert_eq!(series.points.len(), 2);
        assert_eq!(series.points[0].trade_date, date!(2025 - 01 - 02));
        assert_eq!(series.points[1].value, Decimal::new(45, 1));
    }

    #[test]
    fn rejects_mismatched_arrays_and_bad_contract_codes() {
        let error = SanheSpreadSeriesProvider::parse_series(&json!({
            "code": 0,
            "data": {"dates": ["2025-01-02"], "spreads": []}
        }))
        .unwrap_err();
        assert_eq!(error.kind, SpreadProviderErrorKind::ContractChanged);
    }

    #[test]
    fn business_logout_is_forbidden() {
        let error = SanheSpreadSeriesProvider::parse_varieties(&json!({
            "code": 1001,
            "data": null
        }))
        .unwrap_err();
        assert_eq!(error.kind, SpreadProviderErrorKind::Forbidden);
    }

    #[test]
    fn empty_series_is_legal_and_dirty_upstream_statistics_are_ignored() {
        let (series, kind) = SanheSpreadSeriesProvider::parse_series(&json!({
            "code": 0,
            "data": {
                "dates": [],
                "spreads": [],
                "year_data": {"2025": [999999]},
                "stat_ret": [{"m1": 999999}],
                "stat_rate": {"m1": "100%"}
            }
        }))
        .unwrap();
        assert!(series.points.is_empty());
        assert_eq!(kind, ProviderResultKind::Empty);
    }

    #[test]
    fn provider_values_are_normalized_to_numeric_20_8_precision() {
        let (series, _) = SanheSpreadSeriesProvider::parse_series(&json!({
            "code": 0,
            "data": {
                "dates": ["2025-01-02"],
                "spreads": [{
                    "value": 0.123456789,
                    "from_code": "a2505",
                    "to_code": "b2505"
                }]
            }
        }))
        .unwrap();
        assert_eq!(series.points[0].value, Decimal::new(12_345_679, 8));
    }

    #[test]
    fn outbound_target_is_fixed_to_https_whitelisted_host_and_three_paths() {
        for endpoint in [
            ProviderEndpoint::AllVarieties,
            ProviderEndpoint::VarietyContracts,
            ProviderEndpoint::ArbitrageVarieties,
        ] {
            assert!(endpoint_url(endpoint).is_ok());
        }
        for target in [
            "http://www.sanheshuju.com/ajax/all_varieties.php",
            "https://evil.example/ajax/all_varieties.php",
            "https://www.sanheshuju.com/ajax/broker_positions.php",
            "https://www.sanheshuju.com/ajax/all_varieties.php?next=https://evil.example",
        ] {
            let url = reqwest::Url::parse(target).unwrap();
            assert!(validate_endpoint_url(&url, ProviderEndpoint::AllVarieties).is_err());
        }
    }

    #[test]
    fn dns_rebinding_addresses_are_rejected_before_the_http_client_is_built() {
        for address in [
            "127.0.0.1",
            "10.0.0.1",
            "172.16.0.1",
            "192.168.1.1",
            "169.254.169.254",
            "100.64.0.1",
            "::1",
            "fe80::1",
            "fc00::1",
            "::ffff:127.0.0.1",
        ] {
            assert!(
                !is_public_ip(address.parse().unwrap()),
                "accepted {address}"
            );
        }
        assert!(is_public_ip("93.184.216.34".parse().unwrap()));
        assert!(is_public_ip(
            "2606:2800:220:1:248:1893:25c8:1946".parse().unwrap()
        ));
    }

    #[test]
    fn redirects_are_disabled_and_no_automatic_retry_layer_is_configured() {
        let source = include_str!("sanhe_spread.rs");
        let implementation = source.split("#[cfg(test)]").next().unwrap();
        assert!(implementation.contains("redirect(reqwest::redirect::Policy::none())"));
        assert!(!implementation.contains(".retry("));
    }
}
