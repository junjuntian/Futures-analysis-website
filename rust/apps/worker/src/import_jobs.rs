use database::job_queue::{
    ClaimedJob, JobQueueError, claim_next_import_job, execute_import_job, record_job_failure,
    renew_lease,
};
use sqlx::PgPool;
use tokio::time::{Duration, interval};
use tracing::{error, info, warn};

#[derive(Debug, Clone)]
pub struct ImportWorkerConfig {
    pub lease_seconds: i64,
    pub renew_seconds: u64,
    pub idle_millis: u64,
}

impl ImportWorkerConfig {
    pub fn from_env() -> anyhow::Result<Self> {
        let lease_seconds = parse_env("IMPORT_JOB_LEASE_SECONDS", 30_i64)?;
        let renew_seconds = parse_env("IMPORT_JOB_RENEW_SECONDS", 10_u64)?;
        let idle_millis = parse_env("IMPORT_JOB_IDLE_MILLIS", 500_u64)?;
        if lease_seconds <= 0
            || renew_seconds == 0
            || renew_seconds >= lease_seconds as u64
            || idle_millis == 0
        {
            anyhow::bail!("invalid import worker timing configuration");
        }
        Ok(Self {
            lease_seconds,
            renew_seconds,
            idle_millis,
        })
    }
}

pub async fn run(
    pool: PgPool,
    worker_id: String,
    config: ImportWorkerConfig,
) -> anyhow::Result<()> {
    loop {
        match claim_next_import_job(&pool, &worker_id, config.lease_seconds).await {
            Ok(Some(job)) => process_claimed(&pool, &worker_id, &config, job).await,
            Ok(None) => tokio::time::sleep(Duration::from_millis(config.idle_millis)).await,
            Err(error) => {
                warn!(error_code = error.code(), "failed to claim import job");
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        }
    }
}

async fn process_claimed(
    pool: &PgPool,
    worker_id: &str,
    config: &ImportWorkerConfig,
    mut job: ClaimedJob,
) {
    info!(
        job_id = %job.id,
        import_id = %job.aggregate_id,
        attempt = job.attempt_count,
        "processing import job"
    );
    let execute_pool = pool.clone();
    let execute_job = job.clone();
    let execute_worker = worker_id.to_string();
    let task = tokio::spawn(async move {
        execute_import_job(&execute_pool, &execute_job, &execute_worker).await
    });
    tokio::pin!(task);
    let mut renewals = interval(Duration::from_secs(config.renew_seconds));
    renewals.tick().await;
    let result = loop {
        tokio::select! {
            result = &mut task => {
                break match result {
                    Ok(result) => result,
                    Err(_) => Err(JobQueueError::InvalidFrozenImport),
                };
            }
            _ = renewals.tick() => {
                match renew_lease(pool, &job, worker_id, config.lease_seconds).await {
                    Ok(expires_at) => job.lease_expires_at = expires_at,
                    Err(error) => {
                        warn!(job_id = %job.id, error_code = error.code(), "job lease renewal failed");
                    }
                }
            }
        }
    };
    match result {
        Ok(()) => info!(job_id = %job.id, "import job succeeded"),
        Err(error) => {
            warn!(job_id = %job.id, error_code = error.code(), "import job failed");
            if let Err(record_error) = record_job_failure(pool, &job, worker_id, &error).await {
                error!(
                    job_id = %job.id,
                    error_code = record_error.code(),
                    "failed to persist import job failure"
                );
            }
        }
    }
}

fn parse_env<T>(name: &str, default: T) -> anyhow::Result<T>
where
    T: std::str::FromStr,
{
    match std::env::var(name) {
        Ok(value) => value.parse().map_err(|_| anyhow::anyhow!("invalid {name}")),
        Err(_) => Ok(default),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_lease_has_required_renewal_ratio() {
        let config = ImportWorkerConfig {
            lease_seconds: 30,
            renew_seconds: 10,
            idle_millis: 500,
        };
        assert!(config.renew_seconds < config.lease_seconds as u64);
    }
}
