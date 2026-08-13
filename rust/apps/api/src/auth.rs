use application::VersionInfo;
use argon2::{
    Algorithm, Argon2, Params, Version,
    password_hash::{PasswordHash, PasswordHasher, PasswordVerifier, SaltString},
};
use axum::{
    Json,
    extract::{Path, Query, State},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use common::ApiResponse;
use rand_core::{OsRng, RngCore};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use sqlx::{PgPool, Row};
use std::{
    collections::HashMap,
    env, fs,
    sync::{Arc, Mutex},
};
use subtle::ConstantTimeEq;
use time::{Duration, OffsetDateTime};
use utoipa::ToSchema;
use uuid::Uuid;

pub const CSRF_HEADER: &str = "x-csrf-token";

const COLLECTOR_ACCOUNT_USERNAME: &str = "collector-service";

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CollectorCredentialFile {
    base_url: String,
    origin: String,
    username: String,
    password: String,
}

#[derive(Debug, Clone)]
pub struct AuthConfig {
    pub cookie_name: String,
    pub cookie_secure: bool,
    pub public_origin: Option<String>,
    pub bootstrap_token: Option<String>,
    pub bootstrap_token_file: Option<String>,
    pub absolute_session_ttl: Duration,
    pub idle_session_ttl: Duration,
    pub max_sessions_per_user: i64,
    pub argon2_memory_kib: u32,
    pub argon2_iterations: u32,
    pub argon2_parallelism: u32,
    pub password_params_version: i16,
}

impl AuthConfig {
    pub fn from_env() -> common::Result<Self> {
        let env_name = env::var("APP_ENV").unwrap_or_else(|_| "development".to_string());
        let is_production = env_name.eq_ignore_ascii_case("production");
        let cookie_secure = env::var("AUTH_COOKIE_SECURE")
            .ok()
            .map(|value| matches!(value.as_str(), "true" | "1" | "yes"))
            .unwrap_or(is_production);
        let public_origin = env::var("PUBLIC_ORIGIN")
            .ok()
            .filter(|value| !value.is_empty());

        if is_production {
            if !cookie_secure {
                return Err(common::AppError::InvalidConfig(
                    "AUTH_COOKIE_SECURE must be true in production".into(),
                ));
            }
            if !public_origin
                .as_deref()
                .is_some_and(|origin| origin.starts_with("https://"))
            {
                return Err(common::AppError::InvalidConfig(
                    "PUBLIC_ORIGIN must be HTTPS in production".into(),
                ));
            }
            if env::var("BOOTSTRAP_TOKEN").is_ok() {
                return Err(common::AppError::InvalidConfig(
                    "BOOTSTRAP_TOKEN env var is not allowed in production; use BOOTSTRAP_TOKEN_FILE"
                        .into(),
                ));
            }
        }

        let bootstrap_token_file = env::var("BOOTSTRAP_TOKEN_FILE")
            .ok()
            .filter(|value| !value.is_empty());
        let bootstrap_token = bootstrap_token_file
            .as_deref()
            .and_then(|path| fs::read_to_string(path).ok())
            .map(|value| value.trim().to_string())
            .filter(|value| !value.is_empty())
            .or_else(|| env::var("BOOTSTRAP_TOKEN").ok());

        let cookie_name = if cookie_secure {
            "__Host-futures_session"
        } else {
            "futures_session"
        }
        .to_string();

        Ok(Self {
            cookie_name,
            cookie_secure,
            public_origin,
            bootstrap_token,
            bootstrap_token_file,
            absolute_session_ttl: Duration::days(7),
            idle_session_ttl: Duration::hours(4),
            max_sessions_per_user: 5,
            argon2_memory_kib: 64 * 1024,
            argon2_iterations: 3,
            argon2_parallelism: 1,
            password_params_version: 1,
        })
    }

    fn argon2(&self) -> Result<Argon2<'static>, AuthError> {
        let params = Params::new(
            self.argon2_memory_kib,
            self.argon2_iterations,
            self.argon2_parallelism,
            None,
        )
        .map_err(|_| AuthError::Internal)?;
        Ok(Argon2::new(Algorithm::Argon2id, Version::V0x13, params))
    }
}

