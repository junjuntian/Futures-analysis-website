use domain::object_governance::{
    ObjectConsistencyFinding, ObjectConsistencyReport, ObjectConsistencyRun,
    ObjectQuarantineResponse,
};
use serde_json::json;
use sqlx::{PgPool, Postgres, Row, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

const IDEMPOTENCY_LOCK_SQL: &str =
    "select pg_advisory_xact_lock(hashtextextended($1::text || ':objects:' || $2, 0))";

#[derive(Debug, thiserror::Error)]
pub enum ObjectGovernanceError {
    #[error("object governance resource is not visible")]
    NotFound,
    #[error("idempotency key was reused")]
    IdempotencyKeyReused,
    #[error("finding is not eligible for quarantine")]
    QuarantineNotAllowed,
    #[error("object changed after scan")]
    FindingStale,
    #[error("worker lease was lost")]
    LeaseLost,
    #[error("invalid stored object governance state")]
    InvalidStoredState,
    #[error("database operation failed")]
    Database(#[from] sqlx::Error),
}

impl ObjectGovernanceError {
    pub fn retryable(&self) -> bool {
        matches!(self, Self::Database(_))
    }

    pub const fn code(&self) -> &'static str {
        match self {
            Self::NotFound => "object_governance_not_found",
            Self::IdempotencyKeyReused => "idempotency_key_reused",
            Self::QuarantineNotAllowed => "object_quarantine_not_allowed",
            Self::FindingStale => "object_finding_stale",
            Self::LeaseLost => "lease_lost",
            Self::InvalidStoredState => "invalid_object_governance_state",
            Self::Database(_) => "database_error",
        }
    }
}

#[derive(Debug, Clone)]
pub struct ExpectedObject {
    pub id: Uuid,
    pub object_key: String,
    pub sha256: String,
    pub size_bytes: i64,
    pub backend: String,
    pub state: String,
    pub retention_until: Option<OffsetDateTime>,
    pub created_at: OffsetDateTime,
    pub referenced: bool,
}

#[derive(Debug, Clone)]
pub struct NewFinding {
    pub id: Uuid,
    pub stored_object_id: Option<Uuid>,
    pub finding_type: String,
    pub observed_object_key: Option<String>,
    pub observed_sha256: Option<String>,
    pub observed_size_bytes: Option<i64>,
}

#[derive(Debug, Clone)]
pub struct QuarantineWork {
    pub request_id: Uuid,
    pub finding_id: Uuid,
    pub requested_by: Uuid,
    pub stored_object_id: Option<Uuid>,
    pub source_object_key: String,
    pub sha256: String,
    pub size_bytes: i64,
}

#[derive(Debug, Clone)]
pub struct ClaimedGovernanceJob {
    pub id: Uuid,
    pub workspace_id: Uuid,
    pub job_type: String,
    pub aggregate_id: Uuid,
    pub attempt_count: i32,
    pub max_attempts: i32,
    pub lease_generation: i64,
}

pub async fn claim_next_job(
    pool: &PgPool,
    worker_id: &str,
    lease_seconds: i64,
    workspace_id: Uuid,
) -> Result<Option<ClaimedGovernanceJob>, ObjectGovernanceError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let row = sqlx::query(
        "select id, job_type, scan_run_id, quarantine_request_id,
                    attempt_count, max_attempts, lease_generation
               from object_governance_jobs
              where workspace_id = $1
                and (
                    (status = 'queued' and available_at <= now() and attempt_count < max_attempts)
                    or (status = 'running' and lease_expires_at < now())
                )
              order by available_at, created_at, id
              for update skip locked
              limit 1",
    )
    .bind(workspace_id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(row) = row else {
        tx.commit().await?;
        return Ok(None);
    };
    let job_type = row.get::<String, _>("job_type");
    let aggregate_id = match job_type.as_str() {
        "object_consistency_scan" => row
            .get::<Option<Uuid>, _>("scan_run_id")
            .ok_or(ObjectGovernanceError::InvalidStoredState)?,
        "object_quarantine" => row
            .get::<Option<Uuid>, _>("quarantine_request_id")
            .ok_or(ObjectGovernanceError::InvalidStoredState)?,
        _ => return Err(ObjectGovernanceError::InvalidStoredState),
    };
    let prior_attempt_count = row.get::<i32, _>("attempt_count");
    let max_attempts = row.get::<i32, _>("max_attempts");
    let job_id = row.get("id");
    if prior_attempt_count >= max_attempts {
        sqlx::query(
            "update object_governance_jobs
                    set status = 'dead_letter', leased_by = null, lease_expires_at = null,
                        last_error_code = 'lease_attempts_exhausted',
                        finished_at = now(), updated_at = now()
                  where workspace_id = $1 and id = $2",
        )
        .bind(workspace_id)
        .bind(job_id)
        .execute(&mut *tx)
        .await?;
        let actor_user_id = match job_type.as_str() {
            "object_consistency_scan" => {
                sqlx::query_scalar::<_, Uuid>(
                    "update object_consistency_runs
                            set status = 'failed', finished_at = now()
                          where workspace_id = $1 and id = $2 and status = 'running'
                          returning requested_by",
                )
                .bind(workspace_id)
                .bind(aggregate_id)
                .fetch_one(&mut *tx)
                .await?
            }
            "object_quarantine" => {
                sqlx::query_scalar::<_, Uuid>(
                    "update object_quarantine_requests
                            set status = 'failed', finished_at = now()
                          where workspace_id = $1 and id = $2 and status = 'running'
                          returning requested_by",
                )
                .bind(workspace_id)
                .bind(aggregate_id)
                .fetch_one(&mut *tx)
                .await?
            }
            _ => return Err(ObjectGovernanceError::InvalidStoredState),
        };
        insert_audit(
            &mut tx,
            workspace_id,
            actor_user_id,
            Uuid::now_v7(),
            "object.governance_dead_letter",
            "failure",
            json!({
                "job_id": job_id,
                "job_type": job_type,
                "aggregate_id": aggregate_id,
                "error_code": "lease_attempts_exhausted"
            }),
        )
        .await?;
        tx.commit().await?;
        return Ok(None);
    }
    let attempt_count = prior_attempt_count + 1;
    let lease_generation = row.get::<i64, _>("lease_generation") + 1;
    let updated = sqlx::query(
        "update object_governance_jobs
                set status = 'running', attempt_count = $1, leased_by = $2,
                    lease_expires_at = now() + make_interval(secs => $3),
                    lease_generation = $4, updated_at = now()
              where workspace_id = $5 and id = $6",
    )
    .bind(attempt_count)
    .bind(worker_id)
    .bind(lease_seconds as f64)
    .bind(lease_generation)
    .bind(workspace_id)
    .bind(job_id)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    require_one(updated)?;
    let aggregate_updated = match job_type.as_str() {
        "object_consistency_scan" => sqlx::query(
            "update object_consistency_runs
                        set status = 'running'
                      where workspace_id = $1 and id = $2
                        and status in ('queued', 'running')",
        )
        .bind(workspace_id)
        .bind(aggregate_id)
        .execute(&mut *tx)
        .await?
        .rows_affected(),
        "object_quarantine" => sqlx::query(
            "update object_quarantine_requests
                        set status = 'running'
                      where workspace_id = $1 and id = $2
                        and status in ('queued', 'running')",
        )
        .bind(workspace_id)
        .bind(aggregate_id)
        .execute(&mut *tx)
        .await?
        .rows_affected(),
        _ => 0,
    };
    require_one(aggregate_updated)?;
    tx.commit().await?;
    Ok(Some(ClaimedGovernanceJob {
        id: job_id,
        workspace_id,
        job_type,
        aggregate_id,
        attempt_count,
        max_attempts,
        lease_generation,
    }))
}

pub async fn renew_job(
    pool: &PgPool,
    job: &ClaimedGovernanceJob,
    worker_id: &str,
    lease_seconds: i64,
) -> Result<(), ObjectGovernanceError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, job.workspace_id).await?;
    let updated = sqlx::query(
        "update object_governance_jobs
            set lease_expires_at = now() + make_interval(secs => $1), updated_at = now()
          where workspace_id = $2 and id = $3 and status = 'running'
            and leased_by = $4 and lease_generation = $5",
    )
    .bind(lease_seconds as f64)
    .bind(job.workspace_id)
    .bind(job.id)
    .bind(worker_id)
    .bind(job.lease_generation)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    require_one(updated)?;
    tx.commit().await?;
    Ok(())
}

