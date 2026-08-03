use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use sqlx::{PgPool, Postgres, Row, Transaction};
use time::{Duration, OffsetDateTime};
use uuid::Uuid;

#[derive(Debug, Clone)]
pub struct ClaimedJob {
    pub id: Uuid,
    pub workspace_id: Uuid,
    pub job_type: String,
    pub aggregate_id: Uuid,
    pub attempt_count: i32,
    pub max_attempts: i32,
    pub lease_expires_at: OffsetDateTime,
    pub lease_generation: i64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ImportProgressEvent {
    pub event_seq: i64,
    pub event_type: String,
    pub status: String,
    pub processed_rows: i64,
    pub total_rows: i64,
    pub inserted_count: i64,
    pub updated_count: i64,
    pub skipped_count: i64,
    pub conflict_count: i64,
    pub error_code: Option<String>,
}

#[derive(Debug, Clone, Copy, Default)]
struct ImportCounters {
    processed: i64,
    total: i64,
    inserted: i64,
    updated: i64,
    skipped: i64,
    conflicts: i64,
}

#[derive(Debug, Default)]
struct ChangeSequence(i64);

impl ChangeSequence {
    fn next(&mut self) -> i64 {
        self.0 += 1;
        self.0
    }
}

#[derive(Debug)]
struct FormalProjectionChange {
    target_kind: &'static str,
    target_id: Uuid,
    operation: &'static str,
    before_json: Option<Value>,
    after_json: Value,
    target_row_version: i64,
}

#[derive(Debug, thiserror::Error)]
pub enum JobQueueError {
    #[error("database operation failed")]
    Database(#[from] sqlx::Error),
    #[error("job lease is no longer owned")]
    LeaseLost,
    #[error("unsupported job type")]
    UnsupportedJobType,
    #[error("frozen import state is invalid")]
    InvalidFrozenImport,
    #[error("abort policy encountered a conflict")]
    AbortConflict,
    #[error("an automatic source changed an existing immutable revision")]
    SourceRevisionConflict,
    #[error("event stream is not visible")]
    EventNotVisible,
    #[error("event cursor is invalid")]
    EventIdInvalid,
}

impl JobQueueError {
    pub fn retryable(&self) -> bool {
        match self {
            Self::Database(error) => error
                .as_database_error()
                .and_then(|error| error.code())
                .is_some_and(|code| matches!(code.as_ref(), "40001" | "40P01" | "53300" | "57P03")),
            _ => false,
        }
    }

    pub const fn code(&self) -> &'static str {
        match self {
            Self::Database(_) => "database_error",
            Self::LeaseLost => "lease_lost",
            Self::UnsupportedJobType => "unsupported_job_type",
            Self::InvalidFrozenImport => "invalid_frozen_import",
            Self::AbortConflict => "abort_conflict",
            Self::SourceRevisionConflict => "source_revision_conflict",
            Self::EventNotVisible => "event_not_visible",
            Self::EventIdInvalid => "event_id_invalid",
        }
    }
}

const CLAIM_CANDIDATE_SQL: &str = "select id, job_type, aggregate_id, status::text as status,
        attempt_count, max_attempts, available_at, lease_expires_at, lease_generation
    from job_queue
    where workspace_id = $1
      and job_type in ('import_confirm', 'import_rollback')
      and (
           (status = 'queued' and available_at <= now() and attempt_count < max_attempts)
           or (status = 'running' and lease_expires_at < now())
      )
    order by available_at, created_at, id
    for update skip locked
    limit 1";

pub async fn claim_next_import_job(
    pool: &PgPool,
    worker_id: &str,
    lease_seconds: i64,
    workspace_id: Uuid,
) -> Result<Option<ClaimedJob>, JobQueueError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    // Claims and reclaims are short job-first transactions. SKIP LOCKED
    // lets multiple workers select different jobs. Import confirmation
    // keeps its existing commit fence; rollback holds the same job-first
    // order for its intentionally atomic inverse transaction.
    let row = sqlx::query(CLAIM_CANDIDATE_SQL)
        .bind(workspace_id)
        .fetch_optional(&mut *tx)
        .await?;
    let Some(row) = row else {
        tx.commit().await?;
        return Ok(None);
    };
    let job_id: Uuid = row.get("id");
    let aggregate_id: Uuid = row.get("aggregate_id");
    let status: String = row.get("status");
    let attempt_count: i32 = row.get("attempt_count");
    let max_attempts: i32 = row.get("max_attempts");
    let available_at: OffsetDateTime = row.get("available_at");
    let current_expiry: Option<OffsetDateTime> = row.get("lease_expires_at");
    let now = OffsetDateTime::now_utc();
    let expired = current_expiry.is_some_and(|expires_at| expires_at < now);
    if status == "running" && expired && attempt_count >= max_attempts {
        let exhausted_job = ClaimedJob {
            id: row.get("id"),
            workspace_id,
            job_type: row.get("job_type"),
            aggregate_id,
            attempt_count,
            max_attempts,
            lease_expires_at: current_expiry.expect("expired running job has a lease"),
            lease_generation: row.get("lease_generation"),
        };
        sqlx::query(
            "update job_queue
                 set status = 'dead_letter', leased_by = null, lease_expires_at = null,
                     last_error_code = 'lease_attempts_exhausted', finished_at = now(),
                     updated_at = now()
                 where workspace_id = $1 and id = $2",
        )
        .bind(workspace_id)
        .bind(exhausted_job.id)
        .execute(&mut *tx)
        .await?;
        if exhausted_job.job_type == "import_confirm" {
            let actor_user_id = sqlx::query_scalar::<_, Uuid>(
                "update import_batches
                     set status = 'failed', updated_at = now()
                     where workspace_id = $1 and id = $2 and status = 'importing'
                     returning confirmed_by",
            )
            .bind(workspace_id)
            .bind(exhausted_job.aggregate_id)
            .fetch_optional(&mut *tx)
            .await?;
            append_event(
                &mut tx,
                workspace_id,
                exhausted_job.aggregate_id,
                exhausted_job.id,
                "dead_letter",
                "dead_letter",
                ImportCounters::default(),
                Some("lease_attempts_exhausted"),
            )
            .await?;
            if let Some(actor_user_id) = actor_user_id {
                insert_audit(
                    &mut tx,
                    &exhausted_job,
                    actor_user_id,
                    "import.worker_dead_letter",
                    "failure",
                    ImportCounters::default(),
                    Some("lease_attempts_exhausted"),
                    None,
                )
                .await?;
            }
        } else if exhausted_job.job_type == "import_rollback" {
            fail_exhausted_rollback(&mut tx, &exhausted_job).await?;
        }
        tx.commit().await?;
        return Ok(None);
    }
    let claimable = attempt_count < max_attempts
        && ((status == "queued" && available_at <= now) || (status == "running" && expired));
    if !claimable {
        tx.commit().await?;
        return Ok(None);
    }
    let job_type: String = row.get("job_type");
    let attempt_count = attempt_count + 1;
    let lease_generation = row.get::<i64, _>("lease_generation") + 1;
    let lease_expires_at = OffsetDateTime::now_utc() + Duration::seconds(lease_seconds);
    sqlx::query(
        "update job_queue
             set status = 'running', attempt_count = $1, leased_by = $2,
                 lease_expires_at = $3, lease_generation = $4, updated_at = now()
             where workspace_id = $5 and id = $6",
    )
    .bind(attempt_count)
    .bind(worker_id)
    .bind(lease_expires_at)
    .bind(lease_generation)
    .bind(workspace_id)
    .bind(job_id)
    .execute(&mut *tx)
    .await?;
    if job_type == "import_confirm" {
        sqlx::query(
            "update import_batches
             set status = case
                   when status = 'confirmed' then 'importing'::import_batch_status
                   else status
                 end,
                 updated_at = now()
             where workspace_id = $1 and id = $2
               and status in ('confirmed', 'importing')",
        )
        .bind(workspace_id)
        .bind(aggregate_id)
        .execute(&mut *tx)
        .await?;
        sqlx::query(
            "update extraction_jobs set status = 'running'
              where workspace_id = $1 and import_batch_id = $2 and status = 'queued'",
        )
        .bind(workspace_id)
        .bind(aggregate_id)
        .execute(&mut *tx)
        .await?;
        append_event(
            &mut tx,
            workspace_id,
            aggregate_id,
            job_id,
            "running",
            "running",
            ImportCounters::default(),
            None,
        )
        .await?;
    } else if job_type == "import_rollback" {
        let batch_updated = sqlx::query(
            "update import_batches
                    set status = case
                        when status = 'succeeded' then 'rollback_check'::import_batch_status
                        else status
                    end,
                        updated_at = now()
                  where workspace_id = $1 and id = $2
                    and status in ('succeeded', 'rollback_check', 'rolling_back')",
        )
        .bind(workspace_id)
        .bind(aggregate_id)
        .execute(&mut *tx)
        .await?
        .rows_affected();
        if batch_updated != 1 {
            return Err(JobQueueError::InvalidFrozenImport);
        }
        let rolling = sqlx::query(
            "update import_batches
                    set status = 'rolling_back', updated_at = now()
                  where workspace_id = $1 and id = $2
                    and status in ('rollback_check', 'rolling_back')",
        )
        .bind(workspace_id)
        .bind(aggregate_id)
        .execute(&mut *tx)
        .await?
        .rows_affected();
        if rolling != 1 {
            return Err(JobQueueError::InvalidFrozenImport);
        }
        let request_updated = sqlx::query(
            "update import_rollback_requests
                    set status = case when status = 'queued' then 'running' else status end,
                        updated_at = now()
                  where workspace_id = $1 and import_batch_id = $2 and job_id = $3
                    and status in ('queued', 'running')",
        )
        .bind(workspace_id)
        .bind(aggregate_id)
        .bind(job_id)
        .execute(&mut *tx)
        .await?
        .rows_affected();
        if request_updated != 1 {
            return Err(JobQueueError::InvalidFrozenImport);
        }
    }
    tx.commit().await?;
    Ok(Some(ClaimedJob {
        id: job_id,
        workspace_id,
        job_type,
        aggregate_id,
        attempt_count,
        max_attempts,
        lease_expires_at,
        lease_generation,
    }))
}