pub async fn provision_collector_account(pool: &PgPool, config: &AuthConfig) -> anyhow::Result<()> {
    let path = env::var("COLLECTOR_CREDENTIALS_FILE")
        .unwrap_or_else(|_| "/run/secrets/collector-credentials".to_string());
    let raw = fs::read_to_string(&path)?;
    let credential: CollectorCredentialFile = serde_json::from_str(&raw)?;
    if credential.username != COLLECTOR_ACCOUNT_USERNAME
        || credential.base_url.trim().is_empty()
        || credential.origin.trim().is_empty()
    {
        anyhow::bail!("collector credential metadata is invalid");
    }
    validate_password(&credential.password)
        .map_err(|_| anyhow::anyhow!("collector credential password violates policy"))?;

    let mut tx = pool.begin().await?;
    let workspaces = sqlx::query_scalar::<_, Uuid>(
        "select w.id
           from workspaces w
           join users u on u.id = w.owner_user_id
           join user_roles ur on ur.user_id = u.id and ur.role_name = 'admin'
          where u.disabled_at is null
          order by w.created_at",
    )
    .fetch_all(&mut *tx)
    .await?;
    let [workspace_id] = workspaces.as_slice() else {
        anyhow::bail!("collector provisioning requires exactly one enabled admin-owned workspace");
    };
    let existing = sqlx::query(
        "select id, password_hash, disabled_at from users where username_normalized = $1 for update",
    )
    .bind(COLLECTOR_ACCOUNT_USERNAME)
    .fetch_optional(&mut *tx)
    .await?;
    let user_id = if let Some(row) = existing {
        // 停用的账号不许在这里悄悄复活——那是管理决定，不是采集配置。
        if row
            .get::<Option<OffsetDateTime>, _>("disabled_at")
            .is_some()
        {
            anyhow::bail!("existing collector account is disabled");
        }
        let user_id: Uuid = row.get("id");
        // 文件密码与库不一致：**把库改成文件的**，这就是轮换。
        //
        // 原来这里直接报错。听着稳妥，实际上把轮换堵死了：凭据文件本来就是采集器
        // 登录用的唯一事实源（root:root 0400，只有部署流程写它），文件和库不一致时
        // 采集器必然已经登不进去了，收敛库向文件正是修复。2026-08-12 凭据在聊天里
        // 泄过，轮换流程就是「换掉文件 → 重跑本命令」，报错版连这条路都没有。
        if !verify_password(config, &credential.password, row.get("password_hash"))
            .map_err(|_| anyhow::anyhow!("collector account verification failed"))?
        {
            let password_hash = hash_password(config, &credential.password)
                .map_err(|_| anyhow::anyhow!("collector password hashing failed"))?;
            sqlx::query(
                "update users
                    set password_hash = $2, password_params_version = $3
                  where id = $1",
            )
            .bind(user_id)
            .bind(password_hash)
            .bind(config.password_params_version)
            .execute(&mut *tx)
            .await?;
            // 旧密码开出的会话一并作废。采集器每轮自己登录，不靠长会话，
            // 这里撤销掉只影响拿旧凭据的人。
            //
            // 是 revoke 不是 delete：futures_runtime 对 sessions 只有
            // select/insert/update（迁移 202607240002 有意为之——会话史是审计素材），
            // 全库也无一处 delete 会话。第一版写了 delete，生产轮换当场
            // permission denied，事务回滚，文件已换库未换，采集器登不进去。
            sqlx::query(
                "update sessions
                    set revoked_at = now(), revoke_reason = 'password_rotated'
                  where user_id = $1 and revoked_at is null",
            )
            .bind(user_id)
            .execute(&mut *tx)
            .await?;
        }
        user_id
    } else {
        let user_id = Uuid::now_v7();
        let password_hash = hash_password(config, &credential.password)
            .map_err(|_| anyhow::anyhow!("collector password hashing failed"))?;
        sqlx::query(
            "insert into users
                (id, username, username_normalized, password_hash, password_params_version)
             values ($1, $2, $2, $3, $4)",
        )
        .bind(user_id)
        .bind(COLLECTOR_ACCOUNT_USERNAME)
        .bind(password_hash)
        .bind(config.password_params_version)
        .execute(&mut *tx)
        .await?;
        user_id
    };

    sqlx::query(
        "insert into workspace_memberships (id, workspace_id, user_id, role)
         values ($1, $2, $3, 'owner')
         on conflict (user_id) do nothing",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(user_id)
    .execute(&mut *tx)
    .await?;
    let membership_workspace = sqlx::query_scalar::<_, Uuid>(
        "select workspace_id from workspace_memberships where user_id = $1",
    )
    .bind(user_id)
    .fetch_one(&mut *tx)
    .await?;
    if membership_workspace != *workspace_id {
        anyhow::bail!("collector account belongs to a different workspace");
    }
    sqlx::query(
        "insert into user_roles (user_id, role_name) values ($1, 'analyst')
         on conflict (user_id, role_name) do nothing",
    )
    .bind(user_id)
    .execute(&mut *tx)
    .await?;
    let roles = sqlx::query_scalar::<_, String>(
        "select role_name from user_roles where user_id = $1 order by role_name",
    )
    .bind(user_id)
    .fetch_all(&mut *tx)
    .await?;
    if roles != ["analyst"] {
        anyhow::bail!("collector account must have only the analyst role");
    }
    set_workspace(&mut tx, *workspace_id)
        .await
        .map_err(|_| anyhow::anyhow!("collector workspace context failed"))?;
    insert_audit(
        &mut tx,
        *workspace_id,
        Some(user_id),
        "auth.collector_account_provisioned",
        "success",
        Uuid::now_v7(),
    )
    .await
    .map_err(|_| anyhow::anyhow!("collector provisioning audit failed"))?;
    tx.commit().await?;
    Ok(())
}

#[derive(Debug, Default)]
pub struct LoginLimiter {
    attempts: Mutex<HashMap<String, Vec<OffsetDateTime>>>,
}

impl LoginLimiter {
    fn record_and_check(&self, key: &str) -> Result<(), AuthError> {
        let now = OffsetDateTime::now_utc();
        let window_start = now - Duration::minutes(15);
        let mut attempts = self.attempts.lock().map_err(|_| AuthError::Internal)?;
        let bucket = attempts.entry(key.to_string()).or_default();
        bucket.retain(|seen| *seen >= window_start);
        if bucket.len() >= 10 {
            return Err(AuthError::RateLimited);
        }
        bucket.push(now);
        Ok(())
    }
}

#[derive(Debug, Clone)]
pub struct AuthState {
    pub pool: PgPool,
    pub version: VersionInfo,
    pub config: AuthConfig,
    pub limiter: Arc<LoginLimiter>,
}

#[derive(Debug, Deserialize, ToSchema)]
pub struct BootstrapRequest {
    pub username: String,
    pub password: String,
}

#[derive(Debug, Deserialize, ToSchema)]
pub struct LoginRequest {
    pub username: String,
    pub password: String,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct UserSummary {
    pub id: Uuid,
    pub username: String,
    pub roles: Vec<String>,
    pub permissions: Vec<String>,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct WorkspaceSummary {
    pub id: Uuid,
    pub name: String,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct MeResponse {
    pub user: UserSummary,
    pub workspace: WorkspaceSummary,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct CsrfResponse {
    pub csrf_token: String,
    #[serde(with = "time::serde::rfc3339")]
    #[schema(value_type = String)]
    pub expires_at: OffsetDateTime,
}

#[derive(Debug, Serialize, ToSchema)]
pub struct SessionSummary {
    pub id: Uuid,
    pub current: bool,
    #[serde(with = "time::serde::rfc3339")]
    #[schema(value_type = String)]
    pub created_at: OffsetDateTime,
    #[serde(with = "time::serde::rfc3339")]
    #[schema(value_type = String)]
    pub last_seen_at: OffsetDateTime,
    #[serde(with = "time::serde::rfc3339")]
    #[schema(value_type = String)]
    pub absolute_expires_at: OffsetDateTime,
    #[serde(with = "time::serde::rfc3339")]
    #[schema(value_type = String)]
    pub idle_expires_at: OffsetDateTime,
    pub user_agent: Option<String>,
}

#[derive(Debug, Deserialize, ToSchema)]
pub struct SessionsQuery {
    /// Optional identity-control-plane filter. Only admins may query another user's sessions.
    pub user_id: Option<Uuid>,
}

#[derive(Debug, Serialize, ToSchema)]
pub(crate) struct ErrorBody {
    code: &'static str,
    message: &'static str,
}

#[derive(Debug)]
pub(crate) enum AuthError {
    BadRequest(&'static str),
    Unauthorized,
    Forbidden(&'static str),
    Conflict(&'static str),
    RateLimited,
    Internal,
}

impl AuthError {
    pub(crate) fn code(&self) -> &'static str {
        match self {
            Self::BadRequest(code) | Self::Forbidden(code) | Self::Conflict(code) => code,
            Self::Unauthorized => "auth_required",
            Self::RateLimited => "rate_limited",
            Self::Internal => "internal_error",
        }
    }

    pub(crate) fn status(&self) -> StatusCode {
        match self {
            Self::BadRequest(_) => StatusCode::BAD_REQUEST,
            Self::Unauthorized => StatusCode::UNAUTHORIZED,
            Self::Forbidden(_) => StatusCode::FORBIDDEN,
            Self::Conflict(_) => StatusCode::CONFLICT,
            Self::RateLimited => StatusCode::TOO_MANY_REQUESTS,
            Self::Internal => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    pub(crate) fn message(&self) -> &'static str {
        match self {
            Self::BadRequest(_) => "request is invalid",
            Self::Unauthorized => "authentication required",
            Self::Forbidden(_) => "request is not allowed",
            Self::Conflict(_) => "request conflicts with current state",
            Self::RateLimited => "too many attempts",
            Self::Internal => "internal error",
        }
    }
}

impl IntoResponse for AuthError {
    fn into_response(self) -> Response {
        let request_id = Uuid::now_v7();
        let body = ErrorBody {
            code: self.code(),
            message: self.message(),
        };
        (self.status(), Json(ApiResponse::new(body, request_id))).into_response()
    }
}

#[derive(Debug, Clone)]
pub(crate) struct AuthContext {
    session_id: Uuid,
    user_id: Uuid,
    username: String,
    workspace_id: Uuid,
    workspace_name: String,
    roles: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Permission {
    ReadImports,
    Upload,
    Rollback,
    Compensate,
    GovernObjects,
    ReadSpreads,
    ManageSpreadFavorites,
}

impl Permission {
    const fn as_str(self) -> &'static str {
        match self {
            Self::ReadImports => "import.read",
            Self::Upload => "import.upload",
            Self::Rollback => "import.rollback",
            Self::Compensate => "import.compensate",
            Self::GovernObjects => "object.govern",
            Self::ReadSpreads => "spread.read",
            Self::ManageSpreadFavorites => "spread.favorite.manage",
        }
    }
}

impl AuthContext {
    pub(crate) fn session_id(&self) -> Uuid {
        self.session_id
    }

    pub(crate) fn user_id(&self) -> Uuid {
        self.user_id
    }

    pub(crate) fn workspace_id(&self) -> Uuid {
        self.workspace_id
    }

    pub(crate) fn is_collector_account(&self) -> bool {
        self.username == COLLECTOR_ACCOUNT_USERNAME && self.roles == ["analyst"]
    }

    pub(crate) fn require_permission(&self, permission: Permission) -> Result<(), AuthError> {
        if roles_allow_permission(&self.roles, permission) {
            Ok(())
        } else {
            Err(AuthError::Forbidden("permission_denied"))
        }
    }
}

pub fn permissions_for_roles(roles: &[String]) -> Vec<String> {
    let mut permissions = vec![
        "identity.self.read".to_string(),
        "session.self.read".to_string(),
        "session.self.revoke".to_string(),
        "workspace.self.read".to_string(),
    ];
    if roles.iter().any(|role| role == "admin") {
        permissions.push("session.any.read".to_string());
        permissions.push("session.any.revoke".to_string());
    }
    for permission in [
        Permission::ReadImports,
        Permission::Upload,
        Permission::Rollback,
        Permission::Compensate,
        Permission::GovernObjects,
        Permission::ReadSpreads,
        Permission::ManageSpreadFavorites,
    ] {
        if roles_allow_permission(roles, permission) {
            permissions.push(permission.as_str().to_string());
        }
    }
    permissions.sort();
    permissions.dedup();
    permissions
}

fn roles_allow_permission(roles: &[String], permission: Permission) -> bool {
    match permission {
        Permission::ReadImports => roles
            .iter()
            .any(|role| matches!(role.as_str(), "admin" | "analyst" | "viewer")),
        Permission::Upload => roles
            .iter()
            .any(|role| matches!(role.as_str(), "admin" | "analyst")),
        Permission::Rollback => roles
            .iter()
            .any(|role| matches!(role.as_str(), "admin" | "analyst")),
        Permission::Compensate => roles
            .iter()
            .any(|role| matches!(role.as_str(), "admin" | "analyst")),
        Permission::GovernObjects => roles.iter().any(|role| role == "admin"),
        Permission::ReadSpreads => roles
            .iter()
            .any(|role| matches!(role.as_str(), "admin" | "analyst" | "viewer")),
        Permission::ManageSpreadFavorites => roles
            .iter()
            .any(|role| matches!(role.as_str(), "admin" | "analyst")),
    }
}

#[utoipa::path(
    post,
    path = "/api/v1/auth/bootstrap",
    params(
        ("x-bootstrap-token" = String, Header, description = "One-time bootstrap token provided by deployment secret storage"),
        ("Origin" = String, Header, description = "Expected public origin for state-changing browser requests")
    ),
    request_body = BootstrapRequest,
    responses(
        (status = 200, body = MeResponse),
        (status = 400, body = ErrorBody),
        (status = 403, body = ErrorBody),
        (status = 409, body = ErrorBody)
    )
)]
pub async fn bootstrap(
    State(state): State<Arc<AuthState>>,
    headers: HeaderMap,
    Json(payload): Json<BootstrapRequest>,
) -> Result<Response, AuthError> {
    ensure_allowed_origin(&state.config, &headers)?;
    let expected = state
        .config
        .bootstrap_token
        .as_deref()
        .ok_or(AuthError::Forbidden("bootstrap_unavailable"))?;
    let supplied = headers
        .get("x-bootstrap-token")
        .and_then(|value| value.to_str().ok())
        .ok_or(AuthError::Forbidden("bootstrap_token_invalid"))?;
    let username_normalized = normalize_username(&payload.username)?;
    if !constant_time_eq(expected.as_bytes(), supplied.as_bytes()) {
        insert_security_event(
            &state.pool,
            None,
            "auth.bootstrap",
            "denied",
            Some(&username_normalized),
            Uuid::now_v7(),
        )
        .await;
        return Err(AuthError::Forbidden("bootstrap_token_invalid"));
    }
    validate_password(&payload.password)?;

    let password_hash = hash_password(&state.config, &payload.password)?;
    let now = OffsetDateTime::now_utc();
    let user_id = Uuid::now_v7();
    let workspace_id = Uuid::now_v7();
    let membership_id = Uuid::now_v7();
    let session = new_session_material();
    let request_id = Uuid::now_v7();

    let mut tx = state.pool.begin().await.map_err(|_| AuthError::Internal)?;
    let bootstrap_row =
        sqlx::query("select value from system_settings where key = 'bootstrap' for update")
            .fetch_optional(&mut *tx)
            .await
            .map_err(|_| AuthError::Internal)?;
    let Some(row) = bootstrap_row else {
        return Err(AuthError::Conflict("bootstrap_state_missing"));
    };
    let bootstrap_value: serde_json::Value = row.get("value");
    if bootstrap_value
        .get("completed")
        .and_then(serde_json::Value::as_bool)
        .unwrap_or(false)
    {
        return Err(AuthError::Conflict("bootstrap_closed"));
    }
    let user_count: i64 = sqlx::query_scalar("select count(*) from users")
        .fetch_one(&mut *tx)
        .await
        .map_err(|_| AuthError::Internal)?;
    if user_count != 0 {
        return Err(AuthError::Conflict("bootstrap_closed"));
    }

    sqlx::query(
        "insert into users (id, username, username_normalized, password_hash, password_params_version, created_at, updated_at, last_login_at)
         values ($1, $2, $3, $4, $5, $6, $6, $6)",
    )
    .bind(user_id)
    .bind(payload.username.trim())
    .bind(&username_normalized)
    .bind(&password_hash)
    .bind(state.config.password_params_version)
    .bind(now)
    .execute(&mut *tx)
    .await
    .map_err(|_| AuthError::Internal)?;

    let workspace_name = format!("{} 的个人 Workspace", payload.username.trim());
    sqlx::query(
        "insert into workspaces (id, name, owner_user_id, created_at, updated_at)
         values ($1, $2, $3, $4, $4)",
    )
    .bind(workspace_id)
    .bind(&workspace_name)
    .bind(user_id)
    .bind(now)
    .execute(&mut *tx)
    .await
    .map_err(|_| AuthError::Internal)?;

    sqlx::query(
        "insert into workspace_memberships (id, workspace_id, user_id, role, created_at)
         values ($1, $2, $3, 'owner', $4)",
    )
    .bind(membership_id)
    .bind(workspace_id)
    .bind(user_id)
    .bind(now)
    .execute(&mut *tx)
    .await
    .map_err(|_| AuthError::Internal)?;

    sqlx::query("insert into user_roles (user_id, role_name, created_at) values ($1, 'admin', $2)")
        .bind(user_id)
        .bind(now)
        .execute(&mut *tx)
        .await
        .map_err(|_| AuthError::Internal)?;

    insert_session(&mut tx, user_id, &session, &state.config, &headers, now).await?;
    set_workspace(&mut tx, workspace_id).await?;
    insert_audit(
        &mut tx,
        workspace_id,
        Some(user_id),
        "auth.bootstrap",
        "success",
        request_id,
    )
    .await?;
    sqlx::query(
        "update system_settings
         set value = '{\"completed\": true}'::jsonb, locked_at = coalesce(locked_at, now()), updated_at = now()
         where key = 'bootstrap'",
    )
    .execute(&mut *tx)
    .await
    .map_err(|_| AuthError::Internal)?;
    tx.commit().await.map_err(|_| AuthError::Internal)?;
    if let Some(path) = state.config.bootstrap_token_file.as_deref() {
        let _ = fs::remove_file(path);
    }

    let roles = vec!["admin".to_string()];
    let me = MeResponse {
        user: UserSummary {
            id: user_id,
            username: payload.username.trim().to_string(),
            permissions: permissions_for_roles(&roles),
            roles,
        },
        workspace: WorkspaceSummary {
            id: workspace_id,
            name: workspace_name,
        },
    };
    Ok(with_session_cookie(
        &state.config,
        &session.token,
        Json(ApiResponse::new(me, request_id)),
    ))
}

#[utoipa::path(
    post,
    path = "/api/v1/auth/login",
    params(
        ("Origin" = String, Header, description = "Expected public origin for state-changing browser requests")
    ),
    request_body = LoginRequest,
    responses(
        (status = 200, body = MeResponse),
        (status = 400, body = ErrorBody),
        (status = 401, body = ErrorBody),
        (status = 403, body = ErrorBody),
        (status = 429, body = ErrorBody)
    )
)]
pub async fn login(
    State(state): State<Arc<AuthState>>,
    headers: HeaderMap,
    Json(payload): Json<LoginRequest>,
) -> Result<Response, AuthError> {
    ensure_allowed_origin(&state.config, &headers)?;
    let username_normalized = normalize_username(&payload.username)?;
    match state.limiter.record_and_check(&username_normalized) {
        Ok(()) => {}
        Err(err @ AuthError::RateLimited) => {
            insert_security_event(
                &state.pool,
                None,
                "auth.login",
                "denied",
                Some(&username_normalized),
                Uuid::now_v7(),
            )
            .await;
            return Err(err);
        }
        Err(err) => return Err(err),
    }

    let user_row = sqlx::query(
        "select id, username, password_hash, password_params_version from users
         where username_normalized = $1 and disabled_at is null",
    )
    .bind(&username_normalized)
    .fetch_optional(&state.pool)
    .await
    .map_err(|_| AuthError::Internal)?;

    let Some(row) = user_row else {
        burn_password_time(&state.config, &payload.password)?;
        insert_security_event(
            &state.pool,
            None,
            "auth.login",
            "failure",
            Some(&username_normalized),
            Uuid::now_v7(),
        )
        .await;
        return Err(AuthError::Unauthorized);
    };
    let user_id: Uuid = row.get("id");
    let username: String = row.get("username");
    let password_hash: String = row.get("password_hash");
    let params_version: i16 = row.get("password_params_version");
    if !verify_password(&state.config, &payload.password, &password_hash)? {
        insert_security_event(
            &state.pool,
            Some(user_id),
            "auth.login",
            "failure",
            Some(&username_normalized),
            Uuid::now_v7(),
        )
        .await;
        return Err(AuthError::Unauthorized);
    }

    let workspace = workspace_for_user(&state.pool, user_id).await?;
    let roles = roles_for_user(&state.pool, user_id).await?;
    let session = new_session_material();
    let now = OffsetDateTime::now_utc();
    let request_id = Uuid::now_v7();
    let old_session_id = session_id_from_cookie(&state.pool, &state.config, &headers).await;

    let mut tx = state.pool.begin().await.map_err(|_| AuthError::Internal)?;
    if params_version < state.config.password_params_version {
        let upgraded = hash_password(&state.config, &payload.password)?;
        sqlx::query(
            "update users
             set password_hash = $1, password_params_version = $2, password_rehash_required = false, updated_at = now()
             where id = $3",
        )
        .bind(upgraded)
        .bind(state.config.password_params_version)
        .bind(user_id)
        .execute(&mut *tx)
        .await
        .map_err(|_| AuthError::Internal)?;
    }
    if let Some(old_id) = old_session_id {
        sqlx::query("update sessions set revoked_at = now(), revoke_reason = 'login_rotated' where id = $1 and user_id = $2 and revoked_at is null")
            .bind(old_id)
            .bind(user_id)
            .execute(&mut *tx)
            .await
            .map_err(|_| AuthError::Internal)?;
    }
    insert_session(&mut tx, user_id, &session, &state.config, &headers, now).await?;
    enforce_session_limit(&mut tx, user_id, state.config.max_sessions_per_user).await?;
    sqlx::query("update users set last_login_at = now(), updated_at = now() where id = $1")
        .bind(user_id)
        .execute(&mut *tx)
        .await
        .map_err(|_| AuthError::Internal)?;
    set_workspace(&mut tx, workspace.id).await?;
    insert_audit(
        &mut tx,
        workspace.id,
        Some(user_id),
        "auth.login",
        "success",
        request_id,
    )
    .await?;
    tx.commit().await.map_err(|_| AuthError::Internal)?;

    let me = MeResponse {
        user: UserSummary {
            id: user_id,
            username,
            permissions: permissions_for_roles(&roles),
            roles,
        },
        workspace,
    };
    Ok(with_session_cookie(
        &state.config,
        &session.token,
        Json(ApiResponse::new(me, request_id)),
    ))
}

#[utoipa::path(
    post,
    path = "/api/v1/auth/logout",
    params(
        ("x-csrf-token" = String, Header, description = "Session-bound CSRF token from /api/v1/auth/csrf"),
        ("Origin" = String, Header, description = "Expected public origin for state-changing browser requests")
    ),
    security(("session_cookie" = [])),
    responses((status = 200), (status = 401, body = ErrorBody), (status = 403, body = ErrorBody))
)]
pub async fn logout(
    State(state): State<Arc<AuthState>>,
    headers: HeaderMap,
) -> Result<Response, AuthError> {
    ensure_allowed_origin(&state.config, &headers)?;
    ensure_csrf(&state, &headers).await?;
    let context = current_context(&state, &headers).await?;
    let request_id = Uuid::now_v7();
    let mut tx = state.pool.begin().await.map_err(|_| AuthError::Internal)?;
    sqlx::query("update sessions set revoked_at = now(), revoke_reason = 'logout' where id = $1")
        .bind(context.session_id)
        .execute(&mut *tx)
        .await
        .map_err(|_| AuthError::Internal)?;
    set_workspace(&mut tx, context.workspace_id).await?;
    insert_audit(
        &mut tx,
        context.workspace_id,
        Some(context.user_id),
        "auth.logout",
        "success",
        request_id,
    )
    .await?;
    tx.commit().await.map_err(|_| AuthError::Internal)?;
    Ok(with_clear_cookie(
        &state.config,
        Json(ApiResponse::new(
            serde_json::json!({"ok": true}),
            request_id,
        )),
    ))
}

#[utoipa::path(
    get,
    path = "/api/v1/auth/me",
    security(("session_cookie" = [])),
    responses((status = 200, body = MeResponse), (status = 401, body = ErrorBody))
)]
pub async fn me(
    State(state): State<Arc<AuthState>>,
    headers: HeaderMap,
) -> Result<Response, AuthError> {
    let context = current_context(&state, &headers).await?;
    let roles = context.roles.clone();
    let me = MeResponse {
        user: UserSummary {
            id: context.user_id,
            username: context.username,
            permissions: permissions_for_roles(&roles),
            roles,
        },
        workspace: WorkspaceSummary {
            id: context.workspace_id,
            name: context.workspace_name,
        },
    };
    Ok(Json(ApiResponse::new(me, Uuid::now_v7())).into_response())
}

