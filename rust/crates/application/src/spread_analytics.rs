use async_trait::async_trait;
use domain::spread_analytics::RawSpreadPoint;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use time::OffsetDateTime;
use utoipa::ToSchema;

pub const SANHE_PROVIDER_CODE: &str = "sanhe";
pub const SANHE_SOURCE_CODE: &str = "sanhe_spread_readonly";
pub const SANHE_SOURCE_DISPLAY_NAME: &str = "三禾数据";
pub const SANHE_PROVIDER_ALGORITHM_VERSION: &str = "sanhe_spread_v1";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProviderEndpoint {
    AllVarieties,
    VarietyContracts,
    ArbitrageVarieties,
}

impl ProviderEndpoint {
    pub const fn code(self) -> &'static str {
        match self {
            Self::AllVarieties => "all_varieties",
            Self::VarietyContracts => "variety_contracts",
            Self::ArbitrageVarieties => "arbitrage_varieties",
        }
    }

    pub const fn path(self) -> &'static str {
        match self {
            Self::AllVarieties => "/ajax/all_varieties.php",
            Self::VarietyContracts => "/ajax/variety_contracts.php",
            Self::ArbitrageVarieties => "/ajax/arbitrage_varieties.php",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema, PartialEq, Eq)]
pub struct ProviderVariety {
    pub market: String,
    pub name: String,
    pub symbol: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema, PartialEq, Eq)]
pub struct ProviderContractMonths {
    pub variety: String,
    pub months: Vec<String>,
    pub basis: Option<i64>,
}

#[derive(Debug, Clone)]
pub struct ProviderSeries {
    pub points: Vec<RawSpreadPoint>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ProviderResultKind {
    Ok,
    Empty,
}

impl ProviderResultKind {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Ok => "ok",
            Self::Empty => "empty",
        }
    }
}

#[derive(Debug, Clone)]
pub struct ProviderFetch<T> {
    pub data: T,
    pub raw_payload: Value,
    pub fetched_at: OffsetDateTime,
    pub http_status: u16,
    pub business_code: i64,
    pub result_kind: ProviderResultKind,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SpreadProviderErrorKind {
    Unavailable,
    RateLimited,
    Forbidden,
    ContractChanged,
}

impl SpreadProviderErrorKind {
    pub const fn stable_code(&self) -> &'static str {
        match self {
            Self::Unavailable => "spread_provider_unavailable",
            Self::RateLimited => "spread_provider_rate_limited",
            Self::Forbidden => "spread_provider_forbidden",
            Self::ContractChanged => "spread_provider_contract_changed",
        }
    }
}

#[derive(Debug, thiserror::Error)]
#[error("spread provider failed: {kind:?}")]
pub struct SpreadProviderError {
    pub kind: SpreadProviderErrorKind,
    pub retry_after_seconds: Option<u64>,
}

impl SpreadProviderError {
    pub fn new(kind: SpreadProviderErrorKind) -> Self {
        Self {
            kind,
            retry_after_seconds: None,
        }
    }

    pub fn with_retry_after(kind: SpreadProviderErrorKind, seconds: u64) -> Self {
        Self {
            kind,
            retry_after_seconds: Some(seconds),
        }
    }
}

#[async_trait]
pub trait SpreadSeriesProvider: Send + Sync {
    async fn list_varieties(
        &self,
    ) -> Result<ProviderFetch<Vec<ProviderVariety>>, SpreadProviderError>;

    async fn list_contract_months(
        &self,
        variety: &str,
    ) -> Result<ProviderFetch<ProviderContractMonths>, SpreadProviderError>;

    async fn load_series(
        &self,
        variety1: &str,
        code1: &str,
        variety2: &str,
        code2: &str,
    ) -> Result<ProviderFetch<ProviderSeries>, SpreadProviderError>;
}