pub async fn record_job_failure(
    pool: &PgPool,
    job: &ClaimedGovernanceJob,
    worker_id: &str,
    error_code: &str,
    retryable: bool,
) -> Result<(), ObjectGovernanceError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, job.workspace_id).await?;
    let retry = retryable && job.attempt_count < job.max_attempts;
    let status = if retry { "queued" } else { "failed" };
    let updated = sqlx::query(
        "update object_governance_jobs
            set status = $1, leased_by = null, lease_expires_at = null,
                available_at = case when $1 = 'queued' then now() + interval '1 second'
                                    else available_at end,
                last_error_code = $2,
                finished_at = case when $1 = 'failed' then now() else null end,
                updated_at = now()
          where workspace_id = $3 and id = $4 and status = 'running'
            and leased_by = $5 and lease_generation = $6",
    )
    .bind(status)
    .bind(error_code)
    .bind(job.workspace_id)
    .bind(job.id)
    .bind(worker_id)
    .bind(job.lease_generation)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    require_one(updated)?;
    if retry {
        match job.job_type.as_str() {
            "object_consistency_scan" => {
                sqlx::query(
                    "update object_consistency_runs set status = 'queued'
                      where workspace_id = $1 and id = $2 and status = 'running'",
                )
                .bind(job.workspace_id)
                .bind(job.aggregate_id)
                .execute(&mut *tx)
                .await?;
            }
            "object_quarantine" => {
                sqlx::query(
                    "update object_quarantine_requests set status = 'queued'
                      where workspace_id = $1 and id = $2 and status = 'running'",
                )
                .bind(job.workspace_id)
                .bind(job.aggregate_id)
                .execute(&mut *tx)
                .await?;
            }
            _ => return Err(ObjectGovernanceError::InvalidStoredState),
        }
    } else {
        let actor_user_id = match job.job_type.as_str() {
            "object_consistency_scan" => {
                sqlx::query_scalar::<_, Uuid>(
                    "update object_consistency_runs
                        set status = 'failed', finished_at = now()
                      where workspace_id = $1 and id = $2 and status = 'running'
                      returning requested_by",
                )
                .bind(job.workspace_id)
                .bind(job.aggregate_id)
                .fetch_one(&mut *tx)
                .await?
            }
            "object_quarantine" => {
                sqlx::query_scalar::<_, Uuid>(
                    "update object_quarantine_requests
                        set status = 'failed', finished_at = now()
                      where workspace_id = $1 and id = $2 and status = 'running'
                      returning requested_by",
                )
                .bind(job.workspace_id)
                .bind(job.aggregate_id)
                .fetch_one(&mut *tx)
                .await?
            }
            _ => return Err(ObjectGovernanceError::InvalidStoredState),
        };
        insert_audit(
            &mut tx,
            job.workspace_id,
            actor_user_id,
            Uuid::now_v7(),
            "object.governance_failed",
            "failure",
            json!({
                "job_id": job.id,
                "job_type": job.job_type,
                "aggregate_id": job.aggregate_id,
                "error_code": error_code
            }),
        )
        .await?;
    }
    tx.commit().await?;
    Ok(())
}