#[utoipa::path(
    get,
    path = "/api/v1/auth/csrf",
    security(("session_cookie" = [])),
    responses((status = 200, body = CsrfResponse), (status = 401, body = ErrorBody))
)]
pub async fn csrf(
    State(state): State<Arc<AuthState>>,
    headers: HeaderMap,
) -> Result<Response, AuthError> {
    let context = current_context(&state, &headers).await?;
    let token = random_token();
    let token_hash = digest_token(&token);
    sqlx::query("update sessions set csrf_hash = $1, last_seen_at = now() where id = $2 and revoked_at is null")
        .bind(token_hash)
        .bind(context.session_id)
        .execute(&state.pool)
        .await
        .map_err(|_| AuthError::Internal)?;
    Ok(Json(ApiResponse::new(
        CsrfResponse {
            csrf_token: token,
            expires_at: OffsetDateTime::now_utc() + state.config.idle_session_ttl,
        },
        Uuid::now_v7(),
    ))
    .into_response())
}

#[utoipa::path(
    get,
    path = "/api/v1/workspace",
    security(("session_cookie" = [])),
    responses((status = 200, body = WorkspaceSummary), (status = 401, body = ErrorBody))
)]
pub async fn workspace(
    State(state): State<Arc<AuthState>>,
    headers: HeaderMap,
) -> Result<Response, AuthError> {
    let context = current_context(&state, &headers).await?;
    Ok(Json(ApiResponse::new(
        WorkspaceSummary {
            id: context.workspace_id,
            name: context.workspace_name,
        },
        Uuid::now_v7(),
    ))
    .into_response())
}

