mod import_jobs;

use common::AppConfig;
use tracing::info;
use uuid::Uuid;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    infrastructure::init_tracing();
    let config = AppConfig::from_env(8081)?;
    let pool = database::connect(&config.database_url).await?;
    info!(
        database_url = config.redacted_database_url(),
        "worker connected to database"
    );
    let worker_config = import_jobs::ImportWorkerConfig::from_env()?;
    let worker_id = format!("worker-{}", Uuid::now_v7());
    tokio::select! {
        result = import_jobs::run(pool, worker_id, worker_config) => result?,
        _ = shutdown_signal() => info!("worker shutdown requested"),
    }

    Ok(())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        let _ = tokio::signal::ctrl_c().await;
    };

    #[cfg(unix)]
    let terminate = async {
        if let Ok(mut signal) =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
        {
            signal.recv().await;
        }
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }
}