pub async fn queue_scan(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    idempotency_key_hash: &str,
    request_hash: &str,
    root_fingerprint: &str,
    audit_request_id: Uuid,
) -> Result<ObjectConsistencyRun, ObjectGovernanceError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    lock_idempotency(&mut tx, workspace_id, idempotency_key_hash).await?;
    if let Some(existing) = load_scan_by_key(&mut tx, workspace_id, idempotency_key_hash).await? {
        if existing.1 != request_hash {
            return Err(ObjectGovernanceError::IdempotencyKeyReused);
        }
        insert_audit(
            &mut tx,
            workspace_id,
            actor_user_id,
            audit_request_id,
            "object.scan_replayed",
            "success",
            json!({"run_id": existing.0.run_id, "job_id": existing.0.job_id}),
        )
        .await?;
        tx.commit().await?;
        return Ok(ObjectConsistencyRun {
            replayed: true,
            ..existing.0
        });
    }
    let run_id = Uuid::now_v7();
    let job_id = Uuid::now_v7();
    sqlx::query(
        "insert into object_consistency_runs
            (id, workspace_id, status, requested_by, root_fingerprint,
             idempotency_key_hash, request_hash)
         values ($1, $2, 'queued', $3, $4, $5, $6)",
    )
    .bind(run_id)
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(root_fingerprint)
    .bind(idempotency_key_hash)
    .bind(request_hash)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "insert into object_governance_jobs
            (id, workspace_id, job_type, scan_run_id, status,
             attempt_count, max_attempts, available_at, lease_generation)
         values ($1, $2, 'object_consistency_scan', $3, 'queued', 0, 5, now(), 0)",
    )
    .bind(job_id)
    .bind(workspace_id)
    .bind(run_id)
    .execute(&mut *tx)
    .await?;
    insert_audit(
        &mut tx,
        workspace_id,
        actor_user_id,
        audit_request_id,
        "object.scan_queued",
        "success",
        json!({"run_id": run_id, "job_id": job_id}),
    )
    .await?;
    tx.commit().await?;
    Ok(ObjectConsistencyRun {
        run_id,
        job_id,
        status: "queued".into(),
        scanned_object_count: 0,
        finding_count: 0,
        replayed: false,
    })
}

pub async fn get_report(
    pool: &PgPool,
    workspace_id: Uuid,
    run_id: Uuid,
) -> Result<ObjectConsistencyReport, ObjectGovernanceError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let run = load_scan(&mut tx, workspace_id, run_id)
        .await?
        .ok_or(ObjectGovernanceError::NotFound)?;
    let rows = sqlx::query(
        "select finding.id, finding.run_id, finding.stored_object_id,
                finding.finding_type, finding.observed_object_key,
                finding.observed_sha256, finding.observed_size_bytes,
                finding.disposition_status,
                (
                    finding.finding_type = 'orphan_object'
                    and finding.disposition_status = 'detected'
                    and run.status = 'completed'
                    and finding.observed_sha256 is not null
                    and finding.observed_size_bytes is not null
                    and finding.observed_object_key like
                        'objects/' || finding.workspace_id::text || '/%'
                    and (
                        finding.stored_object_id is null
                        or (
                            object.backend = 'local'
                            and object.state = 'available'
                            and (object.retention_until is null or object.retention_until <= now())
                            and object.object_key = finding.observed_object_key
                            and object.sha256 = finding.observed_sha256
                            and object.size_bytes = finding.observed_size_bytes
                        )
                    )
                    and not exists (
                        select 1 from import_files file
                         where file.workspace_id = finding.workspace_id
                           and file.stored_object_id = finding.stored_object_id
                    )
                    and not exists (
                        select 1 from object_quarantine_requests request
                         where request.workspace_id = finding.workspace_id
                           and request.finding_id = finding.id
                           and request.status in ('queued', 'running')
                    )
                ) as quarantine_eligible
           from object_consistency_findings finding
           join object_consistency_runs run
             on run.workspace_id = finding.workspace_id
            and run.id = finding.run_id
           left join stored_objects object
             on object.workspace_id = finding.workspace_id
            and object.id = finding.stored_object_id
          where finding.workspace_id = $1 and finding.run_id = $2
          order by finding.finding_type, finding.id",
    )
    .bind(workspace_id)
    .bind(run_id)
    .fetch_all(&mut *tx)
    .await?;
    let findings = rows
        .into_iter()
        .map(finding_from_row)
        .collect::<Result<Vec<_>, _>>()?;
    tx.commit().await?;
    Ok(ObjectConsistencyReport { run, findings })
}