async fn fail_exhausted_rollback(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
) -> Result<(), JobQueueError> {
    let batch_updated = sqlx::query(
        "update import_batches
            set status = 'rollback_failed', updated_at = now()
          where workspace_id = $1 and id = $2 and status = 'rolling_back'",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .execute(&mut **tx)
    .await?
    .rows_affected();
    require_single_fenced_update(batch_updated)?;
    let actor_user_id = sqlx::query_scalar::<_, Uuid>(
        "update import_rollback_requests
            set status = 'failed', finished_at = now(), updated_at = now()
          where workspace_id = $1 and import_batch_id = $2 and job_id = $3
            and status = 'running'
          returning requested_by",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .bind(job.id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(JobQueueError::InvalidFrozenImport)?;
    append_event(
        tx,
        job.workspace_id,
        job.aggregate_id,
        job.id,
        "rollback_failed",
        "rollback_failed",
        ImportCounters::default(),
        Some("lease_attempts_exhausted"),
    )
    .await?;
    insert_audit(
        tx,
        job,
        actor_user_id,
        "import.rollback_worker_failed",
        "failure",
        ImportCounters::default(),
        Some("lease_attempts_exhausted"),
        None,
    )
    .await?;
    Ok(())
}

pub async fn renew_lease(
    pool: &PgPool,
    job: &ClaimedJob,
    worker_id: &str,
    lease_seconds: i64,
) -> Result<OffsetDateTime, JobQueueError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, job.workspace_id).await?;
    let lease_expires_at = OffsetDateTime::now_utc() + Duration::seconds(lease_seconds);
    let affected = sqlx::query(
        "update job_queue
         set lease_expires_at = $1, updated_at = now()
         where workspace_id = $2 and id = $3 and status = 'running' and leased_by = $4
           and lease_generation = $5",
    )
    .bind(lease_expires_at)
    .bind(job.workspace_id)
    .bind(job.id)
    .bind(worker_id)
    .bind(job.lease_generation)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    if affected != 1 {
        return Err(JobQueueError::LeaseLost);
    }
    tx.commit().await?;
    Ok(lease_expires_at)
}