#[utoipa::path(
    get,
    path = "/api/v1/sessions",
    params(
        ("user_id" = Option<Uuid>, Query, description = "Optional admin-only user id filter for identity control plane session metadata")
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 200, body = Vec<SessionSummary>),
        (status = 401, body = ErrorBody),
        (status = 403, body = ErrorBody)
    )
)]
pub async fn sessions(
    State(state): State<Arc<AuthState>>,
    Query(query): Query<SessionsQuery>,
    headers: HeaderMap,
) -> Result<Response, AuthError> {
    let context = current_context(&state, &headers).await?;
    let target_user_id = query.user_id.unwrap_or(context.user_id);
    if target_user_id != context.user_id && !context.roles.iter().any(|role| role == "admin") {
        return Err(AuthError::Forbidden("session_not_visible"));
    }
    let rows = sqlx::query(
        "select id, created_at, last_seen_at, absolute_expires_at, idle_expires_at, user_agent
         from sessions
         where user_id = $1 and revoked_at is null
         order by created_at desc",
    )
    .bind(target_user_id)
    .fetch_all(&state.pool)
    .await
    .map_err(|_| AuthError::Internal)?;
    let sessions = rows
        .into_iter()
        .map(|row| SessionSummary {
            id: row.get("id"),
            current: row.get::<Uuid, _>("id") == context.session_id,
            created_at: row.get("created_at"),
            last_seen_at: row.get("last_seen_at"),
            absolute_expires_at: row.get("absolute_expires_at"),
            idle_expires_at: row.get("idle_expires_at"),
            user_agent: row.get("user_agent"),
        })
        .collect::<Vec<_>>();
    Ok(Json(ApiResponse::new(sessions, Uuid::now_v7())).into_response())
}