async fn load_scan(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    run_id: Uuid,
) -> Result<Option<ObjectConsistencyRun>, ObjectGovernanceError> {
    let row = sqlx::query(
        "select run.id, run.status, run.scanned_object_count, run.finding_count,
                job.id as job_id
           from object_consistency_runs run
           join object_governance_jobs job
             on job.workspace_id = run.workspace_id
            and job.scan_run_id = run.id
            and job.job_type = 'object_consistency_scan'
          where run.workspace_id = $1 and run.id = $2",
    )
    .bind(workspace_id)
    .bind(run_id)
    .fetch_optional(&mut **tx)
    .await?;
    row.map(|row| run_from_row(&row)).transpose()
}

async fn load_scan_by_key(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    idempotency_key_hash: &str,
) -> Result<Option<(ObjectConsistencyRun, String)>, ObjectGovernanceError> {
    let row = sqlx::query(
        "select run.id, run.status, run.scanned_object_count, run.finding_count,
                run.request_hash, job.id as job_id
           from object_consistency_runs run
           join object_governance_jobs job
             on job.workspace_id = run.workspace_id
            and job.scan_run_id = run.id
            and job.job_type = 'object_consistency_scan'
          where run.workspace_id = $1 and run.idempotency_key_hash = $2",
    )
    .bind(workspace_id)
    .bind(idempotency_key_hash)
    .fetch_optional(&mut **tx)
    .await?;
    row.map(|row| Ok((run_from_row(&row)?, row.get("request_hash"))))
        .transpose()
}

fn run_from_row(
    row: &sqlx::postgres::PgRow,
) -> Result<ObjectConsistencyRun, ObjectGovernanceError> {
    Ok(ObjectConsistencyRun {
        run_id: row.get("id"),
        job_id: row.get("job_id"),
        status: row.get("status"),
        scanned_object_count: nonnegative_u64(row.get("scanned_object_count"))?,
        finding_count: nonnegative_u64(row.get("finding_count"))?,
        replayed: false,
    })
}

fn finding_from_row(
    row: sqlx::postgres::PgRow,
) -> Result<ObjectConsistencyFinding, ObjectGovernanceError> {
    Ok(ObjectConsistencyFinding {
        finding_id: row.get("id"),
        run_id: row.get("run_id"),
        stored_object_id: row.get("stored_object_id"),
        finding_type: row.get("finding_type"),
        observed_object_key: row.get("observed_object_key"),
        observed_sha256: row.get("observed_sha256"),
        observed_size_bytes: row
            .get::<Option<i64>, _>("observed_size_bytes")
            .map(nonnegative_u64)
            .transpose()?,
        disposition_status: row.get("disposition_status"),
        quarantine_eligible: row.get("quarantine_eligible"),
    })
}

pub async fn load_expected_objects(
    pool: &PgPool,
    workspace_id: Uuid,
) -> Result<Vec<ExpectedObject>, ObjectGovernanceError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let rows = sqlx::query(
        "select object.id, object.object_key, object.sha256, object.size_bytes,
                object.backend, object.state, object.retention_until, object.created_at,
                exists(
                    select 1 from import_files file
                     where file.workspace_id = object.workspace_id
                       and file.stored_object_id = object.id
                ) as referenced
           from stored_objects object
          where object.workspace_id = $1
          order by object.id",
    )
    .bind(workspace_id)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(rows
        .into_iter()
        .map(|row| ExpectedObject {
            id: row.get("id"),
            object_key: row.get("object_key"),
            sha256: row.get("sha256"),
            size_bytes: row.get("size_bytes"),
            backend: row.get("backend"),
            state: row.get("state"),
            retention_until: row.get("retention_until"),
            created_at: row.get("created_at"),
            referenced: row.get("referenced"),
        })
        .collect())
}

pub async fn load_scan_root_fingerprint(
    pool: &PgPool,
    workspace_id: Uuid,
    run_id: Uuid,
) -> Result<String, ObjectGovernanceError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let fingerprint = sqlx::query_scalar::<_, String>(
        "select root_fingerprint from object_consistency_runs
          where workspace_id = $1 and id = $2 and status = 'running'",
    )
    .bind(workspace_id)
    .bind(run_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ObjectGovernanceError::InvalidStoredState)?;
    tx.commit().await?;
    Ok(fingerprint)
}

