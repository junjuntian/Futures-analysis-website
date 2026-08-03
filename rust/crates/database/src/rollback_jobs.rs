use crate::{
    imports::{
        ImportRepositoryError, evaluate_rollback, insert_rollback_conflicts, push_rollback_conflict,
    },
    job_queue::{ClaimedJob, JobQueueError, lease_allows_execution, require_single_fenced_update},
};
use domain::import::ImportRollbackConflictType;
use serde_json::Value;
use sqlx::{PgPool, Postgres, Row, Transaction};
use time::OffsetDateTime;
use uuid::Uuid;

pub async fn execute_rollback_job(
    pool: &PgPool,
    job: &ClaimedJob,
    worker_id: &str,
) -> Result<(), JobQueueError> {
    if job.job_type != "import_rollback" {
        return Err(JobQueueError::UnsupportedJobType);
    }
    let mut tx = pool.begin().await?;
    set_workspace(&mut tx, job.workspace_id).await?;

    // Global rollback lock order: job -> batch -> rollback request -> target
    // rows. The job lock also makes the generation fence authoritative for
    // every formal write in this transaction.
    let lease = sqlx::query(
        "select status, leased_by, lease_expires_at, lease_generation
           from job_queue
          where workspace_id = $1 and id = $2 and job_type = 'import_rollback'
          for update",
    )
    .bind(job.workspace_id)
    .bind(job.id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(JobQueueError::LeaseLost)?;
    ensure_lease(&lease, job, worker_id)?;

    let batch = sqlx::query(
        "select status::text as status, rollback_capability, change_log_version
           from import_batches
          where workspace_id = $1 and id = $2
          for update",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(JobQueueError::InvalidFrozenImport)?;
    let change_log_version = batch.get::<Option<i32>, _>("change_log_version");
    if batch.get::<String, _>("status") != "rolling_back"
        || batch.get::<String, _>("rollback_capability") != "direct"
        || !matches!(change_log_version, Some(1 | 2))
    {
        return Err(JobQueueError::InvalidFrozenImport);
    }

    let request = sqlx::query(
        "select id, requested_by, precheck_fingerprint, status, job_id
           from import_rollback_requests
          where workspace_id = $1 and import_batch_id = $2 and job_id = $3
          for update",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .bind(job.id)
    .fetch_optional(&mut *tx)
    .await?
    .ok_or(JobQueueError::InvalidFrozenImport)?;
    if request.get::<String, _>("status") != "running"
        || request.get::<Option<Uuid>, _>("job_id") != Some(job.id)
    {
        return Err(JobQueueError::InvalidFrozenImport);
    }
    let rollback_request_id = request.get::<Uuid, _>("id");
    let actor_user_id = request.get::<Uuid, _>("requested_by");
    let stored_fingerprint = request.get::<String, _>("precheck_fingerprint");

    // This performs the complete precheck again and locks target rows in
    // (target_kind, target_id) order. The current request is excluded from
    // the active-request conflict because it is the job being executed.
    let mut evaluated = evaluate_rollback(
        &mut tx,
        job.workspace_id,
        job.aggregate_id,
        "rolling_back",
        Some(rollback_request_id),
    )
    .await
    .map_err(map_repository_error)?;
    if evaluated.fingerprint != stored_fingerprint {
        push_rollback_conflict(
            &mut evaluated.conflicts,
            ImportRollbackConflictType::IllegalChange,
            None,
            None,
            None,
            None,
            None,
            "rollback_precheck_fingerprint_stale",
        );
    }

    // The job row is still locked, so no renewal or reclaim can change its
    // generation. Re-check ownership without imposing the lease duration as
    // a maximum rollback transaction length.
    ensure_generation_ownership(&lease, job, worker_id)?;
    if !evaluated.conflicts.is_empty() {
        persist_worker_conflict(
            &mut tx,
            job,
            worker_id,
            rollback_request_id,
            actor_user_id,
            evaluated.affected_count,
            &evaluated.conflicts,
        )
        .await?;
        tx.commit().await?;
        return Ok(());
    }

    let changes = sqlx::query(
        "select sequence_no, target_kind, target_id, operation, before_json,
                after_json, target_row_version
           from import_row_changes
          where workspace_id = $1 and import_batch_id = $2
          order by sequence_no desc, id desc",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .fetch_all(&mut *tx)
    .await?;
    for change in &changes {
        let target_kind = change.get::<String, _>("target_kind");
        let target_id = change.get::<Uuid, _>("target_id");
        let expected_version = change.get::<i64, _>("target_row_version");
        let after_json = change
            .get::<Option<Value>, _>("after_json")
            .ok_or(JobQueueError::InvalidFrozenImport)?;
        let operation = change.get::<String, _>("operation");
        if target_kind == "imported_record" {
            rollback_imported_record(
                &mut tx,
                job.workspace_id,
                target_id,
                &operation,
                change.get::<Option<Value>, _>("before_json"),
                &after_json,
                expected_version,
            )
            .await?;
        } else if change_log_version == Some(2) {
            rollback_projection(
                &mut tx,
                job.workspace_id,
                &target_kind,
                target_id,
                &operation,
                change.get::<Option<Value>, _>("before_json"),
                &after_json,
                expected_version,
            )
            .await?;
        } else {
            return Err(JobQueueError::InvalidFrozenImport);
        }
        sqlx::query(
            "insert into import_data_invalidations
               (id, workspace_id, import_batch_id, rollback_request_id,
                target_kind, target_id, invalidation_kind)
             values ($1, $2, $3, $4, $5, $6, 'import_rollback')",
        )
        .bind(Uuid::now_v7())
        .bind(job.workspace_id)
        .bind(job.aggregate_id)
        .bind(rollback_request_id)
        .bind(&target_kind)
        .bind(target_id)
        .execute(&mut *tx)
        .await?;
    }

    // The fenced job update below is the commit boundary. The row lock makes
    // reclaim impossible until this transaction commits or rolls back.
    ensure_generation_ownership(&lease, job, worker_id)?;
    let batch_updated = sqlx::query(
        "update import_batches
            set status = 'rolled_back', rolled_back_at = now(), updated_at = now()
          where workspace_id = $1 and id = $2 and status = 'rolling_back'",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    require_single_fenced_update(batch_updated)?;
    let request_updated = sqlx::query(
        "update import_rollback_requests
            set status = 'succeeded', conflict_count = 0,
                finished_at = now(), updated_at = now()
          where workspace_id = $1 and id = $2 and job_id = $3 and status = 'running'",
    )
    .bind(job.workspace_id)
    .bind(rollback_request_id)
    .bind(job.id)
    .execute(&mut *tx)
    .await?
    .rows_affected();
    require_single_fenced_update(request_updated)?;
    finish_job(&mut tx, job, worker_id, "succeeded", None).await?;
    append_rollback_event(
        &mut tx,
        job,
        rollback_request_id,
        "rollback_running",
        "running",
        evaluated.affected_count,
        0,
        None,
    )
    .await?;
    append_rollback_event(
        &mut tx,
        job,
        rollback_request_id,
        "rolled_back",
        "rolled_back",
        evaluated.affected_count,
        0,
        None,
    )
    .await?;
    insert_rollback_audit(
        &mut tx,
        job,
        rollback_request_id,
        actor_user_id,
        "import.rollback_worker_succeeded",
        "success",
        evaluated.affected_count,
        0,
        None,
    )
    .await?;
    tx.commit().await?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn rollback_imported_record(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    target_id: Uuid,
    operation: &str,
    before_json: Option<Value>,
    after_json: &Value,
    expected_version: i64,
) -> Result<(), JobQueueError> {
    match operation {
        "insert" => {
            let deleted = sqlx::query(
                "delete from imported_records
                  where workspace_id = $1 and id = $2 and row_version = $3
                    and jsonb_build_object(
                        'record_data', record_data,
                        'source_import_batch_id', source_import_batch_id,
                        'source_row_number', source_row_number,
                        'row_version', row_version
                    ) = $4
                  returning id",
            )
            .bind(workspace_id)
            .bind(target_id)
            .bind(expected_version)
            .bind(after_json)
            .fetch_optional(&mut **tx)
            .await?;
            if deleted.is_none() {
                return Err(JobQueueError::InvalidFrozenImport);
            }
        }
        "update" | "soft_delete" => {
            let restore =
                parse_restore_snapshot(&before_json.ok_or(JobQueueError::InvalidFrozenImport)?)?;
            let rollback_version = expected_version
                .checked_add(1)
                .ok_or(JobQueueError::InvalidFrozenImport)?;
            let updated = sqlx::query(
                "update imported_records
                    set record_data = $1, source_import_batch_id = $2,
                        source_row_number = $3, row_version = $4,
                        updated_at = now()
                  where workspace_id = $5 and id = $6 and row_version = $7
                    and jsonb_build_object(
                        'record_data', record_data,
                        'source_import_batch_id', source_import_batch_id,
                        'source_row_number', source_row_number,
                        'row_version', row_version
                    ) = $8",
            )
            .bind(restore.record_data)
            .bind(restore.source_import_batch_id)
            .bind(restore.source_row_number)
            .bind(rollback_version)
            .bind(workspace_id)
            .bind(target_id)
            .bind(expected_version)
            .bind(after_json)
            .execute(&mut **tx)
            .await?
            .rows_affected();
            require_single_fenced_update(updated)?;
        }
        _ => return Err(JobQueueError::InvalidFrozenImport),
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn rollback_projection(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
    target_kind: &str,
    target_id: Uuid,
    operation: &str,
    before_json: Option<Value>,
    after_json: &Value,
    expected_version: i64,
) -> Result<(), JobQueueError> {
    if operation == "insert" {
        let sql = match target_kind {
            "exchange" => {
                "delete from exchanges target
                  where workspace_id = $1 and id = $2 and row_version = $3
                    and to_jsonb(target) - 'workspace_id' - 'created_at' - 'updated_at' = $4"
            }
            "instrument" => {
                "delete from instruments target
                  where workspace_id = $1 and id = $2 and row_version = $3
                    and to_jsonb(target) - 'workspace_id' - 'created_at' - 'updated_at' = $4"
            }
            "contract" => {
                "delete from contracts target
                  where workspace_id = $1 and id = $2 and row_version = $3
                    and to_jsonb(target) - 'workspace_id' - 'created_at' - 'updated_at' = $4"
            }
            "trading_calendar_version" => {
                "delete from trading_calendar_versions target
                  where workspace_id = $1 and id = $2 and row_version = $3
                    and to_jsonb(target) - 'workspace_id' - 'created_at' = $4"
            }
            "trading_calendar_day" => {
                "delete from trading_calendar_days target
                  where workspace_id = $1 and source_record_id = $2 and row_version = $3
                    and to_jsonb(target) - 'workspace_id' - 'created_at' = $4"
            }
            "market_price" => {
                "delete from market_prices target
                  where workspace_id = $1 and source_record_id = $2 and row_version = $3
                    and to_jsonb(target) - 'workspace_id' - 'created_at' = $4"
            }
            "seat_entity" => {
                "delete from seat_entities target
                  where workspace_id = $1 and id = $2 and row_version = $3
                    and to_jsonb(target) - 'workspace_id' - 'created_at' - 'updated_at' = $4"
            }
            "seat_position" => {
                "delete from seat_positions target
                  where workspace_id = $1 and source_record_id = $2 and row_version = $3
                    and to_jsonb(target) - 'workspace_id' - 'created_at' = $4"
            }
            _ => return Err(JobQueueError::InvalidFrozenImport),
        };
        let deleted = sqlx::query(sql)
            .bind(workspace_id)
            .bind(target_id)
            .bind(expected_version)
            .bind(after_json)
            .execute(&mut **tx)
            .await?
            .rows_affected();
        require_single_fenced_update(deleted)?;
        return Ok(());
    }
    if operation != "update" {
        return Err(JobQueueError::InvalidFrozenImport);
    }
    let before_json = before_json.ok_or(JobQueueError::InvalidFrozenImport)?;
    let rollback_version = expected_version
        .checked_add(1)
        .ok_or(JobQueueError::InvalidFrozenImport)?;
    let sql = match target_kind {
        "exchange" => {
            "update exchanges target
                set name = $1->>'name', timezone = $1->>'timezone',
                    source_record_id = ($1->>'source_record_id')::uuid,
                    row_version = $2, updated_at = now()
              where workspace_id = $3 and id = $4 and row_version = $5
                and to_jsonb(target) - 'workspace_id' - 'created_at' - 'updated_at' = $6"
        }
        "instrument" => {
            "update instruments target
                set name = $1->>'name', currency_code = $1->>'currency_code',
                    contract_multiplier = nullif($1->>'contract_multiplier', '')::numeric,
                    price_tick = nullif($1->>'price_tick', '')::numeric,
                    source_record_id = ($1->>'source_record_id')::uuid,
                    row_version = $2, updated_at = now()
              where workspace_id = $3 and id = $4 and row_version = $5
                and to_jsonb(target) - 'workspace_id' - 'created_at' - 'updated_at' = $6"
        }
        "contract" => {
            "update contracts target
                set delivery_month = nullif($1->>'delivery_month', ''),
                    listed_at = nullif($1->>'listed_at', '')::date,
                    expires_at = nullif($1->>'expires_at', '')::date,
                    source_record_id = ($1->>'source_record_id')::uuid,
                    row_version = $2, updated_at = now()
              where workspace_id = $3 and id = $4 and row_version = $5
                and to_jsonb(target) - 'workspace_id' - 'created_at' - 'updated_at' = $6"
        }
        "market_price" => {
            "update market_prices target
                set source_id = ($1->>'source_id')::uuid,
                    contract_id = ($1->>'contract_id')::uuid,
                    trade_date = ($1->>'trade_date')::date,
                    session_type = $1->>'session_type',
                    observed_at = ($1->>'observed_at')::timestamptz,
                    granularity = $1->>'granularity',
                    close_price = nullif($1->>'close_price', '')::numeric,
                    settlement_price = nullif($1->>'settlement_price', '')::numeric,
                    currency_code = $1->>'currency_code',
                    calendar_version_id = ($1->>'calendar_version_id')::uuid,
                    revision_no = ($1->>'revision_no')::integer,
                    source_import_batch_id = ($1->>'source_import_batch_id')::uuid,
                    source_row_number = ($1->>'source_row_number')::integer,
                    source_record_id = ($1->>'source_record_id')::uuid,
                    row_version = $2
              where workspace_id = $3 and source_record_id = $4 and row_version = $5
                and to_jsonb(target) - 'workspace_id' - 'created_at' = $6"
        }
        "seat_position" => {
            "update seat_positions target
                set trade_date = ($1->>'trade_date')::date,
                    contract_id = ($1->>'contract_id')::uuid,
                    seat_id = ($1->>'seat_id')::uuid,
                    rank_type = $1->>'rank_type',
                    rank = ($1->>'rank')::integer,
                    volume = nullif($1->>'volume', '')::bigint,
                    long_position = nullif($1->>'long_position', '')::bigint,
                    short_position = nullif($1->>'short_position', '')::bigint,
                    source_id = ($1->>'source_id')::uuid,
                    source_import_batch_id = ($1->>'source_import_batch_id')::uuid,
                    source_row_number = ($1->>'source_row_number')::integer,
                    source_record_id = ($1->>'source_record_id')::uuid,
                    row_version = $2
              where workspace_id = $3 and source_record_id = $4 and row_version = $5
                and to_jsonb(target) - 'workspace_id' - 'created_at' = $6"
        }
        _ => return Err(JobQueueError::InvalidFrozenImport),
    };
    let updated = sqlx::query(sql)
        .bind(before_json)
        .bind(rollback_version)
        .bind(workspace_id)
        .bind(target_id)
        .bind(expected_version)
        .bind(after_json)
        .execute(&mut **tx)
        .await?
        .rows_affected();
    require_single_fenced_update(updated)?;
    Ok(())
}

#[derive(Debug)]
struct RestoreSnapshot {
    record_data: Value,
    source_import_batch_id: Uuid,
    source_row_number: i32,
}

fn parse_restore_snapshot(value: &Value) -> Result<RestoreSnapshot, JobQueueError> {
    let record_data = value
        .get("record_data")
        .filter(|value| value.is_object())
        .cloned()
        .ok_or(JobQueueError::InvalidFrozenImport)?;
    let source_import_batch_id = value
        .get("source_import_batch_id")
        .and_then(Value::as_str)
        .and_then(|value| Uuid::parse_str(value).ok())
        .ok_or(JobQueueError::InvalidFrozenImport)?;
    let source_row_number = value
        .get("source_row_number")
        .and_then(Value::as_i64)
        .and_then(|value| i32::try_from(value).ok())
        .filter(|value| *value > 0)
        .ok_or(JobQueueError::InvalidFrozenImport)?;
    Ok(RestoreSnapshot {
        record_data,
        source_import_batch_id,
        source_row_number,
    })
}

fn ensure_lease(
    lease: &sqlx::postgres::PgRow,
    job: &ClaimedJob,
    worker_id: &str,
) -> Result<(), JobQueueError> {
    if lease_allows_execution(
        lease.get("status"),
        lease.get::<Option<String>, _>("leased_by").as_deref(),
        lease.get("lease_expires_at"),
        lease.get("lease_generation"),
        job.lease_generation,
        worker_id,
        OffsetDateTime::now_utc(),
    ) {
        Ok(())
    } else {
        Err(JobQueueError::LeaseLost)
    }
}

fn ensure_generation_ownership(
    lease: &sqlx::postgres::PgRow,
    job: &ClaimedJob,
    worker_id: &str,
) -> Result<(), JobQueueError> {
    if lease.get::<String, _>("status") == "running"
        && lease.get::<Option<String>, _>("leased_by").as_deref() == Some(worker_id)
        && lease.get::<i64, _>("lease_generation") == job.lease_generation
    {
        Ok(())
    } else {
        Err(JobQueueError::LeaseLost)
    }
}

#[allow(clippy::too_many_arguments)]
async fn persist_worker_conflict(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
    worker_id: &str,
    rollback_request_id: Uuid,
    actor_user_id: Uuid,
    affected_count: u32,
    conflicts: &[domain::import::ImportRollbackConflict],
) -> Result<(), JobQueueError> {
    insert_rollback_conflicts(
        tx,
        job.workspace_id,
        job.aggregate_id,
        rollback_request_id,
        conflicts,
    )
    .await
    .map_err(map_repository_error)?;
    let batch_updated = sqlx::query(
        "update import_batches
            set status = 'rollback_conflict', updated_at = now()
          where workspace_id = $1 and id = $2 and status = 'rolling_back'",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .execute(&mut **tx)
    .await?
    .rows_affected();
    require_single_fenced_update(batch_updated)?;
    let request_updated = sqlx::query(
        "update import_rollback_requests
            set status = 'worker_conflict', conflict_count = $1,
                finished_at = now(), updated_at = now()
          where workspace_id = $2 and id = $3 and job_id = $4 and status = 'running'",
    )
    .bind(conflicts.len() as i32)
    .bind(job.workspace_id)
    .bind(rollback_request_id)
    .bind(job.id)
    .execute(&mut **tx)
    .await?
    .rows_affected();
    require_single_fenced_update(request_updated)?;
    finish_job(tx, job, worker_id, "failed", Some("rollback_conflict")).await?;
    append_rollback_event(
        tx,
        job,
        rollback_request_id,
        "rollback_running",
        "running",
        affected_count,
        conflicts.len() as u32,
        None,
    )
    .await?;
    append_rollback_event(
        tx,
        job,
        rollback_request_id,
        "rollback_conflict",
        "rollback_conflict",
        affected_count,
        conflicts.len() as u32,
        Some("rollback_conflict"),
    )
    .await?;
    insert_rollback_audit(
        tx,
        job,
        rollback_request_id,
        actor_user_id,
        "import.rollback_worker_conflict",
        "failure",
        affected_count,
        conflicts.len() as u32,
        Some("rollback_conflict"),
    )
    .await
}

async fn finish_job(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
    worker_id: &str,
    status: &str,
    error_code: Option<&str>,
) -> Result<(), JobQueueError> {
    let updated = sqlx::query(
        "update job_queue
            set status = $1, leased_by = null, lease_expires_at = null,
                last_error_code = $2, finished_at = now(), updated_at = now()
          where workspace_id = $3 and id = $4 and status = 'running'
            and leased_by = $5 and lease_generation = $6",
    )
    .bind(status)
    .bind(error_code)
    .bind(job.workspace_id)
    .bind(job.id)
    .bind(worker_id)
    .bind(job.lease_generation)
    .execute(&mut **tx)
    .await?
    .rows_affected();
    require_single_fenced_update(updated)
}

#[allow(clippy::too_many_arguments)]
async fn append_rollback_event(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
    rollback_request_id: Uuid,
    event_type: &str,
    status: &str,
    affected_count: u32,
    conflict_count: u32,
    error_code: Option<&str>,
) -> Result<(), JobQueueError> {
    let event_seq = sqlx::query_scalar::<_, i64>(
        "select coalesce(max(event_seq), 0) + 1
           from import_job_events
          where workspace_id = $1 and import_batch_id = $2",
    )
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .fetch_one(&mut **tx)
    .await?;
    sqlx::query(
        "insert into import_job_events
           (id, workspace_id, import_batch_id, job_id, event_seq, event_type, payload)
         values ($1, $2, $3, $4, $5, $6,
                 jsonb_strip_nulls(jsonb_build_object(
                    'status', $7::text,
                    'rollback_request_id', $8::text,
                    'affected_count', $9::integer,
                    'conflict_count', $10::integer,
                    'error_code', $11::text
                 )))",
    )
    .bind(Uuid::now_v7())
    .bind(job.workspace_id)
    .bind(job.aggregate_id)
    .bind(job.id)
    .bind(event_seq)
    .bind(event_type)
    .bind(status)
    .bind(rollback_request_id)
    .bind(affected_count as i32)
    .bind(conflict_count as i32)
    .bind(error_code)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

#[allow(clippy::too_many_arguments)]
async fn insert_rollback_audit(
    tx: &mut Transaction<'_, Postgres>,
    job: &ClaimedJob,
    rollback_request_id: Uuid,
    actor_user_id: Uuid,
    event_type: &str,
    outcome: &str,
    affected_count: u32,
    conflict_count: u32,
    error_code: Option<&str>,
) -> Result<(), JobQueueError> {
    sqlx::query(
        "insert into audit_logs
           (id, workspace_id, actor_user_id, event_type, outcome, request_id, metadata)
         values ($1, $2, $3, $4, $5, $6,
                 jsonb_strip_nulls(jsonb_build_object(
                    'import_id', $7::text,
                    'rollback_request_id', $8::text,
                    'job_id', $9::text,
                    'affected_count', $10::integer,
                    'conflict_count', $11::integer,
                    'error_code', $12::text
                 )))",
    )
    .bind(Uuid::now_v7())
    .bind(job.workspace_id)
    .bind(actor_user_id)
    .bind(event_type)
    .bind(outcome)
    .bind(Uuid::now_v7())
    .bind(job.aggregate_id)
    .bind(rollback_request_id)
    .bind(job.id)
    .bind(affected_count as i32)
    .bind(conflict_count as i32)
    .bind(error_code)
    .execute(&mut **tx)
    .await?;
    Ok(())
}

fn map_repository_error(error: ImportRepositoryError) -> JobQueueError {
    match error {
        ImportRepositoryError::Database(error) => JobQueueError::Database(error),
        _ => JobQueueError::InvalidFrozenImport,
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
    use super::*;

    const SOURCE: &str = include_str!("rollback_jobs.rs");

    #[test]
    fn restore_snapshot_accepts_only_controlled_fields() {
        let source_batch = Uuid::now_v7();
        let snapshot = serde_json::json!({
            "record_data": {"price": "12.5"},
            "source_import_batch_id": source_batch,
            "source_row_number": 9,
            "row_version": 3,
        });
        let parsed = parse_restore_snapshot(&snapshot).unwrap();
        assert_eq!(parsed.source_import_batch_id, source_batch);
        assert_eq!(parsed.source_row_number, 9);
        assert_eq!(parsed.record_data, serde_json::json!({"price": "12.5"}));
    }

    #[test]
    fn rollback_locks_job_batch_request_then_stably_sorted_targets() {
        let job_lock = SOURCE
            .find("job_type = 'import_rollback'\n          for update")
            .unwrap();
        let batch_lock = SOURCE.find("from import_batches").unwrap();
        let request_lock = SOURCE.find("from import_rollback_requests").unwrap();
        let target_locks = SOURCE.find("evaluate_rollback(").unwrap();
        assert!(job_lock < batch_lock);
        assert!(batch_lock < request_lock);
        assert!(request_lock < target_locks);
        let precheck_source = include_str!("imports.rs");
        assert!(precheck_source.contains("order by change_row.target_kind, change_row.target_id"));
    }

    #[test]
    fn rollback_applies_changes_in_reverse_and_versions_restored_updates() {
        assert!(SOURCE.contains("order by sequence_no desc, id desc"));
        assert!(SOURCE.contains("\"insert\" =>"));
        assert!(SOURCE.contains("\"update\" | \"soft_delete\" =>"));
        assert!(SOURCE.contains(".checked_add(1)"));
        assert!(SOURCE.contains("insert into import_data_invalidations"));
    }

    #[test]
    fn fact_updates_restore_complete_version_fenced_snapshots() {
        for table in ["market_prices", "seat_positions"] {
            let update = format!("update {table} target");
            let section = SOURCE
                .split(&update)
                .nth(1)
                .expect("fact rollback update")
                .split("_ =>")
                .next()
                .expect("fact rollback update end");
            assert!(section.contains("source_import_batch_id"));
            assert!(section.contains("source_row_number"));
            assert!(section.contains("source_record_id"));
            assert!(section.contains("row_version = $2"));
            assert!(section.contains("source_record_id = $4 and row_version = $5"));
            assert!(section.contains("to_jsonb(target)"));
        }
    }

    #[test]
    fn conflict_branch_precedes_and_excludes_all_target_mutation() {
        let conflict = SOURCE.find("if !evaluated.conflicts.is_empty()").unwrap();
        let conflict_return = SOURCE[conflict..]
            .find("return Ok(())")
            .map(|offset| conflict + offset)
            .unwrap();
        let delete = SOURCE.find("delete from imported_records").unwrap();
        let update = SOURCE.find("update imported_records").unwrap();
        assert!(conflict < conflict_return);
        assert!(conflict_return < delete);
        assert!(conflict_return < update);
    }

    #[test]
    fn stale_fingerprint_is_a_worker_conflict_not_a_target_write() {
        let stale = SOURCE.find("rollback_precheck_fingerprint_stale").unwrap();
        let conflict = SOURCE.find("if !evaluated.conflicts.is_empty()").unwrap();
        let delete = SOURCE.find("delete from imported_records").unwrap();
        assert!(stale < conflict);
        assert!(conflict < delete);
    }

    #[test]
    fn rollback_has_only_atomic_success_or_conflict_commits() {
        let execute = SOURCE
            .split("pub async fn execute_rollback_job")
            .nth(1)
            .unwrap()
            .split("#[derive(Debug)]")
            .next()
            .unwrap();
        assert_eq!(execute.matches("pool.begin().await?").count(), 1);
        assert_eq!(execute.matches("tx.commit().await?").count(), 2);
        assert!(!execute.contains("savepoint"));
        assert!(!execute.contains("partial"));
    }

    #[test]
    fn rollback_commit_is_generation_fenced_and_reclaim_safe() {
        assert!(SOURCE.contains("lease_generation = $6"));
        assert!(SOURCE.contains("ensure_generation_ownership(&lease, job, worker_id)?"));
        let claim_source = include_str!("job_queue.rs");
        assert!(claim_source.contains("lease_generation = $4"));
        assert!(claim_source.contains("status == \"running\" && expired"));
        assert!(claim_source.contains("for update skip locked"));
    }

    #[test]
    fn events_and_audits_are_summary_only() {
        let event = SOURCE
            .split("async fn append_rollback_event")
            .nth(1)
            .unwrap()
            .split("async fn insert_rollback_audit")
            .next()
            .unwrap();
        let audit = SOURCE
            .split("async fn insert_rollback_audit")
            .nth(1)
            .unwrap()
            .split("fn map_repository_error")
            .next()
            .unwrap();
        for forbidden in [
            "record_data",
            "before_json",
            "after_json",
            "password",
            "token",
        ] {
            assert!(!event.contains(forbidden));
            assert!(!audit.contains(forbidden));
        }
    }
}