/// 改密码的请求体。旧密码是必须的：会话被偷时，攻击者手里有 cookie 却没有旧密码，
/// 不该能把账号锁走。
#[derive(Debug, Deserialize, ToSchema)]
pub struct ChangePasswordRequest {
    pub current_password: String,
    pub new_password: String,
}

#[utoipa::path(
    post,
    path = "/api/v1/auth/password",
    request_body = ChangePasswordRequest,
    params(
        ("x-csrf-token" = String, Header, description = "Session-bound CSRF token from /api/v1/auth/csrf"),
        ("Origin" = String, Header, description = "Expected public origin for state-changing browser requests")
    ),
    security(("session_cookie" = [])),
    responses(
        (status = 200),
        (status = 400, body = ErrorBody),
        (status = 401, body = ErrorBody),
        (status = 403, body = ErrorBody)
    )
)]
pub async fn change_password(
    State(state): State<Arc<AuthState>>,
    headers: HeaderMap,
    Json(payload): Json<ChangePasswordRequest>,
) -> Result<Response, AuthError> {
    ensure_allowed_origin(&state.config, &headers)?;
    ensure_csrf(&state, &headers).await?;
    let context = current_context(&state, &headers).await?;
    let request_id = Uuid::now_v7();

    validate_password(&payload.new_password)?;
    if payload.new_password == payload.current_password {
        return Err(AuthError::BadRequest("password_unchanged"));
    }

    let mut tx = state.pool.begin().await.map_err(|_| AuthError::Internal)?;
    // for update：同一账号并发改两次密码时，后一次必须看到前一次的结果，
    // 否则两边都拿旧哈希校验通过，最后写入的那次静默覆盖另一次。
    let row = sqlx::query("select password_hash from users where id = $1 for update")
        .bind(context.user_id)
        .fetch_optional(&mut *tx)
        .await
        .map_err(|_| AuthError::Internal)?
        .ok_or(AuthError::Internal)?;

    if !verify_password(
        &state.config,
        &payload.current_password,
        row.get("password_hash"),
    )
    .map_err(|_| AuthError::Internal)?
    {
        // 审计要记下失败：连续的失败尝试是「会话被人拿走了」的最早信号。
        set_workspace(&mut tx, context.workspace_id).await?;
        insert_audit(
            &mut tx,
            context.workspace_id,
            Some(context.user_id),
            "auth.password_changed",
            "denied",
            request_id,
        )
        .await?;
        tx.commit().await.map_err(|_| AuthError::Internal)?;
        return Err(AuthError::Forbidden("current_password_invalid"));
    }

    let password_hash = hash_password(&state.config, &payload.new_password)?;
    sqlx::query(
        "update users set password_hash = $2, password_params_version = $3, updated_at = now()
          where id = $1",
    )
    .bind(context.user_id)
    .bind(password_hash)
    .bind(state.config.password_params_version)
    .execute(&mut *tx)
    .await
    .map_err(|_| AuthError::Internal)?;

    // 别的设备上的会话全部作废，当前这台留着——改完密码不该把自己也踢下线。
    // 是 revoke 不是 delete：futures_runtime 对 sessions 只有 select/insert/update
    // （会话史是审计素材），写 delete 会在生产上撞 permission denied。
    let revoked = sqlx::query(
        "update sessions
            set revoked_at = now(), revoke_reason = 'password_changed'
          where user_id = $1 and id <> $2 and revoked_at is null",
    )
    .bind(context.user_id)
    .bind(context.session_id)
    .execute(&mut *tx)
    .await
    .map_err(|_| AuthError::Internal)?
    .rows_affected();

    set_workspace(&mut tx, context.workspace_id).await?;
    insert_audit(
        &mut tx,
        context.workspace_id,
        Some(context.user_id),
        "auth.password_changed",
        "success",
        request_id,
    )
    .await?;
    tx.commit().await.map_err(|_| AuthError::Internal)?;

    Ok(Json(ApiResponse::new(
        serde_json::json!({"ok": true, "revoked_sessions": revoked}),
        request_id,
    ))
    .into_response())
}