#[allow(clippy::too_many_arguments)]
pub async fn complete_scan(
    pool: &PgPool,
    workspace_id: Uuid,
    run_id: Uuid,
    job_id: Uuid,
    worker_id: &str,
    lease_generation: i64,
    scanned_object_count: i64,
    findings: &[NewFinding],
) -> Result<(), ObjectGovernanceError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let requested_by = sqlx::query_scalar::<_, Uuid>(
        "select requested_by from object_consistency_runs
          where workspace_id = $1 and id = $2 and status = 'running'
          for update",
    )
    .bind(workspace_id)
    .bind(run_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ObjectGovernanceError::InvalidStoredState)?;
    for finding in findings {
        sqlx::query(
            "insert into object_consistency_findings
                (id, workspace_id, run_id, stored_object_id, finding_type,
                 observed_object_key, observed_sha256, observed_size_bytes)
             values ($1, $2, $3, $4, $5, $6, $7, $8)
             on conflict do nothing",
        )
        .bind(finding.id)
        .bind(workspace_id)
        .bind(run_id)
        .bind(finding.stored_object_id)
        .bind(&finding.finding_type)
        .bind(&finding.observed_object_key)
        .bind(&finding.observed_sha256)
        .bind(finding.observed_size_bytes)
        .execute(&mut *tx)
        .await?;
    }
    let finding_count = sqlx::query_scalar::<_, i64>(
        "select count(*) from object_consistency_findings
          where workspace_id = $1 and run_id = $2",
    )
    .bind(workspace_id)
    .bind(run_id)
    .fetch_one(&mut *tx)
    .await?;
    let job_updated = sqlx::query(
        "update object_governance_jobs
            set status = 'succeeded', leased_by = null, lease_expires_at = null,
                finished_at = now(), updated_at = now()
          where workspace_id = $1 and id = $2 and job_type = 'object_consistency_scan'
            and scan_run_id = $3 and status = 'running' and leased_by = $4
            and lease_generation = $5",
    )
    .bind(workspace_id)
    .bind(job_id)
    .bind(run_id)
    .bind(worker_id)
    .bind(lease_generation)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    require_one(job_updated)?;
    sqlx::query(
        "update object_consistency_runs
            set status = 'completed', scanned_object_count = $1, finding_count = $2,
                finished_at = now()
          where workspace_id = $3 and id = $4 and status = 'running'",
    )
    .bind(scanned_object_count)
    .bind(finding_count)
    .bind(workspace_id)
    .bind(run_id)
    .execute(&mut *tx)
    .await?;
    insert_audit(
        &mut tx,
        workspace_id,
        requested_by,
        Uuid::now_v7(),
        "object.scan_completed",
        "success",
        json!({
            "run_id": run_id,
            "scanned_object_count": scanned_object_count,
            "finding_count": finding_count
        }),
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
pub async fn queue_quarantine(
    pool: &PgPool,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    finding_id: Uuid,
    idempotency_key_hash: &str,
    request_hash: &str,
    audit_request_id: Uuid,
) -> Result<ObjectQuarantineResponse, ObjectGovernanceError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    lock_idempotency(&mut tx, workspace_id, idempotency_key_hash).await?;
    if let Some(existing) =
        load_quarantine_by_key(&mut tx, workspace_id, idempotency_key_hash).await?
    {
        if existing.1 != request_hash || existing.0.finding_id != finding_id {
            return Err(ObjectGovernanceError::IdempotencyKeyReused);
        }
        insert_audit(
            &mut tx,
            workspace_id,
            actor_user_id,
            audit_request_id,
            "object.quarantine_replayed",
            "success",
            json!({
                "quarantine_request_id": existing.0.quarantine_request_id,
                "finding_id": existing.0.finding_id,
                "job_id": existing.0.job_id
            }),
        )
        .await?;
        tx.commit().await?;
        return Ok(ObjectQuarantineResponse {
            replayed: true,
            ..existing.0
        });
    }
    if let Err(error) = lock_eligible_finding(&mut tx, workspace_id, finding_id).await {
        if matches!(error, ObjectGovernanceError::QuarantineNotAllowed)
            && let Some(existing) =
                load_quarantine_by_finding(&mut tx, workspace_id, finding_id).await?
        {
            insert_audit(
                &mut tx,
                workspace_id,
                actor_user_id,
                audit_request_id,
                "object.quarantine_replayed",
                "success",
                json!({
                    "quarantine_request_id": existing.quarantine_request_id,
                    "finding_id": existing.finding_id,
                    "job_id": existing.job_id
                }),
            )
            .await?;
            tx.commit().await?;
            return Ok(ObjectQuarantineResponse {
                replayed: true,
                ..existing
            });
        }
        return Err(error);
    }
    if let Some(existing) = load_quarantine_by_finding(&mut tx, workspace_id, finding_id).await? {
        insert_audit(
            &mut tx,
            workspace_id,
            actor_user_id,
            audit_request_id,
            "object.quarantine_replayed",
            "success",
            json!({
                "quarantine_request_id": existing.quarantine_request_id,
                "finding_id": existing.finding_id,
                "job_id": existing.job_id
            }),
        )
        .await?;
        tx.commit().await?;
        return Ok(ObjectQuarantineResponse {
            replayed: true,
            ..existing
        });
    }
    let request_id = Uuid::now_v7();
    let job_id = Uuid::now_v7();
    sqlx::query(
        "insert into object_quarantine_requests
            (id, workspace_id, finding_id, status, requested_by,
             idempotency_key_hash, request_hash)
         values ($1, $2, $3, 'queued', $4, $5, $6)",
    )
    .bind(request_id)
    .bind(workspace_id)
    .bind(finding_id)
    .bind(actor_user_id)
    .bind(idempotency_key_hash)
    .bind(request_hash)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "insert into object_governance_jobs
            (id, workspace_id, job_type, quarantine_request_id, status,
             attempt_count, max_attempts, available_at, lease_generation)
         values ($1, $2, 'object_quarantine', $3, 'queued', 0, 5, now(), 0)",
    )
    .bind(job_id)
    .bind(workspace_id)
    .bind(request_id)
    .execute(&mut *tx)
    .await?;
    insert_audit(
        &mut tx,
        workspace_id,
        actor_user_id,
        audit_request_id,
        "object.quarantine_queued",
        "success",
        json!({"quarantine_request_id": request_id, "finding_id": finding_id, "job_id": job_id}),
    )
    .await?;
    tx.commit().await?;
    Ok(ObjectQuarantineResponse {
        quarantine_request_id: request_id,
        finding_id,
        job_id,
        status: "queued".into(),
        replayed: false,
    })
}

