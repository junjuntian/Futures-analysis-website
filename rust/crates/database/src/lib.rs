pub mod spread_analytics;

use sqlx::{PgPool, postgres::PgPoolOptions};
use std::time::Duration;

/// 连接池。**5 条太小,2026-09-11 在生产上把页面打成 500**。
///
/// 事故形态:机构资金页的逐合约小窗**一次并发开五个窗**,每个 `net-position`
/// 未命中缓存时要占着连接算 3.3 秒(日志实测),五条连接当场占满;
/// 同一页同时发出的「品种范围」「席位组收藏」两个请求排队等不到连接,
/// 5 秒 `acquire_timeout` 一到就 **500**。页面于是回落成
/// **品种下拉只剩代码没有中文、收藏的席位组全空** —— 看着像数据丢了,
/// 其实库里一条没少(当时实测 scope 12 行、收藏 9 条都在)。
///
/// 判断依据是日志里那个 `latency: 5064 ms`:**不多不少就是 acquire_timeout**,
/// 不是查询本身慢。
///
/// 为什么 20 是安全的:Postgres 这边 `max_connections = 100`,平时只用 11 条
/// (2026-09-11 实测),留 80 条余量;采集与引擎走各自的 psql 短连接,不吃这个池。
/// 超时也放宽到 15 秒 —— **宁可让人多等一会,也不要把一次慢查询变成一次报错**。
pub const MAX_CONNECTIONS: u32 = 20;
/// 宁可让人多等一会,也不要把一次慢查询变成一次报错。
pub const ACQUIRE_TIMEOUT_SECS: u64 = 15;

pub async fn connect(database_url: &str) -> Result<PgPool, sqlx::Error> {
    PgPoolOptions::new()
        .max_connections(MAX_CONNECTIONS)
        .acquire_timeout(Duration::from_secs(ACQUIRE_TIMEOUT_SECS))
        .connect(database_url)
        .await
}

pub async fn check_ready(pool: &PgPool) -> bool {
    sqlx::query("select 1").execute(pool).await.is_ok()
}

#[cfg(test)]
// 断言的就是两个常量本身 —— clippy 说「这条断言是常值」正是本意:
// 这道守卫要在**编译期就把人拦住**,而不是等生产上再炸一次 500。
#[allow(clippy::assertions_on_constants)]
mod pool_sizing {
    //! 连接池不能再被调回「比一次开页的并发还小」。
    //!
    //! 2026-09-11 的事故:池子 5 条、逐合约小窗一次并发五个窗,占满之后同页
    //! 其它请求等 5 秒拿不到连接就 500,页面表现成「品种没有中文名 + 收藏全没了」。
    //! **那两个症状看着像丢数据,实际是连接池饥饿** —— 这条守卫钉的就是别再回去。
    use super::{ACQUIRE_TIMEOUT_SECS, MAX_CONNECTIONS};

    /// 逐合约小窗一次开几个(见 `HogMoney.vue` 的 `contracts_panel`),
    /// 加上同页还要发的「品种范围」「收藏」「引擎指纹」等。取 8 作下限。
    const ONE_PAGE_CONCURRENCY: u32 = 8;

    #[test]
    fn 池子要大于一次开页的并发数() {
        assert!(
            MAX_CONNECTIONS > ONE_PAGE_CONCURRENCY,
            "连接池 {MAX_CONNECTIONS} 条不够一次开页的 {ONE_PAGE_CONCURRENCY} 个并发请求用 ——              占满之后同页其它请求会等到 acquire_timeout 然后 500(2026-09-11 实际发生过)"
        );
    }

    #[test]
    fn 超时要留得住一次未命中的慢请求() {
        // 未命中时单个 net-position 实测 3.3 秒;超时必须显著大于它,
        // 否则排在后面的请求还是会被判死。
        assert!(
            ACQUIRE_TIMEOUT_SECS >= 10,
            "acquire_timeout {ACQUIRE_TIMEOUT_SECS}s 太短,排队的请求会被判成 500"
        );
    }
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