#[utoipa::path(
    delete,
    path = "/api/v1/sessions/{session_id}",
    params(
        ("session_id" = Uuid, Path, description = "Session id to revoke"),
        ("x-csrf-token" = String, Header, description = "Session-bound CSRF token from /api/v1/auth/csrf"),
        ("Origin" = String, Header, description = "Expected public origin for state-changing browser requests")
    ),
    security(("session_cookie" = [])),
    responses((status = 200), (status = 401, body = ErrorBody), (status = 403, body = ErrorBody))
)]
pub async fn revoke_session(
    State(state): State<Arc<AuthState>>,
    Path(session_id): Path<Uuid>,
    headers: HeaderMap,
) -> Result<Response, AuthError> {
    ensure_allowed_origin(&state.config, &headers)?;
    ensure_csrf(&state, &headers).await?;
    let context = current_context(&state, &headers).await?;
    let request_id = Uuid::now_v7();
    let is_admin = context.roles.iter().any(|role| role == "admin");
    let affected = sqlx::query(
        "update sessions
         set revoked_at = now(), revoke_reason = 'user_revoked'
         where id = $1
           and revoked_at is null
           and (user_id = $2 or $3)",
    )
    .bind(session_id)
    .bind(context.user_id)
    .bind(is_admin)
    .execute(&state.pool)
    .await
    .map_err(|_| AuthError::Internal)?
    .rows_affected();
    if affected == 0 {
        return Err(AuthError::Forbidden("session_not_visible"));
    }
    Ok(Json(ApiResponse::new(
        serde_json::json!({"ok": true}),
        request_id,
    ))
    .into_response())
}

fn normalize_username(username: &str) -> Result<String, AuthError> {
    let normalized = username.trim().to_lowercase();
    if normalized.len() < 3 || normalized.len() > 64 {
        return Err(AuthError::BadRequest("username_policy"));
    }
    Ok(normalized)
}

fn validate_password(password: &str) -> Result<(), AuthError> {
    let length = password.chars().count();
    if !(15..=128).contains(&length) {
        return Err(AuthError::BadRequest("password_policy"));
    }
    let normalized = password.trim().to_lowercase();
    let common = [
        "password",
        "123456789012345",
        "qwertyuiopasdfg",
        "letmeinletmeinlet",
        "correcthorsebatterystaple",
        "change-this-local-bootstrap-token",
    ];
    if common.iter().any(|item| normalized.contains(item)) {
        return Err(AuthError::BadRequest("password_common"));
    }
    Ok(())
}

fn hash_password(config: &AuthConfig, password: &str) -> Result<String, AuthError> {
    let salt = SaltString::generate(&mut OsRng);
    Ok(config
        .argon2()?
        .hash_password(password.as_bytes(), &salt)
        .map_err(|_| AuthError::Internal)?
        .to_string())
}

fn verify_password(config: &AuthConfig, password: &str, hash: &str) -> Result<bool, AuthError> {
    let parsed = PasswordHash::new(hash).map_err(|_| AuthError::Internal)?;
    Ok(config
        .argon2()?
        .verify_password(password.as_bytes(), &parsed)
        .is_ok())
}

fn burn_password_time(config: &AuthConfig, password: &str) -> Result<(), AuthError> {
    let _ = hash_password(config, password)?;
    Ok(())
}