async fn load_quarantine_by_finding(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    finding_id: Uuid,
) -> Result<Option<ObjectQuarantineResponse>, ObjectGovernanceError> {
    let row = sqlx::query(
        "select request.id, request.finding_id, request.status, job.id as job_id
           from object_quarantine_requests request
           join object_governance_jobs job
             on job.workspace_id = request.workspace_id
            and job.quarantine_request_id = request.id
            and job.job_type = 'object_quarantine'
          where request.workspace_id = $1 and request.finding_id = $2",
    )
    .bind(workspace_id)
    .bind(finding_id)
    .fetch_optional(&mut **tx)
    .await?;
    Ok(row.map(|row| ObjectQuarantineResponse {
        quarantine_request_id: row.get("id"),
        finding_id: row.get("finding_id"),
        job_id: row.get("job_id"),
        status: row.get("status"),
        replayed: false,
    }))
}

async fn load_quarantine_by_key(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    idempotency_key_hash: &str,
) -> Result<Option<(ObjectQuarantineResponse, String)>, ObjectGovernanceError> {
    let row = sqlx::query(
        "select request.id, request.finding_id, request.status, request.request_hash,
                job.id as job_id
           from object_quarantine_requests request
           join object_governance_jobs job
             on job.workspace_id = request.workspace_id
            and job.quarantine_request_id = request.id
            and job.job_type = 'object_quarantine'
          where request.workspace_id = $1 and request.idempotency_key_hash = $2",
    )
    .bind(workspace_id)
    .bind(idempotency_key_hash)
    .fetch_optional(&mut **tx)
    .await?;
    Ok(row.map(|row| {
        (
            ObjectQuarantineResponse {
                quarantine_request_id: row.get("id"),
                finding_id: row.get("finding_id"),
                job_id: row.get("job_id"),
                status: row.get("status"),
                replayed: false,
            },
            row.get("request_hash"),
        )
    }))
}

