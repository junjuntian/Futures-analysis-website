use serde::Serialize;
use std::{env, fs, net::SocketAddr};
use thiserror::Error;
use uuid::Uuid;

pub type Result<T> = std::result::Result<T, AppError>;

#[derive(Debug, Error)]
pub enum AppError {
    #[error("missing required configuration: {0}")]
    MissingConfig(&'static str),
    #[error("invalid configuration: {0}")]
    InvalidConfig(String),
}

#[derive(Debug, Clone)]
pub struct AppConfig {
    pub app_name: String,
    pub app_version: String,
    pub bind_addr: SocketAddr,
    pub database_url: String,
}

impl AppConfig {
    pub fn from_env(default_port: u16) -> Result<Self> {
        let app_name = env::var("APP_NAME").unwrap_or_else(|_| "futures-analysis-platform".into());
        let app_version =
            env::var("APP_VERSION").unwrap_or_else(|_| env!("CARGO_PKG_VERSION").into());
        let bind_addr = env::var("BIND_ADDR")
            .unwrap_or_else(|_| format!("0.0.0.0:{default_port}"))
            .parse()
            .map_err(|err| AppError::InvalidConfig(format!("BIND_ADDR: {err}")))?;
        let database_url_file = env::var("DATABASE_URL_FILE").ok();
        if env::var("APP_ENV").is_ok_and(|value| value.eq_ignore_ascii_case("production"))
            && database_url_file.is_none()
        {
            return Err(AppError::InvalidConfig(
                "DATABASE_URL_FILE is required in production".into(),
            ));
        }
        let database_url = database_url_file
            .and_then(|path| fs::read_to_string(path).ok())
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .or_else(|| env::var("DATABASE_URL").ok())
            .ok_or(AppError::MissingConfig("DATABASE_URL"))?;

        Ok(Self {
            app_name,
            app_version,
            bind_addr,
            database_url,
        })
    }

    pub fn redacted_database_url(&self) -> &'static str {
        "[redacted]"
    }
}

#[derive(Debug, Serialize)]
pub struct ApiResponse<T: Serialize> {
    pub data: T,
    pub meta: ResponseMeta,
}

impl<T: Serialize> ApiResponse<T> {
    pub fn new(data: T, request_id: Uuid) -> Self {
        Self {
            data,
            meta: ResponseMeta { request_id },
        }
    }
}

#[derive(Debug, Serialize)]
pub struct ResponseMeta {
    pub request_id: Uuid,
}