fn random_token() -> String {
    let mut bytes = [0_u8; 32];
    OsRng.fill_bytes(&mut bytes);
    URL_SAFE_NO_PAD.encode(bytes)
}

fn digest_token(token: &str) -> String {
    URL_SAFE_NO_PAD.encode(Sha256::digest(token.as_bytes()))
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    left.ct_eq(right).into()
}

#[derive(Debug)]
struct SessionMaterial {
    id: Uuid,
    token: String,
    token_hash: String,
}

fn new_session_material() -> SessionMaterial {
    let token = random_token();
    SessionMaterial {
        id: Uuid::now_v7(),
        token_hash: digest_token(&token),
        token,
    }
}

async fn insert_session(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    user_id: Uuid,
    session: &SessionMaterial,
    config: &AuthConfig,
    headers: &HeaderMap,
    now: OffsetDateTime,
) -> Result<(), AuthError> {
    let user_agent = headers
        .get(header::USER_AGENT)
        .and_then(|value| value.to_str().ok())
        .map(|value| value.chars().take(512).collect::<String>());
    let ip_address = headers
        .get("x-forwarded-for")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(',').next())
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(|value| value.chars().take(64).collect::<String>());
    sqlx::query(
        "insert into sessions
         (id, user_id, token_hash, created_at, last_seen_at, absolute_expires_at, idle_expires_at, user_agent, ip_address)
         values ($1, $2, $3, $4, $4, $5, $6, $7, $8)",
    )
    .bind(session.id)
    .bind(user_id)
    .bind(&session.token_hash)
    .bind(now)
    .bind(now + config.absolute_session_ttl)
    .bind(now + config.idle_session_ttl)
    .bind(user_agent)
    .bind(ip_address)
    .execute(&mut **tx)
    .await
    .map_err(|_| AuthError::Internal)?;
    Ok(())
}

async fn enforce_session_limit(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    user_id: Uuid,
    max_sessions: i64,
) -> Result<(), AuthError> {
    sqlx::query(
        "with ranked as (
             select id, row_number() over (order by created_at desc) as rn
             from sessions
             where user_id = $1 and revoked_at is null
         )
         update sessions
         set revoked_at = now(), revoke_reason = 'concurrency_limit'
         where id in (select id from ranked where rn > $2)",
    )
    .bind(user_id)
    .bind(max_sessions)
    .execute(&mut **tx)
    .await
    .map_err(|_| AuthError::Internal)?;
    Ok(())
}

async fn set_workspace(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
) -> Result<(), AuthError> {
    sqlx::query("select set_config('app.current_workspace_id', $1, true)")
        .bind(workspace_id.to_string())
        .execute(&mut **tx)
        .await
        .map_err(|_| AuthError::Internal)?;
    Ok(())
}