async fn lock_eligible_finding(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    finding_id: Uuid,
) -> Result<(), ObjectGovernanceError> {
    let identity = sqlx::query(
        "select stored_object_id, observed_object_key
           from object_consistency_findings
          where workspace_id = $1 and id = $2
          for update",
    )
    .bind(workspace_id)
    .bind(finding_id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(ObjectGovernanceError::NotFound)?;
    let observed_object_key = identity
        .get::<Option<String>, _>("observed_object_key")
        .ok_or(ObjectGovernanceError::QuarantineNotAllowed)?;
    sqlx::query(
        "select pg_advisory_xact_lock(
            hashtextextended($1::text || ':object-key:' || $2, 0)
         )",
    )
    .bind(workspace_id)
    .bind(&observed_object_key)
    .execute(&mut **tx)
    .await?;
    if let Some(stored_object_id) = identity.get::<Option<Uuid>, _>("stored_object_id") {
        sqlx::query(
            "select id from stored_objects
              where workspace_id = $1 and id = $2
              for update",
        )
        .bind(workspace_id)
        .bind(stored_object_id)
        .fetch_optional(&mut **tx)
        .await?
        .ok_or(ObjectGovernanceError::QuarantineNotAllowed)?;
    }
    let row = sqlx::query(
        "select finding.finding_type, finding.stored_object_id,
                finding.observed_object_key, finding.observed_sha256,
                finding.observed_size_bytes, finding.disposition_status,
                run.status as run_status,
                object.object_key as registered_object_key,
                object.sha256 as registered_sha256,
                object.size_bytes as registered_size_bytes,
                object.backend, object.state, object.retention_until,
                exists(
                    select 1 from import_files file
                     where file.workspace_id = finding.workspace_id
                       and file.stored_object_id = finding.stored_object_id
                ) as referenced,
                exists(
                    select 1 from stored_objects duplicate
                     where duplicate.workspace_id = finding.workspace_id
                       and duplicate.object_key = finding.observed_object_key
                       and duplicate.id is distinct from finding.stored_object_id
                ) as key_registered
           from object_consistency_findings finding
           join object_consistency_runs run
             on run.workspace_id = finding.workspace_id and run.id = finding.run_id
          left join stored_objects object
             on object.workspace_id = finding.workspace_id
            and object.id = finding.stored_object_id
          where finding.workspace_id = $1 and finding.id = $2",
    )
    .bind(workspace_id)
    .bind(finding_id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(ObjectGovernanceError::NotFound)?;
    let allowed_type = row.get::<String, _>("finding_type") == "orphan_object";
    let stored_object_id = row.get::<Option<Uuid>, _>("stored_object_id");
    let known_state = stored_object_id.is_none()
        || (row.get::<Option<String>, _>("backend").as_deref() == Some("local")
            && row.get::<Option<String>, _>("state").as_deref() == Some("available")
            && row
                .get::<Option<OffsetDateTime>, _>("retention_until")
                .is_none_or(|until| until <= OffsetDateTime::now_utc())
            && row
                .get::<Option<String>, _>("registered_object_key")
                .as_deref()
                == Some(observed_object_key.as_str())
            && row.get::<Option<String>, _>("registered_sha256")
                == row.get::<Option<String>, _>("observed_sha256")
            && row.get::<Option<i64>, _>("registered_size_bytes")
                == row.get::<Option<i64>, _>("observed_size_bytes"));
    if !allowed_type
        || row.get::<String, _>("disposition_status") != "detected"
        || row.get::<String, _>("run_status") != "completed"
        || row
            .get::<Option<String>, _>("observed_object_key")
            .is_none()
        || row.get::<Option<String>, _>("observed_sha256").is_none()
        || row.get::<Option<i64>, _>("observed_size_bytes").is_none()
        || !row
            .get::<String, _>("observed_object_key")
            .starts_with(&format!("objects/{workspace_id}/"))
        || row.get::<bool, _>("referenced")
        || row.get::<bool, _>("key_registered")
        || !known_state
    {
        return Err(ObjectGovernanceError::QuarantineNotAllowed);
    }
    Ok(())
}

pub async fn load_quarantine_work(
    pool: &PgPool,
    workspace_id: Uuid,
    request_id: Uuid,
) -> Result<QuarantineWork, ObjectGovernanceError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let row = sqlx::query(
        "select request.id, request.finding_id, request.requested_by,
                finding.stored_object_id, finding.observed_object_key,
                finding.observed_sha256, finding.observed_size_bytes
           from object_quarantine_requests request
           join object_consistency_findings finding
             on finding.workspace_id = request.workspace_id
            and finding.id = request.finding_id
          where request.workspace_id = $1 and request.id = $2
            and request.status = 'running'",
    )
    .bind(workspace_id)
    .bind(request_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(ObjectGovernanceError::InvalidStoredState)?;
    lock_eligible_finding(&mut tx, workspace_id, row.get("finding_id")).await?;
    tx.commit().await?;
    Ok(QuarantineWork {
        request_id: row.get("id"),
        finding_id: row.get("finding_id"),
        requested_by: row.get("requested_by"),
        stored_object_id: row.get("stored_object_id"),
        source_object_key: row.get("observed_object_key"),
        sha256: row.get("observed_sha256"),
        size_bytes: row.get("observed_size_bytes"),
    })
}

#[allow(clippy::too_many_arguments)]
pub async fn complete_quarantine(
    pool: &PgPool,
    job_id: Uuid,
    worker_id: &str,
    lease_generation: i64,
    work: &QuarantineWork,
    workspace_id: Uuid,
    quarantine_object_key: &str,
) -> Result<(), ObjectGovernanceError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    lock_eligible_finding(&mut tx, workspace_id, work.finding_id).await?;
    if let Some(stored_object_id) = work.stored_object_id {
        let updated = sqlx::query(
            "update stored_objects
                set object_key = $1, state = 'quarantined', updated_at = now()
              where workspace_id = $2 and id = $3
                and object_key = $4 and sha256 = $5 and size_bytes = $6
                and backend = 'local' and state in ('pending', 'available')
                and (retention_until is null or retention_until <= now())
                and not exists (
                    select 1 from import_files file
                     where file.workspace_id = stored_objects.workspace_id
                       and file.stored_object_id = stored_objects.id
                )",
        )
        .bind(quarantine_object_key)
        .bind(workspace_id)
        .bind(stored_object_id)
        .bind(&work.source_object_key)
        .bind(&work.sha256)
        .bind(work.size_bytes)
        .execute(&mut *tx)
        .await?
        .rows_affected();
        require_one(updated).map_err(|_| ObjectGovernanceError::FindingStale)?;
    }
    sqlx::query(
        "insert into object_quarantines
            (id, workspace_id, finding_id, stored_object_id, source_object_key,
             quarantine_object_key, sha256, size_bytes, quarantined_by)
         values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
         on conflict (workspace_id, finding_id) do nothing",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(work.finding_id)
    .bind(work.stored_object_id)
    .bind(&work.source_object_key)
    .bind(quarantine_object_key)
    .bind(&work.sha256)
    .bind(work.size_bytes)
    .bind(work.requested_by)
    .execute(&mut *tx)
    .await?;
    sqlx::query(
        "update object_consistency_findings
            set disposition_status = 'quarantined'
          where workspace_id = $1 and id = $2 and disposition_status = 'detected'",
    )
    .bind(workspace_id)
    .bind(work.finding_id)
    .execute(&mut *tx)
    .await?;
    let job_updated = sqlx::query(
        "update object_governance_jobs
            set status = 'succeeded', leased_by = null, lease_expires_at = null,
                finished_at = now(), updated_at = now()
          where workspace_id = $1 and id = $2 and job_type = 'object_quarantine'
            and quarantine_request_id = $3 and status = 'running' and leased_by = $4
            and lease_generation = $5",
    )
    .bind(workspace_id)
    .bind(job_id)
    .bind(work.request_id)
    .bind(worker_id)
    .bind(lease_generation)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    require_one(job_updated)?;
    sqlx::query(
        "update object_quarantine_requests
            set status = 'succeeded', finished_at = now()
          where workspace_id = $1 and id = $2 and status = 'running'",
    )
    .bind(workspace_id)
    .bind(work.request_id)
    .execute(&mut *tx)
    .await?;
    insert_audit(
        &mut tx,
        workspace_id,
        work.requested_by,
        Uuid::now_v7(),
        "object.quarantined",
        "success",
        json!({
            "quarantine_request_id": work.request_id,
            "finding_id": work.finding_id,
            "stored_object_id": work.stored_object_id,
            "sha256": work.sha256,
            "size_bytes": work.size_bytes
        }),
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

async fn lock_idempotency(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    key: &str,
) -> Result<(), ObjectGovernanceError> {
    sqlx::query(IDEMPOTENCY_LOCK_SQL)
        .bind(workspace_id)
        .bind(key)
        .execute(&mut **tx)
        .await?;
    Ok(())
}

async fn insert_audit(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    actor_user_id: Uuid,
    request_id: Uuid,
    event_type: &str,
    outcome: &str,
    metadata: serde_json::Value,
) -> Result<(), ObjectGovernanceError> {
    sqlx::query(
        "insert into audit_logs
            (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(actor_user_id)
    .bind(event_type)
    .bind(outcome)
    .bind(request_id)
    .bind(metadata)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

fn nonnegative_u64(value: i64) -> Result<u64, ObjectGovernanceError> {
    value
        .try_into()
        .map_err(|_| ObjectGovernanceError::InvalidStoredState)
}

fn require_one(rows: u64) -> Result<(), ObjectGovernanceError> {
    if rows == 1 {
        Ok(())
    } else {
        Err(ObjectGovernanceError::LeaseLost)
    }
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
    const SOURCE: &str = include_str!("object_governance.rs");

    #[test]
    fn jobs_have_server_controlled_typed_aggregate_columns_and_no_payload() {
        let production = SOURCE.split("#[cfg(test)]").next().unwrap();
        assert!(production.contains("object_governance_jobs"));
        assert!(production.contains("scan_run_id"));
        assert!(production.contains("quarantine_request_id"));
        assert!(!production.contains("payload"));
        assert!(!production.contains("delete from"));
    }

    #[test]
    fn quarantine_rechecks_every_safety_gate_after_worker_claim() {
        let eligibility = SOURCE
            .split("async fn lock_eligible_finding")
            .nth(1)
            .unwrap()
            .split("pub async fn load_quarantine_work")
            .next()
            .unwrap();
        assert!(eligibility.contains("import_files"));
        assert!(eligibility.contains("retention_until"));
        assert!(eligibility.contains("observed_sha256"));
        assert!(eligibility.contains("observed_size_bytes"));
        assert!(eligibility.contains("backend"));
        assert!(eligibility.contains("state"));
        assert!(eligibility.contains("\"orphan_object\""));
        assert!(eligibility.contains("Some(\"available\")"));
        assert!(!eligibility.contains("stale_temporary_object"));
        assert!(!eligibility.contains("commit_outcome_unknown"));
        assert!(!eligibility.contains(".tmp/"));
    }

    #[test]
    fn reports_only_mark_completed_unclaimed_orphans_as_quarantine_eligible() {
        let report = SOURCE
            .split("pub async fn get_report")
            .nth(1)
            .unwrap()
            .split("async fn load_scan")
            .next()
            .unwrap();
        assert!(report.contains("finding.finding_type = 'orphan_object'"));
        assert!(report.contains("finding.disposition_status = 'detected'"));
        assert!(report.contains("run.status = 'completed'"));
        assert!(report.contains("object.state = 'available'"));
        assert!(report.contains("request.status in ('queued', 'running')"));
        assert!(!report.contains("stale_temporary_object"));
        assert!(!report.contains("commit_outcome_unknown"));
        assert!(!report.contains("'.tmp/'"));
    }

    #[test]
    fn rename_recovery_uses_deterministic_finding_identity() {
        let production = SOURCE.split("#[cfg(test)]").next().unwrap();
        assert!(production.contains("on conflict (workspace_id, finding_id) do nothing"));
        assert!(!production.contains("remove_file"));
    }

    #[test]
    fn exhausted_and_terminal_failures_close_aggregates_with_audit() {
        let production = SOURCE.split("#[cfg(test)]").next().unwrap();
        assert!(production.contains("lease_attempts_exhausted"));
        assert!(production.contains("object.governance_dead_letter"));
        assert!(production.contains("object.governance_failed"));
        assert!(production.contains("set status = 'failed', finished_at = now()"));
    }
}
