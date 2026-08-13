pub mod spread_analytics;

use sqlx::{PgPool, postgres::PgPoolOptions};
use std::time::Duration;

pub async fn connect(database_url: &str) -> Result<PgPool, sqlx::Error> {
    PgPoolOptions::new()
        .max_connections(5)
        .acquire_timeout(Duration::from_secs(5))
        .connect(database_url)
        .await
}

pub async fn check_ready(pool: &PgPool) -> bool {
    sqlx::query("select 1").execute(pool).await.is_ok()
}

#[cfg(test)]
mod phase_4a_schema_contract {
    use std::{fs, path::Path};

    const MIGRATION: &str =
        include_str!("../../../migrations/202608020001_phase_4a_collection_schema.sql");

    #[test]
    fn all_phase_4a_business_tables_force_workspace_rls() {
        for table in [
            "exchanges",
            "instruments",
            "contracts",
            "trading_calendar_versions",
            "trading_calendar_days",
        ] {
            assert!(MIGRATION.contains(&format!("alter table {table} enable row level security;")));
            assert!(MIGRATION.contains(&format!("alter table {table} force row level security;")));
            assert!(MIGRATION.contains(&format!(
                "create policy {table}_workspace_isolation on {table}"
            )));
        }
    }

    /// 每条迁移都要自报版本号，否则部署会在打包那一步失败。
    ///
    /// 部署工作流本来就有这道守卫，但它在流水线最后才跑：我因此白白来回了一趟——
    /// 建镜像、部署、被拒、改、再建镜像，二十多分钟。同一条规则在这里再查一遍，
    /// CI 一分钟内就能告诉我。
    #[test]
    fn every_migration_records_its_own_version() {
        let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../migrations");
        let mut checked = 0;
        for entry in fs::read_dir(&dir).expect("migrations directory") {
            let path = entry.expect("migration entry").path();
            if path.extension().and_then(|value| value.to_str()) != Some("sql") {
                continue;
            }
            let name = path
                .file_name()
                .and_then(|value| value.to_str())
                .expect("migration file name")
                .to_string();
            let version = name.split('_').next().expect("version prefix").to_string();
            let body = fs::read_to_string(&path).expect("migration body");
            assert!(
                body.contains("insert into schema_versions"),
                "{name} 没有往 schema_versions 写记录"
            );
            assert!(
                body.contains(&format!("'{version}'")),
                "{name} 写的版本号与文件名不符"
            );
            checked += 1;
        }
        assert!(checked > 20, "只扫到 {checked} 条迁移，路径大概错了");
    }
}