pub async fn execute_import_job(
    pool: &PgPool,
    job: &ClaimedJob,
    worker_id: &str,
) -> Result<(), JobQueueError> {
    if job.job_type != "import_confirm" {
        return Err(JobQueueError::UnsupportedJobType);
    }
    if is_automatic_import(pool, job).await? {
        return execute_automatic_import_job(pool, job, worker_id).await;
    }
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, job.workspace_id).await?;
    let lease = sqlx::query(
        "select status, leased_by, lease_expires_at, lease_generation
         from job_queue
         where workspace_id = $1 and id = $2",
    )
    .bind(job.workspace_id)
    .bind(job.id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(lease) = lease else {
        return Err(JobQueueError::LeaseLost);
    };
    if !lease_allows_execution(
        lease.get("status"),
        lease.get::<Option<String>, _>("leased_by").as_deref(),
        lease.get("lease_expires_at"),
        lease.get("lease_generation"),
        job.lease_generation,
        worker_id,
        OffsetDateTime::now_utc(),
    ) {
        return Err(JobQueueError::LeaseLost);
    }

    let batch = sqlx::query(
        "select status::text as status, dataset_type, conflict_policy, validation_version,
                validated_mapping_id, validated_staging_version, staging_version, confirmed_by,
                rollback_capability, change_log_version,
                (
                    select file.id
                      from import_files file
                     where file.workspace_id = import_batches.workspace_id
                       and file.import_batch_id = import_batches.id
                ) as source_file_id
         from import_batches
         where workspace_id = $1 and id = $2",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(JobQueueError::InvalidFrozenImport)?;
    let dataset_type: String = batch.get("dataset_type");
    let policy: Option<String> = batch.get("conflict_policy");
    let validation_version: Option<i32> = batch.get("validation_version");
    let validated_mapping_id: Option<Uuid> = batch.get("validated_mapping_id");
    let validated_staging_version: Option<i64> = batch.get("validated_staging_version");
    let staging_version: i64 = batch.get("staging_version");
    let actor_user_id = confirmed_actor(batch.get("confirmed_by"))?;
    let source_file_id = batch
        .get::<Option<Uuid>, _>("source_file_id")
        .ok_or(JobQueueError::InvalidFrozenImport)?;
    if batch.get::<String, _>("status") != "importing"
        || dataset_type != "generic"
        || policy.is_none()
        || validation_version.is_none()
        || validated_mapping_id.is_none()
        || validated_staging_version != Some(staging_version)
        || batch.get::<String, _>("rollback_capability") != "compensation_only"
        || batch.get::<Option<i32>, _>("change_log_version").is_some()
    {
        return Err(JobQueueError::InvalidFrozenImport);
    }
    let policy = policy.expect("checked");

    let rows = sqlx::query(
        "select id, row_number, business_key, record_data, is_file_duplicate
         from import_staging_rows
         where workspace_id = $1 and import_batch_id = $2
           and validation_version = $3 and business_key is not null and record_data is not null
         order by business_key, row_number, id",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .bind(validation_version)
    .fetch_all(&mut *tx)
    .await?;
    let mut counters = ImportCounters {
        total: rows.len() as i64,
        ..Default::default()
    };
    let mut change_sequence = ChangeSequence::default();

    if policy == "abort" {
        let duplicate = rows
            .iter()
            .any(|row| row.get::<bool, _>("is_file_duplicate"));
        let keys: Vec<String> = rows.iter().map(|row| row.get("business_key")).collect();
        let db_conflict = sqlx::query_scalar::<_, bool>(
            "select exists(
                select 1 from imported_records
                where workspace_id = $1 and dataset_type = $2
                  and business_key = any($3)
             )",
        )
        .bind(job.workspace_id)
        .bind(&dataset_type)
        .bind(&keys)
        .fetch_one(&mut *tx)
        .await?;
        if duplicate || db_conflict {
            return Err(JobQueueError::AbortConflict);
        }
    }

    let mut grouped: std::collections::BTreeMap<String, Vec<&sqlx::postgres::PgRow>> =
        std::collections::BTreeMap::new();
    for row in &rows {
        grouped
            .entry(row.get("business_key"))
            .or_default()
            .push(row);
    }
    for (business_key, candidates) in grouped {
        counters.processed += candidates.len() as i64;
        if policy == "keep_conflict" && candidates.len() > 1 {
            for row in candidates {
                insert_conflict_candidate(
                    &mut tx,
                    job,
                    &dataset_type,
                    &business_key,
                    row,
                    None,
                    "file_duplicate",
                )
                .await?;
                counters.conflicts += 1;
            }
            continue;
        }
        let candidate = if policy == "overwrite" {
            candidates.last().expect("group is not empty")
        } else {
            candidates.first().expect("group is not empty")
        };
        counters.skipped += (candidates.len() - 1) as i64;
        let existing = sqlx::query(
            "select id, record_data, source_import_batch_id, source_row_number, row_version
             from imported_records
             where workspace_id = $1 and dataset_type = $2 and business_key = $3
             for update",
        )
        .bind(job.workspace_id)
        .bind(&dataset_type)
        .bind(&business_key)
        .fetch_optional(&mut *tx)
        .await?;
        match (policy.as_str(), existing) {
            ("skip", Some(_)) => counters.skipped += 1,
            ("keep_conflict", Some(existing_id)) => {
                insert_conflict_candidate(
                    &mut tx,
                    job,
                    &dataset_type,
                    &business_key,
                    candidate,
                    Some(existing_id.get("id")),
                    "database_conflict",
                )
                .await?;
                counters.conflicts += 1;
            }
            ("overwrite", Some(existing_id)) => {
                let before = imported_record_snapshot(&existing_id);
                let updated = sqlx::query(
                    "update imported_records
                     set record_data = $1, source_import_batch_id = $2,
                         source_row_number = $3, row_version = row_version + 1,
                         updated_at = now()
                     where workspace_id = $4 and id = $5
                     returning id, record_data, source_import_batch_id,
                               source_row_number, row_version",
                )
                .bind(candidate.get::<Value, _>("record_data"))
                .bind(job.aggregate_id)
                .bind(candidate.get::<i32, _>("row_number"))
                .bind(job.workspace_id)
                .bind(existing_id.get::<Uuid, _>("id"))
                .fetch_one(&mut *tx)
                .await?;
                append_row_change(
                    &mut tx,
                    job,
                    change_sequence.next(),
                    updated.get("id"),
                    "update",
                    Some(before),
                    imported_record_snapshot(&updated),
                    updated.get("row_version"),
                    source_file_id,
                    candidate.get("row_number"),
                )
                .await?;
                counters.updated += 1;
            }
            (_, None) => {
                let record_data = candidate.get::<Value, _>("record_data");
                let row_number = candidate.get::<i32, _>("row_number");
                if policy == "overwrite" {
                    let inserted = sqlx::query(
                        "insert into imported_records
                           (id, workspace_id, dataset_type, business_key, record_data,
                            source_import_batch_id, source_row_number, row_version, created_by)
                         values ($1, $2, $3, $4, $5, $6, $7, 1, $8)
                         on conflict (workspace_id, dataset_type, business_key)
                         do nothing
                         returning id, record_data, source_import_batch_id,
                                   source_row_number, row_version",
                    )
                    .bind(Uuid::now_v7())
                    .bind(job.workspace_id)
                    .bind(&dataset_type)
                    .bind(&business_key)
                    .bind(&record_data)
                    .bind(job.aggregate_id)
                    .bind(row_number)
                    .bind(actor_user_id)
                    .fetch_optional(&mut *tx)
                    .await?;
                    if let Some(inserted) = inserted {
                        append_row_change(
                            &mut tx,
                            job,
                            change_sequence.next(),
                            inserted.get("id"),
                            "insert",
                            None,
                            imported_record_snapshot(&inserted),
                            inserted.get("row_version"),
                            source_file_id,
                            row_number,
                        )
                        .await?;
                        counters.inserted += 1;
                    } else {
                        // INSERT ... DO NOTHING waits for a concurrent winner.
                        // Lock and read that exact committed row before updating
                        // it so the change log never guesses the overwritten state.
                        let existing = sqlx::query(
                            "select id, record_data, source_import_batch_id,
                                    source_row_number, row_version
                             from imported_records
                             where workspace_id = $1 and dataset_type = $2
                               and business_key = $3
                             for update",
                        )
                        .bind(job.workspace_id)
                        .bind(&dataset_type)
                        .bind(&business_key)
                        .fetch_one(&mut *tx)
                        .await?;
                        let before = imported_record_snapshot(&existing);
                        let updated = sqlx::query(
                            "update imported_records
                             set record_data = $1, source_import_batch_id = $2,
                                 source_row_number = $3, row_version = row_version + 1,
                                 updated_at = now()
                             where workspace_id = $4 and id = $5
                             returning id, record_data, source_import_batch_id,
                                       source_row_number, row_version",
                        )
                        .bind(&record_data)
                        .bind(job.aggregate_id)
                        .bind(row_number)
                        .bind(job.workspace_id)
                        .bind(existing.get::<Uuid, _>("id"))
                        .fetch_one(&mut *tx)
                        .await?;
                        append_row_change(
                            &mut tx,
                            job,
                            change_sequence.next(),
                            updated.get("id"),
                            "update",
                            Some(before),
                            imported_record_snapshot(&updated),
                            updated.get("row_version"),
                            source_file_id,
                            row_number,
                        )
                        .await?;
                        counters.updated += 1;
                    }
                } else {
                    let inserted = sqlx::query(
                        "insert into imported_records
                           (id, workspace_id, dataset_type, business_key, record_data,
                            source_import_batch_id, source_row_number, row_version, created_by)
                         values ($1, $2, $3, $4, $5, $6, $7, 1, $8)
                         on conflict (workspace_id, dataset_type, business_key) do nothing
                         returning id, record_data, source_import_batch_id,
                                   source_row_number, row_version",
                    )
                    .bind(Uuid::now_v7())
                    .bind(job.workspace_id)
                    .bind(&dataset_type)
                    .bind(&business_key)
                    .bind(&record_data)
                    .bind(job.aggregate_id)
                    .bind(row_number)
                    .bind(actor_user_id)
                    .fetch_optional(&mut *tx)
                    .await?;
                    if let Some(inserted) = inserted {
                        append_row_change(
                            &mut tx,
                            job,
                            change_sequence.next(),
                            inserted.get("id"),
                            "insert",
                            None,
                            imported_record_snapshot(&inserted),
                            inserted.get("row_version"),
                            source_file_id,
                            row_number,
                        )
                        .await?;
                        counters.inserted += 1;
                    } else if policy == "skip" {
                        counters.skipped += 1;
                    } else if policy == "keep_conflict" {
                        let existing_id = sqlx::query_scalar::<_, Uuid>(
                            "select id from imported_records
                             where workspace_id = $1 and dataset_type = $2 and business_key = $3
                             for update",
                        )
                        .bind(job.workspace_id)
                        .bind(&dataset_type)
                        .bind(&business_key)
                        .fetch_one(&mut *tx)
                        .await?;
                        insert_conflict_candidate(
                            &mut tx,
                            job,
                            &dataset_type,
                            &business_key,
                            candidate,
                            Some(existing_id),
                            "database_conflict",
                        )
                        .await?;
                        counters.conflicts += 1;
                    } else {
                        return Err(JobQueueError::AbortConflict);
                    }
                }
            }
            ("abort", Some(_)) => return Err(JobQueueError::AbortConflict),
            _ => return Err(JobQueueError::InvalidFrozenImport),
        }
    }

    // Formal writes above are still uncommitted. The long phase held neither
    // the job nor batch row lock, so renewals and expired-lease reclaim stay
    // live. At the commit boundary use the global job -> batch order. A
    // reclaimed generation fences this transaction and rolls every formal
    // write back.
    let final_lease = sqlx::query(
        "select status, leased_by, lease_expires_at, lease_generation
         from job_queue
         where workspace_id = $1 and id = $2
         for update",
    )
    .bind(job.workspace_id)
    .bind(job.id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(final_lease) = final_lease else {
        return Err(JobQueueError::LeaseLost);
    };
    if !lease_allows_execution(
        final_lease.get("status"),
        final_lease.get::<Option<String>, _>("leased_by").as_deref(),
        final_lease.get("lease_expires_at"),
        final_lease.get("lease_generation"),
        job.lease_generation,
        worker_id,
        OffsetDateTime::now_utc(),
    ) {
        return Err(JobQueueError::LeaseLost);
    }
    let final_batch = sqlx::query(
        "select status::text as status, dataset_type, conflict_policy,
                validation_version, validated_mapping_id,
                validated_staging_version, staging_version, confirmed_by,
                rollback_capability, change_log_version
         from import_batches
         where workspace_id = $1 and id = $2
         for update",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(JobQueueError::InvalidFrozenImport)?;
    if final_batch.get::<String, _>("status") != "importing"
        || final_batch.get::<String, _>("dataset_type") != dataset_type
        || final_batch
            .get::<Option<String>, _>("conflict_policy")
            .as_deref()
            != Some(policy.as_str())
        || final_batch.get::<Option<i32>, _>("validation_version") != validation_version
        || final_batch.get::<Option<Uuid>, _>("validated_mapping_id") != validated_mapping_id
        || final_batch.get::<Option<i64>, _>("validated_staging_version")
            != validated_staging_version
        || final_batch.get::<i64, _>("staging_version") != staging_version
        || final_batch.get::<Option<Uuid>, _>("confirmed_by") != Some(actor_user_id)
        || final_batch.get::<String, _>("rollback_capability") != "compensation_only"
        || final_batch
            .get::<Option<i32>, _>("change_log_version")
            .is_some()
    {
        return Err(JobQueueError::InvalidFrozenImport);
    }

    let batch_updated = sqlx::query(
        "update import_batches
         set status = 'succeeded', processed_count = $1, imported_count = $2,
             skipped_count = $3, overwritten_count = $4, conflict_result_count = $5,
             rollback_capability = 'direct', change_log_version = 1,
             committed_at = now(), updated_at = now()
         where workspace_id = $6 and id = $7 and status = 'importing'",
    )
    .bind(counters.processed)
    .bind(counters.inserted)
    .bind(counters.skipped)
    .bind(counters.updated)
    .bind(counters.conflicts)
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    if batch_updated != 1 {
        return Err(JobQueueError::InvalidFrozenImport);
    }
    let job_updated = sqlx::query(
        "update job_queue
         set status = 'succeeded', leased_by = null, lease_expires_at = null,
             last_error_code = null, finished_at = now(), updated_at = now()
         where workspace_id = $1 and id = $2 and status = 'running' and leased_by = $3
           and lease_generation = $4",
    )
    .bind(job.workspace_id)
    .bind(job.id)
    .bind(worker_id)
    .bind(job.lease_generation)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    if job_updated != 1 {
        return Err(JobQueueError::LeaseLost);
    }
    append_event(
        &mut tx,
        job.workspace_id,
        job.aggregate_id,
        job.id,
        "progress",
        "running",
        counters,
        None,
    )
    .await?;
    append_event(
        &mut tx,
        job.workspace_id,
        job.aggregate_id,
        job.id,
        "succeeded",
        "succeeded",
        counters,
        None,
    )
    .await?;
    insert_audit(
        &mut tx,
        job,
        actor_user_id,
        "import.worker_succeeded",
        "success",
        counters,
        None,
        Some(&policy),
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

async fn is_automatic_import(pool: &PgPool, job: &ClaimedJob) -> Result<bool, JobQueueError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, job.workspace_id).await?;
    let automatic = sqlx::query_scalar::<_, bool>(
        "select ingestion_mode = 'automatic'
           from import_batches where workspace_id = $1 and id = $2",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(JobQueueError::InvalidFrozenImport)?;
    tx.commit().await?;
    Ok(automatic)
}

async fn execute_automatic_import_job(
    pool: &PgPool,
    job: &ClaimedJob,
    worker_id: &str,
) -> Result<(), JobQueueError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, job.workspace_id).await?;
    let lease = sqlx::query(
        "select status, leased_by, lease_expires_at, lease_generation
           from job_queue where workspace_id = $1 and id = $2",
    )
    .bind(job.workspace_id)
    .bind(job.id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(JobQueueError::LeaseLost)?;
    if !lease_allows_execution(
        lease.get("status"),
        lease.get::<Option<String>, _>("leased_by").as_deref(),
        lease.get("lease_expires_at"),
        lease.get("lease_generation"),
        job.lease_generation,
        worker_id,
        OffsetDateTime::now_utc(),
    ) {
        return Err(JobQueueError::LeaseLost);
    }
    let batch = sqlx::query(
        "select status::text as status, ingestion_mode, dataset_type, conflict_policy,
                validation_version, validated_mapping_id, validated_staging_version,
                staging_version, confirmed_by, rollback_capability, change_log_version,
                data_source_id, collection_date,
                (select file.id from import_files file
                  where file.workspace_id = import_batches.workspace_id
                    and file.import_batch_id = import_batches.id) as source_file_id
           from import_batches where workspace_id = $1 and id = $2",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(JobQueueError::InvalidFrozenImport)?;
    let dataset_type: String = batch.get("dataset_type");
    let validation_version: Option<i32> = batch.get("validation_version");
    let mapping_id: Option<Uuid> = batch.get("validated_mapping_id");
    let staging_version: i64 = batch.get("staging_version");
    let actor_user_id = confirmed_actor(batch.get("confirmed_by"))?;
    let source_id = batch
        .get::<Option<Uuid>, _>("data_source_id")
        .ok_or(JobQueueError::InvalidFrozenImport)?;
    let source_file_id = batch
        .get::<Option<Uuid>, _>("source_file_id")
        .ok_or(JobQueueError::InvalidFrozenImport)?;
    if batch.get::<String, _>("status") != "importing"
        || batch.get::<String, _>("ingestion_mode") != "automatic"
        || !matches!(
            dataset_type.as_str(),
            "futures_catalog_v1"
                | "trading_calendar_v1"
                | "daily_market_prices_v1"
                | "seat_positions_v1"
        )
        || batch.get::<Option<String>, _>("conflict_policy").as_deref() != Some("skip")
        || validation_version.is_none()
        || mapping_id.is_none()
        || batch.get::<Option<i64>, _>("validated_staging_version") != Some(staging_version)
        || batch.get::<String, _>("rollback_capability") != "compensation_only"
        || batch.get::<Option<i32>, _>("change_log_version").is_some()
    {
        return Err(JobQueueError::InvalidFrozenImport);
    }
    let collection_date: time::Date = batch
        .get::<Option<time::Date>, _>("collection_date")
        .ok_or(JobQueueError::InvalidFrozenImport)?;
    let rows = sqlx::query(
        "select id, row_number, business_key, record_data, is_file_duplicate
           from import_staging_rows
          where workspace_id = $1 and import_batch_id = $2
            and validation_version = $3 and business_key is not null and record_data is not null
          order by business_key, row_number, id",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .bind(validation_version)
    .fetch_all(&mut *tx)
    .await?;
    let mut counters = ImportCounters {
        total: rows.len() as i64,
        ..Default::default()
    };
    let mut change_sequence = ChangeSequence::default();
    for row in rows {
        counters.processed += 1;
        if row.get::<bool, _>("is_file_duplicate") {
            counters.skipped += 1;
            continue;
        }
        let row_number: i32 = row.get("row_number");
        let staging_row_id: Uuid = row.get("id");
        let business_key: String = row.get("business_key");
        let record_data: Value = row.get("record_data");
        if let Some(row_date) = record_string(&record_data, "trade_date")
            && row_date != collection_date.to_string()
        {
            return Err(JobQueueError::InvalidFrozenImport);
        }
        if matches!(
            dataset_type.as_str(),
            "daily_market_prices_v1" | "seat_positions_v1"
        ) && resolve_contract(&mut tx, job.workspace_id, &record_data)
            .await?
            .is_none()
        {
            insert_unknown_contract(
                &mut tx,
                job,
                staging_row_id,
                row_number,
                staging_version,
                validation_version.expect("checked"),
                actor_user_id,
            )
            .await?;
            counters.skipped += 1;
            continue;
        }
        let existing = sqlx::query(
            "select id, record_data, source_import_batch_id, source_row_number, row_version
               from imported_records
              where workspace_id = $1 and dataset_type = $2 and business_key = $3
              for update",
        )
        .bind(job.workspace_id)
        .bind(&dataset_type)
        .bind(&business_key)
        .fetch_optional(&mut *tx)
        .await?;
        let (record_id, record_change) = if let Some(existing) = existing {
            if existing.get::<Value, _>("record_data") == record_data {
                counters.skipped += 1;
                continue;
            }
            if dataset_type != "futures_catalog_v1" {
                return Err(JobQueueError::SourceRevisionConflict);
            }
            let record_id = existing.get::<Uuid, _>("id");
            let before_json = imported_record_snapshot(&existing);
            let updated = sqlx::query(
                "update imported_records
                    set record_data = $1, source_import_batch_id = $2,
                        source_row_number = $3, row_version = row_version + 1,
                        updated_at = now()
                  where workspace_id = $4 and id = $5 and row_version = $6
                  returning id, record_data, source_import_batch_id,
                            source_row_number, row_version",
            )
            .bind(&record_data)
            .bind(job.aggregate_id)
            .bind(row_number)
            .bind(job.workspace_id)
            .bind(record_id)
            .bind(existing.get::<i64, _>("row_version"))
            .fetch_optional(&mut *tx)
            .await?
            .ok_or(JobQueueError::SourceRevisionConflict)?;
            counters.updated += 1;
            (
                record_id,
                FormalProjectionChange {
                    target_kind: "imported_record",
                    target_id: record_id,
                    operation: "update",
                    before_json: Some(before_json),
                    after_json: imported_record_snapshot(&updated),
                    target_row_version: updated.get("row_version"),
                },
            )
        } else {
            let record_id = Uuid::now_v7();
            let inserted = sqlx::query(
                "insert into imported_records
                (id, workspace_id, dataset_type, business_key, record_data,
                 source_import_batch_id, source_row_number, row_version, created_by)
             values ($1, $2, $3, $4, $5, $6, $7, 1, $8)
             on conflict (workspace_id, dataset_type, business_key) do nothing
             returning id, record_data, source_import_batch_id, source_row_number, row_version",
            )
            .bind(record_id)
            .bind(job.workspace_id)
            .bind(&dataset_type)
            .bind(&business_key)
            .bind(&record_data)
            .bind(job.aggregate_id)
            .bind(row_number)
            .bind(actor_user_id)
            .fetch_optional(&mut *tx)
            .await?;
            let Some(inserted) = inserted else {
                return Err(JobQueueError::SourceRevisionConflict);
            };
            counters.inserted += 1;
            (
                record_id,
                FormalProjectionChange {
                    target_kind: "imported_record",
                    target_id: record_id,
                    operation: "insert",
                    before_json: None,
                    after_json: imported_record_snapshot(&inserted),
                    target_row_version: 1,
                },
            )
        };
        append_row_change(
            &mut tx,
            job,
            change_sequence.next(),
            record_change.target_id,
            record_change.operation,
            record_change.before_json,
            record_change.after_json,
            record_change.target_row_version,
            source_file_id,
            row_number,
        )
        .await?;
        let projection_changes = match dataset_type.as_str() {
            "futures_catalog_v1" => {
                insert_catalog_projection(&mut tx, job.workspace_id, record_id, &record_data)
                    .await?
            }
            "trading_calendar_v1" => {
                insert_calendar_projection(
                    &mut tx,
                    job,
                    actor_user_id,
                    source_id,
                    record_id,
                    row_number,
                    &record_data,
                )
                .await?
            }
            "daily_market_prices_v1" => {
                insert_market_projection(
                    &mut tx,
                    job,
                    source_id,
                    record_id,
                    row_number,
                    &record_data,
                )
                .await?
            }
            "seat_positions_v1" => {
                insert_seat_projection(&mut tx, job, source_id, record_id, row_number, &record_data)
                    .await?
            }
            _ => return Err(JobQueueError::InvalidFrozenImport),
        };
        for change in projection_changes {
            append_projection_change(
                &mut tx,
                job,
                change_sequence.next(),
                change,
                source_file_id,
                row_number,
            )
            .await?;
        }
    }

    let final_lease = sqlx::query(
        "select status, leased_by, lease_expires_at, lease_generation
           from job_queue where workspace_id = $1 and id = $2 for update",
    )
    .bind(job.workspace_id)
    .bind(job.id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(JobQueueError::LeaseLost)?;
    if !lease_allows_execution(
        final_lease.get("status"),
        final_lease.get::<Option<String>, _>("leased_by").as_deref(),
        final_lease.get("lease_expires_at"),
        final_lease.get("lease_generation"),
        job.lease_generation,
        worker_id,
        OffsetDateTime::now_utc(),
    ) {
        return Err(JobQueueError::LeaseLost);
    }
    let batch_updated = sqlx::query(
        "update import_batches
            set status = 'succeeded', processed_count = $1, imported_count = $2,
                skipped_count = $3, overwritten_count = $4, conflict_result_count = 0,
                rollback_capability = 'direct', change_log_version = 2,
                committed_at = now(), updated_at = now()
          where workspace_id = $5 and id = $6 and status = 'importing'
            and ingestion_mode = 'automatic' and dataset_type = $7
            and validated_mapping_id = $8 and validated_staging_version = $9",
    )
    .bind(counters.processed)
    .bind(counters.inserted)
    .bind(counters.skipped)
    .bind(counters.updated)
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .bind(&dataset_type)
    .bind(mapping_id)
    .bind(staging_version)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    require_single_fenced_update(batch_updated)?;
    let job_updated = sqlx::query(
        "update job_queue
            set status = 'succeeded', leased_by = null, lease_expires_at = null,
                last_error_code = null, finished_at = now(), updated_at = now()
          where workspace_id = $1 and id = $2 and status = 'running' and leased_by = $3
            and lease_generation = $4",
    )
    .bind(job.workspace_id)
    .bind(job.id)
    .bind(worker_id)
    .bind(job.lease_generation)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    if job_updated != 1 {
        return Err(JobQueueError::LeaseLost);
    }
    sqlx::query(
        "update extraction_jobs
            set status = 'succeeded', completed_at = now(), stable_error_code = null
          where workspace_id = $1 and import_batch_id = $2 and status in ('queued', 'running')",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .execute(&mut *tx)
    .await?;
    append_event(
        &mut tx,
        job.workspace_id,
        job.aggregate_id,
        job.id,
        "succeeded",
        "succeeded",
        counters,
        None,
    )
    .await?;
    insert_audit(
        &mut tx,
        job,
        actor_user_id,
        "import.worker_succeeded",
        "success",
        counters,
        None,
        Some("skip"),
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

fn record_string(record: &Value, field: &str) -> Option<String> {
    record
        .get(field)
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}

fn required_string(record: &Value, field: &str) -> Result<String, JobQueueError> {
    record_string(record, field).ok_or(JobQueueError::InvalidFrozenImport)
}

async fn resolve_exchange(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    record: &Value,
) -> Result<Option<Uuid>, JobQueueError> {
    Ok(sqlx::query_scalar::<_, Uuid>(
        "select id from exchanges where workspace_id = $1 and code = $2",
    )
    .bind(workspace_id)
    .bind(required_string(record, "exchange_code")?.to_ascii_uppercase())
    .fetch_optional(&mut **tx)
    .await?)
}

async fn resolve_contract(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    record: &Value,
) -> Result<Option<Uuid>, JobQueueError> {
    Ok(sqlx::query_scalar::<_, Uuid>(
        "select contract.id
           from contracts contract
           join instruments instrument on instrument.workspace_id = contract.workspace_id
                                      and instrument.id = contract.instrument_id
           join exchanges exchange on exchange.workspace_id = instrument.workspace_id
                                  and exchange.id = instrument.exchange_id
          where contract.workspace_id = $1 and contract.code = $2 and exchange.code = $3",
    )
    .bind(workspace_id)
    .bind(required_string(record, "contract_code")?.to_ascii_uppercase())
    .bind(required_string(record, "exchange_code")?.to_ascii_uppercase())
    .fetch_optional(&mut **tx)
    .await?)
}

async fn insert_catalog_projection(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    record_id: Uuid,
    record: &Value,
) -> Result<Vec<FormalProjectionChange>, JobQueueError> {
    let mut changes = Vec::new();
    let exchange_code = required_string(record, "exchange_code")?.to_ascii_uppercase();
    let exchange_name = required_string(record, "exchange_name")?;
    let timezone = required_string(record, "timezone")?;
    let existing_exchange = sqlx::query(
        "select id, row_version,
                to_jsonb(exchange_row) - 'workspace_id' - 'created_at' - 'updated_at' as snapshot
           from exchanges exchange_row
          where workspace_id = $1 and code = $2
          for update",
    )
    .bind(workspace_id)
    .bind(&exchange_code)
    .fetch_optional(&mut **tx)
    .await?;
    let exchange_id = if let Some(existing) = existing_exchange {
        let exchange_id: Uuid = existing.get("id");
        let before_json: Value = existing.get("snapshot");
        let updated = sqlx::query(
            "update exchanges exchange_row
                set name = $1, timezone = $2, source_record_id = $3,
                    row_version = row_version + 1, updated_at = now()
              where workspace_id = $4 and id = $5 and row_version = $6
                and (name, timezone) is distinct from ($1, $2)
              returning row_version,
                        to_jsonb(exchange_row) - 'workspace_id' - 'created_at' - 'updated_at' as snapshot",
        )
        .bind(&exchange_name)
        .bind(&timezone)
        .bind(record_id)
        .bind(workspace_id)
        .bind(exchange_id)
        .bind(existing.get::<i64, _>("row_version"))
        .fetch_optional(&mut **tx)
        .await?;
        if let Some(updated) = updated {
            changes.push(FormalProjectionChange {
                target_kind: "exchange",
                target_id: exchange_id,
                operation: "update",
                before_json: Some(before_json),
                after_json: updated.get("snapshot"),
                target_row_version: updated.get("row_version"),
            });
        }
        exchange_id
    } else {
        let exchange_id = Uuid::now_v7();
        let inserted = sqlx::query(
            "insert into exchanges (id, workspace_id, code, name, timezone, source_record_id)
         values ($1, $2, $3, $4, $5, $6)
         returning row_version,
                   to_jsonb(exchanges) - 'workspace_id' - 'created_at' - 'updated_at' as snapshot",
        )
        .bind(exchange_id)
        .bind(workspace_id)
        .bind(&exchange_code)
        .bind(&exchange_name)
        .bind(&timezone)
        .bind(record_id)
        .fetch_one(&mut **tx)
        .await?;
        changes.push(FormalProjectionChange {
            target_kind: "exchange",
            target_id: exchange_id,
            operation: "insert",
            before_json: None,
            after_json: inserted.get("snapshot"),
            target_row_version: inserted.get("row_version"),
        });
        exchange_id
    };
    let instrument_code = required_string(record, "instrument_code")?.to_ascii_uppercase();
    let instrument_name = required_string(record, "instrument_name")?;
    let currency_code = required_string(record, "currency_code")?.to_ascii_uppercase();
    let multiplier = record_string(record, "contract_multiplier").unwrap_or_default();
    let tick = record_string(record, "price_tick").unwrap_or_default();
    let existing_instrument = sqlx::query(
        "select id, row_version,
                to_jsonb(instrument_row) - 'workspace_id' - 'created_at' - 'updated_at' as snapshot
           from instruments instrument_row
          where workspace_id = $1 and exchange_id = $2 and code = $3
          for update",
    )
    .bind(workspace_id)
    .bind(exchange_id)
    .bind(&instrument_code)
    .fetch_optional(&mut **tx)
    .await?;
    let instrument_id = if let Some(existing) = existing_instrument {
        let instrument_id: Uuid = existing.get("id");
        let before_json: Value = existing.get("snapshot");
        let updated = sqlx::query(
            "update instruments instrument_row
                set name = $1, currency_code = $2,
                    contract_multiplier = coalesce(nullif($3, '')::numeric, contract_multiplier),
                    price_tick = coalesce(nullif($4, '')::numeric, price_tick),
                    source_record_id = $5, row_version = row_version + 1, updated_at = now()
              where workspace_id = $6 and id = $7 and row_version = $8
                and (name, currency_code, contract_multiplier, price_tick) is distinct from
                    ($1, $2, coalesce(nullif($3, '')::numeric, contract_multiplier),
                     coalesce(nullif($4, '')::numeric, price_tick))
              returning row_version,
                        to_jsonb(instrument_row) - 'workspace_id' - 'created_at' - 'updated_at' as snapshot",
        )
        .bind(&instrument_name)
        .bind(&currency_code)
        .bind(&multiplier)
        .bind(&tick)
        .bind(record_id)
        .bind(workspace_id)
        .bind(instrument_id)
        .bind(existing.get::<i64, _>("row_version"))
        .fetch_optional(&mut **tx)
        .await?;
        if let Some(updated) = updated {
            changes.push(FormalProjectionChange {
                target_kind: "instrument",
                target_id: instrument_id,
                operation: "update",
                before_json: Some(before_json),
                after_json: updated.get("snapshot"),
                target_row_version: updated.get("row_version"),
            });
        }
        instrument_id
    } else {
        let instrument_id = Uuid::now_v7();
        let inserted = sqlx::query(
        "insert into instruments
            (id, workspace_id, exchange_id, code, name, currency_code,
             contract_multiplier, price_tick, source_record_id)
         values ($1, $2, $3, $4, $5, $6,
                 nullif($7, '')::numeric, nullif($8, '')::numeric, $9)
         returning row_version,
                   to_jsonb(instruments) - 'workspace_id' - 'created_at' - 'updated_at' as snapshot",
        )
        .bind(instrument_id)
        .bind(workspace_id)
        .bind(exchange_id)
        .bind(&instrument_code)
        .bind(&instrument_name)
        .bind(&currency_code)
        .bind(&multiplier)
        .bind(&tick)
        .bind(record_id)
        .fetch_one(&mut **tx)
        .await?;
        changes.push(FormalProjectionChange {
            target_kind: "instrument",
            target_id: instrument_id,
            operation: "insert",
            before_json: None,
            after_json: inserted.get("snapshot"),
            target_row_version: inserted.get("row_version"),
        });
        instrument_id
    };
    let contract_code = required_string(record, "contract_code")?.to_ascii_uppercase();
    let delivery_month = record_string(record, "delivery_month").unwrap_or_default();
    let listed_at = record_string(record, "listed_at").unwrap_or_default();
    let expires_at = record_string(record, "expires_at").unwrap_or_default();
    let existing_contract = sqlx::query(
        "select id, row_version,
                to_jsonb(contract_row) - 'workspace_id' - 'created_at' - 'updated_at' as snapshot
           from contracts contract_row
          where workspace_id = $1 and instrument_id = $2 and code = $3
          for update",
    )
    .bind(workspace_id)
    .bind(instrument_id)
    .bind(&contract_code)
    .fetch_optional(&mut **tx)
    .await?;
    if let Some(existing) = existing_contract {
        let contract_id: Uuid = existing.get("id");
        let before_json: Value = existing.get("snapshot");
        let updated = sqlx::query(
            "update contracts contract_row
                set delivery_month = coalesce(nullif($1, ''), delivery_month),
                    listed_at = coalesce(nullif($2, '')::date, listed_at),
                    expires_at = coalesce(nullif($3, '')::date, expires_at),
                    source_record_id = $4, row_version = row_version + 1, updated_at = now()
              where workspace_id = $5 and id = $6 and row_version = $7
                and (delivery_month, listed_at, expires_at) is distinct from
                    (coalesce(nullif($1, ''), delivery_month),
                     coalesce(nullif($2, '')::date, listed_at),
                     coalesce(nullif($3, '')::date, expires_at))
              returning row_version,
                        to_jsonb(contract_row) - 'workspace_id' - 'created_at' - 'updated_at' as snapshot",
        )
        .bind(&delivery_month)
        .bind(&listed_at)
        .bind(&expires_at)
        .bind(record_id)
        .bind(workspace_id)
        .bind(contract_id)
        .bind(existing.get::<i64, _>("row_version"))
        .fetch_optional(&mut **tx)
        .await?;
        if let Some(updated) = updated {
            changes.push(FormalProjectionChange {
                target_kind: "contract",
                target_id: contract_id,
                operation: "update",
                before_json: Some(before_json),
                after_json: updated.get("snapshot"),
                target_row_version: updated.get("row_version"),
            });
        }
    } else {
        let contract_id = Uuid::now_v7();
        let inserted = sqlx::query(
            "insert into contracts
            (id, workspace_id, instrument_id, code, delivery_month, listed_at,
             expires_at, source_record_id)
         values ($1, $2, $3, $4, nullif($5, ''), nullif($6, '')::date,
                 nullif($7, '')::date, $8)
         returning row_version,
                   to_jsonb(contracts) - 'workspace_id' - 'created_at' - 'updated_at' as snapshot",
        )
        .bind(contract_id)
        .bind(workspace_id)
        .bind(instrument_id)
        .bind(&contract_code)
        .bind(&delivery_month)
        .bind(&listed_at)
        .bind(&expires_at)
        .bind(record_id)
        .fetch_one(&mut **tx)
        .await?;
        changes.push(FormalProjectionChange {
            target_kind: "contract",
            target_id: contract_id,
            operation: "insert",
            before_json: None,
            after_json: inserted.get("snapshot"),
            target_row_version: inserted.get("row_version"),
        });
    }
    Ok(changes)
}

async fn insert_calendar_projection(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
    actor_user_id: Uuid,
    source_id: Uuid,
    record_id: Uuid,
    row_number: i32,
    record: &Value,
) -> Result<Vec<FormalProjectionChange>, JobQueueError> {
    let exchange_id = resolve_exchange(tx, job.workspace_id, record)
        .await?
        .ok_or(JobQueueError::InvalidFrozenImport)?;
    let version_id = Uuid::now_v7();
    let version = sqlx::query(
        "insert into trading_calendar_versions
            (id, workspace_id, exchange_id, version, source_id, effective_from,
             created_by, source_record_id)
         values ($1, $2, $3, $4, $5, $6::date, $7, $8)
         returning row_version,
                   to_jsonb(trading_calendar_versions) - 'workspace_id' - 'created_at' as snapshot",
    )
    .bind(version_id)
    .bind(job.workspace_id)
    .bind(exchange_id)
    .bind(required_string(record, "calendar_version")?)
    .bind(source_id)
    .bind(required_string(record, "effective_from")?)
    .bind(actor_user_id)
    .bind(record_id)
    .fetch_one(&mut **tx)
    .await?;
    let day_json: Value = serde_json::from_str(&required_string(record, "day_session_json")?)
        .map_err(|_| JobQueueError::InvalidFrozenImport)?;
    let night_json: Value = serde_json::from_str(&required_string(record, "night_session_json")?)
        .map_err(|_| JobQueueError::InvalidFrozenImport)?;
    let is_trading_day = required_string(record, "is_trading_day")?
        .parse::<bool>()
        .map_err(|_| JobQueueError::InvalidFrozenImport)?;
    let day = sqlx::query(
        "insert into trading_calendar_days
            (workspace_id, calendar_version_id, trade_date, is_trading_day,
             day_session_json, night_session_json, source_import_batch_id,
             source_row_number, source_record_id)
         values ($1, $2, $3::date, $4, $5, $6, $7, $8, $9)
         returning row_version,
                   to_jsonb(trading_calendar_days) - 'workspace_id' - 'created_at' as snapshot",
    )
    .bind(job.workspace_id)
    .bind(version_id)
    .bind(required_string(record, "trade_date")?)
    .bind(is_trading_day)
    .bind(day_json)
    .bind(night_json)
    .bind(job.aggregate_id)
    .bind(row_number)
    .bind(record_id)
    .fetch_one(&mut **tx)
    .await?;
    Ok(vec![
        FormalProjectionChange {
            target_kind: "trading_calendar_version",
            target_id: version_id,
            operation: "insert",
            before_json: None,
            after_json: version.get("snapshot"),
            target_row_version: version.get("row_version"),
        },
        FormalProjectionChange {
            target_kind: "trading_calendar_day",
            target_id: record_id,
            operation: "insert",
            before_json: None,
            after_json: day.get("snapshot"),
            target_row_version: day.get("row_version"),
        },
    ])
}

async fn insert_market_projection(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
    source_id: Uuid,
    record_id: Uuid,
    row_number: i32,
    record: &Value,
) -> Result<Vec<FormalProjectionChange>, JobQueueError> {
    let contract_id = resolve_contract(tx, job.workspace_id, record)
        .await?
        .ok_or(JobQueueError::InvalidFrozenImport)?;
    let exchange_id = resolve_exchange(tx, job.workspace_id, record)
        .await?
        .ok_or(JobQueueError::InvalidFrozenImport)?;
    let calendar_id = sqlx::query_scalar::<_, Uuid>(
        "select id from trading_calendar_versions
          where workspace_id = $1 and exchange_id = $2 and version = $3 and source_id = $4",
    )
    .bind(job.workspace_id)
    .bind(exchange_id)
    .bind(required_string(record, "calendar_version")?)
    .bind(source_id)
    .fetch_optional(&mut **tx)
    .await?
    .ok_or(JobQueueError::InvalidFrozenImport)?;
    let inserted = sqlx::query(
        "insert into market_prices
            (workspace_id, source_id, contract_id, trade_date, session_type, observed_at,
             granularity, close_price, settlement_price, currency_code, calendar_version_id,
             revision_no, source_import_batch_id, source_row_number, source_record_id)
         values ($1, $2, $3, $4::date, $5, $6::timestamptz, $7,
                 nullif($8, '')::numeric, nullif($9, '')::numeric, $10, $11,
                 $12::integer, $13, $14, $15)
         returning row_version,
                   to_jsonb(market_prices) - 'workspace_id' - 'created_at' as snapshot",
    )
    .bind(job.workspace_id)
    .bind(source_id)
    .bind(contract_id)
    .bind(required_string(record, "trade_date")?)
    .bind(required_string(record, "session_type")?)
    .bind(required_string(record, "observed_at")?)
    .bind(required_string(record, "granularity")?)
    .bind(record_string(record, "close_price").unwrap_or_default())
    .bind(record_string(record, "settlement_price").unwrap_or_default())
    .bind(required_string(record, "currency_code")?.to_ascii_uppercase())
    .bind(calendar_id)
    .bind(required_string(record, "revision_no")?)
    .bind(job.aggregate_id)
    .bind(row_number)
    .bind(record_id)
    .fetch_one(&mut **tx)
    .await?;
    Ok(vec![FormalProjectionChange {
        target_kind: "market_price",
        target_id: record_id,
        operation: "insert",
        before_json: None,
        after_json: inserted.get("snapshot"),
        target_row_version: inserted.get("row_version"),
    }])
}

async fn insert_seat_projection(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
    source_id: Uuid,
    record_id: Uuid,
    row_number: i32,
    record: &Value,
) -> Result<Vec<FormalProjectionChange>, JobQueueError> {
    let contract_id = resolve_contract(tx, job.workspace_id, record)
        .await?
        .ok_or(JobQueueError::InvalidFrozenImport)?;
    let seat_name = required_string(record, "seat_name")?;
    let existing_seat = sqlx::query(
        "select id from seat_entities
          where workspace_id = $1 and canonical_name = $2
          for update",
    )
    .bind(job.workspace_id)
    .bind(&seat_name)
    .fetch_optional(&mut **tx)
    .await?;
    let (seat_id, seat_change) = if let Some(existing) = existing_seat {
        (existing.get("id"), None)
    } else {
        let seat_id = Uuid::now_v7();
        let inserted = sqlx::query(
        "insert into seat_entities
            (id, workspace_id, canonical_name, status, source_record_id)
         values ($1, $2, $3, 'unreviewed', $4)
         returning row_version,
                   to_jsonb(seat_entities) - 'workspace_id' - 'created_at' - 'updated_at' as snapshot",
        )
        .bind(seat_id)
        .bind(job.workspace_id)
        .bind(&seat_name)
        .bind(record_id)
        .fetch_one(&mut **tx)
        .await?;
        (
            seat_id,
            Some(FormalProjectionChange {
                target_kind: "seat_entity",
                target_id: seat_id,
                operation: "insert",
                before_json: None,
                after_json: inserted.get("snapshot"),
                target_row_version: inserted.get("row_version"),
            }),
        )
    };
    let position = sqlx::query(
        "insert into seat_positions
            (workspace_id, trade_date, contract_id, seat_id, rank_type, rank,
             volume, long_position, short_position, source_id, source_import_batch_id,
             source_row_number, source_record_id)
         values ($1, $2::date, $3, $4, $5, $6::integer,
                 nullif($7, '')::bigint, nullif($8, '')::bigint, nullif($9, '')::bigint,
                 $10, $11, $12, $13)
         returning row_version,
                   to_jsonb(seat_positions) - 'workspace_id' - 'created_at' as snapshot",
    )
    .bind(job.workspace_id)
    .bind(required_string(record, "trade_date")?)
    .bind(contract_id)
    .bind(seat_id)
    .bind(required_string(record, "rank_type")?)
    .bind(required_string(record, "rank")?)
    .bind(record_string(record, "volume").unwrap_or_default())
    .bind(record_string(record, "long_position").unwrap_or_default())
    .bind(record_string(record, "short_position").unwrap_or_default())
    .bind(source_id)
    .bind(job.aggregate_id)
    .bind(row_number)
    .bind(record_id)
    .fetch_one(&mut **tx)
    .await?;
    let mut changes = Vec::with_capacity(2);
    if let Some(change) = seat_change {
        changes.push(change);
    }
    changes.push(FormalProjectionChange {
        target_kind: "seat_position",
        target_id: record_id,
        operation: "insert",
        before_json: None,
        after_json: position.get("snapshot"),
        target_row_version: position.get("row_version"),
    });
    Ok(changes)
}

async fn insert_unknown_contract(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
    staging_row_id: Uuid,
    row_number: i32,
    staging_version: i64,
    validation_version: i32,
    actor_user_id: Uuid,
) -> Result<(), JobQueueError> {
    sqlx::query(
        "insert into import_errors
            (id, workspace_id, import_batch_id, staging_row_id, row_number,
             field_name, severity, error_code, raw_value, message, created_by,
             staging_version, validation_version, error_kind)
         values ($1, $2, $3, $4, $5, 'contract_code', 'error', 'unknown_contract',
                 null, '合约未在目录中建档，已跳过本行', $6, $7, $8, 'validation')",
    )
    .bind(Uuid::now_v7())
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .bind(staging_row_id)
    .bind(row_number)
    .bind(actor_user_id)
    .bind(staging_version)
    .bind(validation_version)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

pub async fn record_job_failure(
    pool: &PgPool,
    job: &ClaimedJob,
    worker_id: &str,
    error: &JobQueueError,
) -> Result<(), JobQueueError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, job.workspace_id).await?;
    let lease = sqlx::query(
        "select status, leased_by, lease_expires_at, lease_generation
         from job_queue
         where workspace_id = $1 and id = $2
         for update",
    )
    .bind(job.workspace_id)
    .bind(job.id)
    .fetch_optional(&mut *tx)
    .await?;
    let Some(lease) = lease else {
        return Err(JobQueueError::LeaseLost);
    };
    if !lease_allows_execution(
        lease.get("status"),
        lease.get::<Option<String>, _>("leased_by").as_deref(),
        lease.get("lease_expires_at"),
        lease.get("lease_generation"),
        job.lease_generation,
        worker_id,
        OffsetDateTime::now_utc(),
    ) {
        return Err(JobQueueError::LeaseLost);
    }
    let retry = error.retryable() && job.attempt_count < job.max_attempts;
    let terminal_status = if error.retryable() && job.attempt_count >= job.max_attempts {
        "dead_letter"
    } else {
        "failed"
    };
    if retry {
        let delay_seconds = (2_i64.pow(job.attempt_count.clamp(1, 5) as u32)).min(60);
        let updated = sqlx::query(
            "update job_queue
             set status = 'queued', available_at = now() + make_interval(secs => $1),
                 leased_by = null, lease_expires_at = null, last_error_code = $2,
                 updated_at = now()
             where workspace_id = $3 and id = $4 and status = 'running' and leased_by = $5
               and lease_generation = $6",
        )
        .bind(delay_seconds as f64)
        .bind(error.code())
        .bind(job.workspace_id)
        .bind(job.id)
        .bind(worker_id)
        .bind(job.lease_generation)
        .execute(&mut *tx)
        .await?
        .rows_affected();
        require_single_fenced_update(updated)?;
        append_event(
            &mut tx,
            job.workspace_id,
            job.aggregate_id,
            job.id,
            "progress",
            "queued",
            ImportCounters::default(),
            Some(error.code()),
        )
        .await?;
    } else {
        let updated = sqlx::query(
            "update job_queue
             set status = $1, leased_by = null, lease_expires_at = null,
                 last_error_code = $2, finished_at = now(), updated_at = now()
             where workspace_id = $3 and id = $4 and status = 'running' and leased_by = $5
               and lease_generation = $6",
        )
        .bind(terminal_status)
        .bind(error.code())
        .bind(job.workspace_id)
        .bind(job.id)
        .bind(worker_id)
        .bind(job.lease_generation)
        .execute(&mut *tx)
        .await?
        .rows_affected();
        require_single_fenced_update(updated)?;
        if job.job_type == "import_confirm" {
            let actor_user_id = sqlx::query_scalar::<_, Uuid>(
                "update import_batches
                 set status = 'failed', updated_at = now()
                 where workspace_id = $1 and id = $2 and status = 'importing'
                 returning confirmed_by",
            )
            .bind(job.workspace_id)
            .bind(job.aggregate_id)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or(JobQueueError::InvalidFrozenImport)?;
            sqlx::query(
                "update extraction_jobs
                    set status = 'failed', stable_error_code = $1, completed_at = now()
                  where workspace_id = $2 and import_batch_id = $3
                    and status in ('queued', 'running')",
            )
            .bind(error.code())
            .bind(job.workspace_id)
            .bind(job.aggregate_id)
            .execute(&mut *tx)
            .await?;
            append_event(
                &mut tx,
                job.workspace_id,
                job.aggregate_id,
                job.id,
                terminal_status,
                terminal_status,
                ImportCounters::default(),
                Some(error.code()),
            )
            .await?;
            insert_audit(
                &mut tx,
                job,
                actor_user_id,
                if terminal_status == "dead_letter" {
                    "import.worker_dead_letter"
                } else {
                    "import.worker_failed"
                },
                "failure",
                ImportCounters::default(),
                Some(error.code()),
                None,
            )
            .await?;
        } else if job.job_type == "import_rollback" {
            let batch_updated = sqlx::query(
                "update import_batches
                    set status = 'rollback_failed', updated_at = now()
                  where workspace_id = $1 and id = $2 and status = 'rolling_back'",
            )
            .bind(job.workspace_id)
            .bind(job.aggregate_id)
            .execute(&mut *tx)
            .await?
            .rows_affected();
            require_single_fenced_update(batch_updated)?;
            let actor_user_id = sqlx::query_scalar::<_, Uuid>(
                "update import_rollback_requests
                    set status = 'failed', finished_at = now(), updated_at = now()
                  where workspace_id = $1 and import_batch_id = $2 and job_id = $3
                    and status = 'running'
                  returning requested_by",
            )
            .bind(job.workspace_id)
            .bind(job.aggregate_id)
            .bind(job.id)
            .fetch_optional(&mut *tx)
            .await?
            .ok_or(JobQueueError::InvalidFrozenImport)?;
            append_event(
                &mut tx,
                job.workspace_id,
                job.aggregate_id,
                job.id,
                "rollback_failed",
                "rollback_failed",
                ImportCounters::default(),
                Some(error.code()),
            )
            .await?;
            insert_audit(
                &mut tx,
                job,
                actor_user_id,
                "import.rollback_worker_failed",
                "failure",
                ImportCounters::default(),
                Some(error.code()),
                None,
            )
            .await?;
        }
    }
    tx.commit().await?;
    Ok(())
}

pub async fn list_events_after(
    pool: &PgPool,
    workspace_id: Uuid,
    import_id: Uuid,
    after: i64,
) -> Result<Vec<ImportProgressEvent>, JobQueueError> {
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, workspace_id).await?;
    let visible = sqlx::query_scalar::<_, bool>(
        "select exists(select 1 from import_batches where workspace_id = $1 and id = $2)",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_one(&mut *tx)
    .await?;
    if !visible {
        return Err(JobQueueError::EventNotVisible);
    }
    let max = sqlx::query_scalar::<_, i64>(
        "select coalesce(max(event_seq), 0) from import_job_events
         where workspace_id = $1 and import_batch_id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_one(&mut *tx)
    .await?;
    if after < 0 || after > max {
        return Err(JobQueueError::EventIdInvalid);
    }
    let rows = sqlx::query(
        "select event_seq, event_type, payload
         from import_job_events
         where workspace_id = $1 and import_batch_id = $2 and event_seq > $3
         order by event_seq",
    )
    .bind(workspace_id)
    .bind(import_id)
    .bind(after)
    .fetch_all(&mut *tx)
    .await?;
    tx.commit().await?;
    rows.into_iter()
        .map(|row| {
            let payload: Value = row.get("payload");
            Ok(ImportProgressEvent {
                event_seq: row.get("event_seq"),
                event_type: row.get("event_type"),
                status: payload
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or("running")
                    .to_string(),
                processed_rows: number(&payload, "processed_rows"),
                total_rows: number(&payload, "total_rows"),
                inserted_count: number(&payload, "inserted_count"),
                updated_count: number(&payload, "updated_count"),
                skipped_count: number(&payload, "skipped_count"),
                conflict_count: number(&payload, "conflict_count"),
                error_code: payload
                    .get("error_code")
                    .and_then(Value::as_str)
                    .map(str::to_string),
            })
        })
        .collect()
}

fn imported_record_snapshot(row: &sqlx::postgres::PgRow) -> Value {
    imported_record_snapshot_values(
        row.get("record_data"),
        row.get("source_import_batch_id"),
        row.get("source_row_number"),
        row.get("row_version"),
    )
}

fn imported_record_snapshot_values(
    record_data: Value,
    source_import_batch_id: Uuid,
    source_row_number: i32,
    row_version: i64,
) -> Value {
    json!({
        "record_data": record_data,
        "source_import_batch_id": source_import_batch_id,
        "source_row_number": source_row_number,
        "row_version": row_version,
    })
}

#[allow(clippy::too_many_arguments)]
async fn append_row_change(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
    sequence_no: i64,
    target_id: Uuid,
    operation: &str,
    before_json: Option<Value>,
    after_json: Value,
    target_row_version: i64,
    source_file_id: Uuid,
    source_row_number: i32,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "insert into import_row_changes
           (id, workspace_id, import_batch_id, sequence_no, target_kind, target_id,
            operation, before_json, after_json, target_row_version, source_file_id,
            source_row_number)
         values ($1, $2, $3, $4, 'imported_record', $5, $6, $7, $8, $9, $10, $11)",
    )
    .bind(Uuid::now_v7())
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .bind(sequence_no)
    .bind(target_id)
    .bind(operation)
    .bind(before_json)
    .bind(after_json)
    .bind(target_row_version)
    .bind(source_file_id)
    .bind(source_row_number)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

async fn append_projection_change(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
    sequence_no: i64,
    change: FormalProjectionChange,
    source_file_id: Uuid,
    source_row_number: i32,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "insert into import_row_changes
           (id, workspace_id, import_batch_id, sequence_no, target_kind, target_id,
            operation, before_json, after_json, target_row_version, source_file_id,
            source_row_number)
         values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)",
    )
    .bind(Uuid::now_v7())
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .bind(sequence_no)
    .bind(change.target_kind)
    .bind(change.target_id)
    .bind(change.operation)
    .bind(change.before_json)
    .bind(change.after_json)
    .bind(change.target_row_version)
    .bind(source_file_id)
    .bind(source_row_number)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

async fn insert_conflict_candidate(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
    dataset_type: &str,
    business_key: &str,
    row: &sqlx::postgres::PgRow,
    existing_record_id: Option<Uuid>,
    conflict_kind: &str,
) -> Result<(), sqlx::Error> {
    sqlx::query(
        "insert into import_conflict_candidates
           (id, workspace_id, import_batch_id, staging_row_id, dataset_type,
            business_key, candidate_data, existing_record_id, conflict_kind)
         values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
         on conflict (workspace_id, import_batch_id, staging_row_id, conflict_kind)
         do nothing",
    )
    .bind(Uuid::now_v7())
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .bind(row.get::<Uuid, _>("id"))
    .bind(dataset_type)
    .bind(business_key)
    .bind(row.get::<Value, _>("record_data"))
    .bind(existing_record_id)
    .bind(conflict_kind)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn append_event(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    import_id: Uuid,
    job_id: Uuid,
    event_type: &str,
    status: &str,
    counters: ImportCounters,
    error_code: Option<&str>,
) -> Result<(), sqlx::Error> {
    let next_seq = sqlx::query_scalar::<_, i64>(
        "select coalesce(max(event_seq), 0) + 1 from import_job_events
         where workspace_id = $1 and import_batch_id = $2",
    )
    .bind(workspace_id)
    .bind(import_id)
    .fetch_one(&mut **tx)
    .await?;
    let mut payload = summary(counters);
    payload["status"] = json!(status);
    if let Some(code) = error_code {
        payload["error_code"] = json!(code);
    }
    sqlx::query(
        "insert into import_job_events
           (id, workspace_id, import_batch_id, job_id, event_seq, event_type, payload)
         values ($1, $2, $3, $4, $5, $6, $7)
         on conflict (workspace_id, import_batch_id, event_seq) do nothing",
    )
    .bind(Uuid::now_v7())
    .bind(workspace_id)
    .bind(import_id)
    .bind(job_id)
    .bind(next_seq)
    .bind(event_type)
    .bind(payload)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn insert_audit(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
    actor_user_id: Uuid,
    event_type: &str,
    outcome: &str,
    counters: ImportCounters,
    error_code: Option<&str>,
    conflict_policy: Option<&str>,
) -> Result<(), sqlx::Error> {
    let mut metadata = summary(counters);
    metadata["job_id"] = json!(job.id);
    if let Some(code) = error_code {
        metadata["error_code"] = json!(code);
    }
    if let Some(policy) = conflict_policy {
        metadata["conflict_policy"] = json!(policy);
    }
    sqlx::query(
        "insert into audit_logs
           (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, $4, $5, $6, $7)",
    )
    .bind(Uuid::now_v7())
    .bind(job.workspace_id)
    .bind(actor_user_id)
    .bind(event_type)
    .bind(outcome)
    .bind(Uuid::now_v7())
    .bind(metadata)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

fn summary(counters: ImportCounters) -> Value {
    json!({
        "processed_rows": counters.processed,
        "total_rows": counters.total,
        "inserted_count": counters.inserted,
        "updated_count": counters.updated,
        "skipped_count": counters.skipped,
        "conflict_count": counters.conflicts
    })
}

fn number(payload: &Value, key: &str) -> i64 {
    payload.get(key).and_then(Value::as_i64).unwrap_or(0)
}

pub(crate) fn lease_allows_execution(
    status: &str,
    leased_by: Option<&str>,
    lease_expires_at: Option<OffsetDateTime>,
    actual_generation: i64,
    expected_generation: i64,
    worker_id: &str,
    now: OffsetDateTime,
) -> bool {
    status == "running"
        && leased_by == Some(worker_id)
        && actual_generation == expected_generation
        && lease_expires_at.is_some_and(|expires_at| expires_at > now)
}

pub(crate) fn require_single_fenced_update(rows_affected: u64) -> Result<(), JobQueueError> {
    if rows_affected == 1 {
        Ok(())
    } else {
        Err(JobQueueError::LeaseLost)
    }
}

fn confirmed_actor(confirmed_by: Option<Uuid>) -> Result<Uuid, JobQueueError> {
    confirmed_by.ok_or(JobQueueError::InvalidFrozenImport)
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

    const WORKER_SOURCE: &str = include_str!("job_queue.rs");

    #[test]
    fn retry_allowlist_is_deny_by_default_for_business_failures() {
        assert!(!JobQueueError::AbortConflict.retryable());
        assert!(!JobQueueError::InvalidFrozenImport.retryable());
        assert!(!JobQueueError::UnsupportedJobType.retryable());
    }

    #[test]
    fn worker_claim_allowlist_contains_only_confirm_and_rollback() {
        assert!(CLAIM_CANDIDATE_SQL.contains("job_type in ('import_confirm', 'import_rollback')"));
        assert!(!CLAIM_CANDIDATE_SQL.contains("object_consistency_scan"));
        assert!(!CLAIM_CANDIDATE_SQL.contains("object_quarantine"));
    }

    #[test]
    fn progress_payload_never_contains_record_data() {
        let payload = summary(ImportCounters {
            processed: 3,
            total: 5,
            inserted: 1,
            updated: 1,
            skipped: 1,
            conflicts: 0,
        });
        assert!(payload.get("record_data").is_none());
        assert_eq!(payload["processed_rows"], 3);
        let event = ImportProgressEvent {
            event_seq: 4,
            event_type: "progress".into(),
            status: "running".into(),
            processed_rows: 3,
            total_rows: 5,
            inserted_count: 1,
            updated_count: 1,
            skipped_count: 1,
            conflict_count: 0,
            error_code: None,
        };
        let serialized = serde_json::to_string(&event).unwrap();
        for forbidden in ["record_data", "cookie", "token", "csrf", "idempotency_key"] {
            assert!(!serialized.contains(forbidden));
        }
    }

    #[test]
    fn lease_fence_rejects_expired_or_reassigned_worker() {
        let now = OffsetDateTime::now_utc();
        assert!(lease_allows_execution(
            "running",
            Some("worker-a"),
            Some(now + Duration::seconds(30)),
            7,
            7,
            "worker-a",
            now
        ));
        assert!(!lease_allows_execution(
            "running",
            Some("worker-b"),
            Some(now + Duration::seconds(30)),
            7,
            7,
            "worker-a",
            now
        ));
        assert!(!lease_allows_execution(
            "running",
            Some("worker-a"),
            Some(now - Duration::seconds(1)),
            7,
            7,
            "worker-a",
            now
        ));
        assert!(!lease_allows_execution(
            "running",
            Some("worker-a"),
            Some(now + Duration::seconds(30)),
            8,
            7,
            "worker-a",
            now
        ));
    }

    #[test]
    fn stale_failure_writer_cannot_pass_fenced_update() {
        assert!(require_single_fenced_update(1).is_ok());
        assert!(matches!(
            require_single_fenced_update(0),
            Err(JobQueueError::LeaseLost)
        ));
        assert!(matches!(
            require_single_fenced_update(2),
            Err(JobQueueError::LeaseLost)
        ));
    }

    #[test]
    fn claim_contract_keeps_skip_locked_for_multi_worker_safety() {
        assert!(CLAIM_CANDIDATE_SQL.contains("for update skip locked"));
        assert!(CLAIM_CANDIDATE_SQL.contains("lease_expires_at < now()"));
        assert!(CLAIM_CANDIDATE_SQL.contains("attempt_count < max_attempts"));
    }

    #[test]
    fn worker_actor_requires_the_frozen_confirmer() {
        let confirmer = Uuid::now_v7();
        assert_eq!(confirmed_actor(Some(confirmer)).unwrap(), confirmer);
        assert!(matches!(
            confirmed_actor(None),
            Err(JobQueueError::InvalidFrozenImport)
        ));
    }

    #[test]
    fn change_sequence_is_contiguous_and_only_advances_when_requested() {
        let mut sequence = ChangeSequence::default();
        assert_eq!(sequence.next(), 1);
        assert_eq!(sequence.next(), 2);
        assert_eq!(sequence.next(), 3);
    }

    #[test]
    fn rollback_snapshot_contains_only_restorable_record_state() {
        let source_batch_id = Uuid::now_v7();
        let snapshot = imported_record_snapshot_values(
            json!({"code": "A", "value": "12.50"}),
            source_batch_id,
            17,
            4,
        );
        assert_eq!(
            snapshot,
            json!({
                "record_data": {"code": "A", "value": "12.50"},
                "source_import_batch_id": source_batch_id,
                "source_row_number": 17,
                "row_version": 4,
            })
        );
        for forbidden in ["created_by", "business_key", "cookie", "token", "password"] {
            assert!(snapshot.get(forbidden).is_none());
        }
    }

    #[test]
    fn overwrite_race_reads_the_exact_winner_before_updating() {
        assert!(WORKER_SOURCE.contains("on conflict (workspace_id, dataset_type, business_key)\n                         do nothing\n                         returning id, record_data"));
        assert!(WORKER_SOURCE.contains("INSERT ... DO NOTHING waits for a concurrent winner."));
        let race_path = WORKER_SOURCE
            .split("INSERT ... DO NOTHING waits for a concurrent winner.")
            .nth(1)
            .expect("race path");
        let locked_read = race_path.find("for update").expect("locked winner read");
        let before_snapshot = race_path
            .find("let before = imported_record_snapshot(&existing)")
            .expect("exact before snapshot");
        let update = race_path
            .find("update imported_records")
            .expect("controlled overwrite");
        let returning = race_path
            .find("returning id, record_data")
            .expect("exact after snapshot");
        assert!(locked_read < before_snapshot);
        assert!(before_snapshot < update);
        assert!(update < returning);
    }

    #[test]
    fn skip_conflict_and_abort_paths_do_not_append_change_rows() {
        let duplicate_conflicts = WORKER_SOURCE
            .split("if policy == \"keep_conflict\" && candidates.len() > 1")
            .nth(1)
            .expect("duplicate conflict branch")
            .split("let candidate =")
            .next()
            .expect("duplicate conflict branch end");
        assert!(!duplicate_conflicts.contains("append_row_change"));

        let existing_conflict = WORKER_SOURCE
            .split("(\"keep_conflict\", Some(existing_id))")
            .nth(1)
            .expect("database conflict branch")
            .split("(\"overwrite\", Some(existing_id))")
            .next()
            .expect("database conflict branch end");
        assert!(!existing_conflict.contains("append_row_change"));

        assert!(WORKER_SOURCE.contains("(\"skip\", Some(_)) => counters.skipped += 1"));
        assert!(WORKER_SOURCE.contains("return Err(JobQueueError::AbortConflict)"));
        let execute_body = WORKER_SOURCE
            .split("pub async fn execute_import_job")
            .nth(1)
            .expect("execute function")
            .split("async fn is_automatic_import")
            .next()
            .expect("execute function end");
        assert_eq!(
            execute_body.matches("append_row_change(").count(),
            4,
            "only the four actual insert/update branches append changes"
        );
    }

    #[test]
    fn changes_capability_terminal_state_event_and_audit_share_one_commit() {
        let execute_body = WORKER_SOURCE
            .split("pub async fn execute_import_job")
            .nth(1)
            .expect("execute function")
            .split("async fn is_automatic_import")
            .next()
            .expect("execute function end");
        assert_eq!(execute_body.matches("tx.commit().await?").count(), 1);
        let change = execute_body
            .find("append_row_change(")
            .expect("change append");
        let capability = execute_body
            .find("rollback_capability = 'direct', change_log_version = 1")
            .expect("capability activation");
        let event = execute_body.rfind("\"succeeded\"").expect("terminal event");
        let audit = execute_body
            .find("\"import.worker_succeeded\"")
            .expect("terminal audit");
        let commit = execute_body
            .find("tx.commit().await?")
            .expect("single commit");
        assert!(change < capability);
        assert!(capability < event);
        assert!(event < audit);
        assert!(audit < commit);
    }
}