async fn insert_audit(
    tx: &mut sqlx::Transaction<'_, sqlx::Postgres>,
    workspace_id: Uuid,
    actor_user_id: Option<Uuid>,
    event_type: &str,
    outcome: &str,
    request_id: Uuid,
) -> Result<(), AuthError> {
    sqlx::query(
        "insert into audit_logs (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, $4, $5, $6, '{}'::jsonb)",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(event_type)
    .bind(outcome)
    .bind(request_id)
    .execute(&mut **tx)
    .await
    .map_err(|_| AuthError::Internal)?;
    Ok(())
}

async fn insert_security_event(
    pool: &PgPool,
    actor_user_id: Option<Uuid>,
    event_type: &str,
    outcome: &str,
    username_normalized: Option<&str>,
    request_id: Uuid,
) {
    let _ = sqlx::query(
        "insert into security_events (id, actor_user_id, event_type, outcome, request_id, username_normalized, metadata)
         values ($1, $2, $3, $4, $5, $6, '{}'::jsonb)",
    )
    .bind(Uuid::now_v7())
    .bind(actor_user_id)
    .bind(event_type)
    .bind(outcome)
    .bind(request_id)
    .bind(username_normalized)
    .execute(pool)
    .await;
}

async fn workspace_for_user(pool: &PgPool, user_id: Uuid) -> Result<WorkspaceSummary, AuthError> {
    let row = sqlx::query(
        "select w.id, w.name
         from workspaces w
         join workspace_memberships wm on wm.workspace_id = w.id
         where wm.user_id = $1",
    )
    .bind(user_id)
    .fetch_optional(pool)
    .await
    .map_err(|_| AuthError::Internal)?
    .ok_or(AuthError::Unauthorized)?;
    Ok(WorkspaceSummary {
        id: row.get("id"),
        name: row.get("name"),
    })
}

async fn roles_for_user(pool: &PgPool, user_id: Uuid) -> Result<Vec<String>, AuthError> {
    let rows =
        sqlx::query("select role_name from user_roles where user_id = $1 order by role_name")
            .bind(user_id)
            .fetch_all(pool)
            .await
            .map_err(|_| AuthError::Internal)?;
    Ok(rows.into_iter().map(|row| row.get("role_name")).collect())
}

pub(crate) async fn current_context(
    state: &AuthState,
    headers: &HeaderMap,
) -> Result<AuthContext, AuthError> {
    let token = cookie_value(headers, &state.config.cookie_name).ok_or(AuthError::Unauthorized)?;
    let token_hash = digest_token(&token);
    let now = OffsetDateTime::now_utc();
    let row = sqlx::query(
        "select s.id as session_id, u.id as user_id, u.username, w.id as workspace_id, w.name as workspace_name
         from sessions s
         join users u on u.id = s.user_id
         join workspace_memberships wm on wm.user_id = u.id
         join workspaces w on w.id = wm.workspace_id
         where s.token_hash = $1
           and s.revoked_at is null
           and s.absolute_expires_at > $2
           and s.idle_expires_at > $2
           and u.disabled_at is null",
    )
    .bind(token_hash)
    .bind(now)
    .fetch_optional(&state.pool)
    .await
    .map_err(|_| AuthError::Internal)?
    .ok_or(AuthError::Unauthorized)?;
    let session_id: Uuid = row.get("session_id");
    let user_id: Uuid = row.get("user_id");
    sqlx::query("update sessions set last_seen_at = $1, idle_expires_at = $2 where id = $3")
        .bind(now)
        .bind(now + state.config.idle_session_ttl)
        .bind(session_id)
        .execute(&state.pool)
        .await
        .map_err(|_| AuthError::Internal)?;
    Ok(AuthContext {
        session_id,
        user_id,
        username: row.get("username"),
        workspace_id: row.get("workspace_id"),
        workspace_name: row.get("workspace_name"),
        roles: roles_for_user(&state.pool, user_id).await?,
    })
}

async fn session_id_from_cookie(
    pool: &PgPool,
    config: &AuthConfig,
    headers: &HeaderMap,
) -> Option<Uuid> {
    let token = cookie_value(headers, &config.cookie_name)?;
    let token_hash = digest_token(&token);
    sqlx::query_scalar("select id from sessions where token_hash = $1 and revoked_at is null")
        .bind(token_hash)
        .fetch_optional(pool)
        .await
        .ok()
        .flatten()
}

pub(crate) async fn ensure_csrf(state: &AuthState, headers: &HeaderMap) -> Result<(), AuthError> {
    let token = cookie_value(headers, &state.config.cookie_name).ok_or(AuthError::Unauthorized)?;
    let csrf = headers
        .get(CSRF_HEADER)
        .and_then(|value| value.to_str().ok())
        .ok_or(AuthError::Forbidden("csrf_required"))?;
    let row =
        sqlx::query("select csrf_hash from sessions where token_hash = $1 and revoked_at is null")
            .bind(digest_token(&token))
            .fetch_optional(&state.pool)
            .await
            .map_err(|_| AuthError::Internal)?
            .ok_or(AuthError::Unauthorized)?;
    let expected: Option<String> = row.get("csrf_hash");
    let Some(expected) = expected else {
        return Err(AuthError::Forbidden("csrf_required"));
    };
    if !constant_time_eq(expected.as_bytes(), digest_token(csrf).as_bytes()) {
        return Err(AuthError::Forbidden("csrf_invalid"));
    }
    Ok(())
}

pub(crate) fn ensure_allowed_origin(
    config: &AuthConfig,
    headers: &HeaderMap,
) -> Result<(), AuthError> {
    let Some(expected) = config.public_origin.as_deref() else {
        return Ok(());
    };
    let supplied = headers
        .get(header::ORIGIN)
        .or_else(|| headers.get(header::REFERER))
        .and_then(|value| value.to_str().ok());
    let Some(supplied) = supplied else {
        return Err(AuthError::Forbidden("origin_required"));
    };
    if supplied == expected || supplied.starts_with(&format!("{expected}/")) {
        Ok(())
    } else {
        Err(AuthError::Forbidden("origin_mismatch"))
    }
}

fn cookie_value(headers: &HeaderMap, name: &str) -> Option<String> {
    headers
        .get(header::COOKIE)
        .and_then(|value| value.to_str().ok())
        .and_then(|cookies| {
            cookies.split(';').find_map(|part| {
                let (key, value) = part.trim().split_once('=')?;
                (key == name).then(|| value.to_string())
            })
        })
}

fn with_session_cookie<T: Serialize>(config: &AuthConfig, token: &str, body: Json<T>) -> Response {
    let mut cookie = format!(
        "{}={}; HttpOnly; SameSite=Lax; Path=/; Max-Age={}",
        config.cookie_name,
        token,
        config.absolute_session_ttl.whole_seconds()
    );
    if config.cookie_secure {
        cookie.push_str("; Secure");
    }
    let mut response = body.into_response();
    response.headers_mut().append(
        header::SET_COOKIE,
        HeaderValue::from_str(&cookie).expect("session cookie is valid"),
    );
    response
}

fn with_clear_cookie<T: Serialize>(config: &AuthConfig, body: Json<T>) -> Response {
    let mut cookie = format!(
        "{}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0",
        config.cookie_name
    );
    if config.cookie_secure {
        cookie.push_str("; Secure");
    }
    let mut response = body.into_response();
    response.headers_mut().append(
        header::SET_COOKIE,
        HeaderValue::from_str(&cookie).expect("clear cookie is valid"),
    );
    response
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn password_policy_allows_phrases_and_rejects_short_passwords() {
        assert!(validate_password("这是 一个 足够 长 的 密码 短语").is_ok());
        assert!(validate_password("too short").is_err());
    }

    #[test]
    fn permissions_do_not_grant_cross_workspace_business_read() {
        let permissions = permissions_for_roles(&["admin".to_string()]);
        assert!(permissions.contains(&"session.any.read".to_string()));
        assert!(!permissions.contains(&"workspace.any.business.read".to_string()));
    }

    #[test]
    fn import_permission_matrix_is_centralized_by_role() {
        let admin = permissions_for_roles(&["admin".to_string()]);
        assert!(admin.contains(&"import.read".to_string()));
        assert!(admin.contains(&"import.upload".to_string()));
        assert!(admin.contains(&"import.rollback".to_string()));
        assert!(admin.contains(&"import.compensate".to_string()));
        assert!(admin.contains(&"object.govern".to_string()));

        let analyst = permissions_for_roles(&["analyst".to_string()]);
        assert!(analyst.contains(&"import.read".to_string()));
        assert!(analyst.contains(&"import.upload".to_string()));
        assert!(analyst.contains(&"import.rollback".to_string()));
        assert!(analyst.contains(&"import.compensate".to_string()));
        assert!(!analyst.contains(&"object.govern".to_string()));

        let viewer = permissions_for_roles(&["viewer".to_string()]);
        assert!(viewer.contains(&"import.read".to_string()));
        assert!(!viewer.contains(&"import.upload".to_string()));
        assert!(!viewer.contains(&"import.rollback".to_string()));
        assert!(!viewer.contains(&"import.compensate".to_string()));
        assert!(!viewer.contains(&"object.govern".to_string()));
    }

    #[test]
    fn unknown_or_empty_roles_receive_no_import_permissions() {
        assert!(
            permissions_for_roles(&["unknown".to_string()])
                .iter()
                .all(|permission| !permission.starts_with("import."))
        );
        assert!(
            permissions_for_roles(&[])
                .iter()
                .all(|permission| !permission.starts_with("import."))
        );
    }

    #[test]
    fn automatic_collection_identity_is_exact_not_role_only() {
        let mut context = AuthContext {
            session_id: Uuid::now_v7(),
            user_id: Uuid::now_v7(),
            username: COLLECTOR_ACCOUNT_USERNAME.to_string(),
            workspace_id: Uuid::now_v7(),
            workspace_name: "test".to_string(),
            roles: vec!["analyst".to_string()],
        };
        assert!(context.is_collector_account());
        context.username = "ordinary-analyst".to_string();
        assert!(!context.is_collector_account());
        context.username = COLLECTOR_ACCOUNT_USERNAME.to_string();
        context.roles.push("admin".to_string());
        assert!(!context.is_collector_account());
    }

    #[test]
    fn token_hash_is_not_plaintext_token() {
        let token = random_token();
        let hash = digest_token(&token);
        assert_ne!(token, hash);
        assert!(token.len() >= 40);
    }

    #[test]
    fn login_limiter_blocks_after_ten_attempts() {
        let limiter = LoginLimiter::default();
        for _ in 0..10 {
            assert!(limiter.record_and_check("limited-user").is_ok());
        }

        assert!(matches!(
            limiter.record_and_check("limited-user"),
            Err(AuthError::RateLimited)
        ));
    }
}
