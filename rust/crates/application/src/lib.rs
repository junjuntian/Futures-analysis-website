pub mod import_jobs;
pub mod imports;
pub mod spread_analytics;

use serde::Serialize;
use time::OffsetDateTime;
use utoipa::ToSchema;

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct HealthStatus {
    pub status: &'static str,
    #[serde(with = "time::serde::rfc3339")]
    #[schema(value_type = String, example = "2026-07-24T00:00:00Z")]
    pub checked_at: OffsetDateTime,
}

impl HealthStatus {
    pub fn live() -> Self {
        Self {
            status: "ok",
            checked_at: OffsetDateTime::now_utc(),
        }
    }

    pub fn ready(is_ready: bool) -> Self {
        Self {
            status: if is_ready { "ready" } else { "not_ready" },
            checked_at: OffsetDateTime::now_utc(),
        }
    }
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct VersionInfo {
    pub name: String,
    pub version: String,
    pub git_sha: String,
}

impl VersionInfo {
    pub fn new(name: String, version: String) -> Self {
        Self {
            name,
            version,
            git_sha: option_env!("GIT_SHA").unwrap_or("local").into(),
        }
    }
}
