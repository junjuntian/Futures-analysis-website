use common::AppConfig;
use tokio::time::{Duration, interval};
use tracing::{info, warn};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    infrastructure::init_tracing();
    let config = AppConfig::from_env(8081)?;
    let pool = database::connect(&config.database_url).await?;
    info!(
        database_url = config.redacted_database_url(),
        "worker connected to database"
    );

    let mut ticks = interval(Duration::from_secs(5));
    loop {
        tokio::select! {
            _ = ticks.tick() => {
                if !database::check_ready(&pool).await {
                    warn!("database readiness check failed");
                }
            }
            _ = shutdown_signal() => {
                info!("worker shutdown requested");
                break;
            }
        }
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
