//! Keeping a local copy of the spread history the page reads.
//!
//! The upstream returns a whole thirteen-year series on every call — its
//! request carries no date, so there is no way to ask it only for what is new.
//! Capturing the history is therefore one full pass over the combinations, and
//! staying current is the same pass repeated; the only thing that can be
//! incremental is what is kept, which the cache retention handles.
//!
//! Warming writes nothing but the provider cache. That is deliberate: the cache
//! is what a refused request falls back on, and everything else the page shows
//! is derived from it on demand. Skipping the derived tables means warming
//! needs no user, no workspace and no analytics pass.

use std::{collections::HashMap, sync::Arc};

use application::spread_analytics::ProviderEndpoint;
use time::OffsetDateTime;
use tracing::{info, warn};
use uuid::Uuid;

use crate::spread_analytics::{
    FreeSpreadLeg, FreeSpreadQueryRequest, SpreadAnalyticsState, warm_one_combination,
};

/// Varieties whose own cross-month spreads are kept warm.
const DEFAULT_SAME_VARIETY: &str = "苹果,鸡蛋,焦煤,玻璃,纯碱,生猪";
/// Cross-variety pairs, `first:second`, each covering every month combination.
const DEFAULT_CROSS_PAIRS: &str = "玻璃:纯碱";
/// Combinations refreshed per run.
///
/// A full pass is unnecessary daily: only the last few days of a thirteen-year
/// series change, and any combination someone actually opens is refreshed by
/// that view. The cap exists so a scheduled run is a predictable, modest number
/// of requests to a third party rather than a burst of several hundred.
const DEFAULT_BUDGET: usize = 250;

pub struct WarmSummary {
    pub considered: usize,
    pub attempted: usize,
    pub succeeded: usize,
    pub failed: usize,
}

/// One (variety, month) leg. The symbol is not part of the upstream request and
/// is left empty rather than guessed.
fn leg(variety: &str, month: &str) -> FreeSpreadLeg {
    FreeSpreadLeg {
        variety: variety.to_string(),
        symbol: String::new(),
        month: month.to_string(),
    }
}

fn env_list(name: &str, default: &str) -> Vec<String> {
    std::env::var(name)
        .unwrap_or_else(|_| default.to_string())
        .split(',')
        .map(|part| part.trim().to_string())
        .filter(|part| !part.is_empty())
        .collect()
}

/// Every combination to keep warm, in a stable order.
///
/// Same-variety pairs run in both month orders on purpose: 09-01 and 01-09 are
/// different spreads, not one spread and its negation — the first crosses into
/// the following year and the second does not, so they are built from different
/// contracts. Cross-variety pairs run one way only, because there the reverse
/// really is just the same series negated.
async fn combinations(
    state: &SpreadAnalyticsState,
    request_id: Uuid,
) -> Result<Vec<FreeSpreadQueryRequest>, anyhow::Error> {
    let mut months_by_variety: HashMap<String, Vec<String>> = HashMap::new();
    let same_variety = env_list("SPREAD_WARM_SAME_VARIETY", DEFAULT_SAME_VARIETY);
    let cross_pairs = env_list("SPREAD_WARM_CROSS_PAIRS", DEFAULT_CROSS_PAIRS);
    let mut wanted: Vec<String> = same_variety.clone();
    for pair in &cross_pairs {
        for side in pair.split(':') {
            wanted.push(side.trim().to_string());
        }
    }
    wanted.sort();
    wanted.dedup();

    for variety in &wanted {
        match crate::spread_analytics::warm_contract_months(state, variety, request_id).await {
            Ok(months) => {
                if months.is_empty() {
                    // The upstream carries no arbitrage months for this
                    // variety at all — gold and silver answer this way. Not an
                    // error, but it must not pass silently, or the page would
                    // simply never show them and nobody would know why.
                    warn!(
                        variety,
                        "upstream lists no arbitrage months for this variety"
                    );
                }
                months_by_variety.insert(variety.clone(), months);
            }
            Err(error) => {
                warn!(
                    variety,
                    ?error,
                    "could not list months; skipping this variety"
                );
            }
        }
    }

    Ok(build_combinations(
        &months_by_variety,
        &same_variety,
        &cross_pairs,
    ))
}

/// The pure half of the above: given the months, which pairs to keep warm.
fn build_combinations(
    months_by_variety: &HashMap<String, Vec<String>>,
    same_variety: &[String],
    cross_pairs: &[String],
) -> Vec<FreeSpreadQueryRequest> {
    let mut out = Vec::new();
    for variety in same_variety {
        let Some(months) = months_by_variety.get(variety) else {
            continue;
        };
        for first in months {
            for second in months {
                if first != second {
                    out.push(FreeSpreadQueryRequest {
                        provider: "sanhe".to_string(),
                        leg1: leg(variety, first),
                        leg2: leg(variety, second),
                    });
                }
            }
        }
    }
    for pair in cross_pairs {
        let Some((left, right)) = pair.split_once(':') else {
            warn!(pair, "cross pair is not in first:second form; skipped");
            continue;
        };
        let (left, right) = (left.trim(), right.trim());
        let (Some(left_months), Some(right_months)) =
            (months_by_variety.get(left), months_by_variety.get(right))
        else {
            continue;
        };
        for first in left_months {
            for second in right_months {
                out.push(FreeSpreadQueryRequest {
                    provider: "sanhe".to_string(),
                    leg1: leg(left, first),
                    leg2: leg(right, second),
                });
            }
        }
    }
    out
}

/// Refresh the stalest combinations, up to the budget.
pub async fn warm_spread_cache(state: Arc<SpreadAnalyticsState>) -> anyhow::Result<WarmSummary> {
    let request_id = Uuid::now_v7();
    let candidates = combinations(&state, request_id).await?;
    let considered = candidates.len();

    // Oldest first, never-fetched before everything else, so a first run
    // captures history and later runs top up whatever has drifted furthest.
    let mut ranked: Vec<(Option<OffsetDateTime>, FreeSpreadQueryRequest)> = Vec::new();
    for request in candidates {
        let hash = crate::spread_analytics::warm_parameter_hash(&request);
        let last = database::spread_analytics::get_latest_cache(
            &state.auth.pool,
            ProviderEndpoint::ArbitrageVarieties,
            &hash,
        )
        .await
        .ok()
        .flatten()
        .map(|cache| cache.fetched_at);
        ranked.push((last, request));
    }
    ranked.sort_by(|left, right| match (left.0, right.0) {
        (None, None) => std::cmp::Ordering::Equal,
        (None, Some(_)) => std::cmp::Ordering::Less,
        (Some(_), None) => std::cmp::Ordering::Greater,
        (Some(a), Some(b)) => a.cmp(&b),
    });

    let budget = std::env::var("SPREAD_WARM_BUDGET")
        .ok()
        .and_then(|raw| raw.parse::<usize>().ok())
        .unwrap_or(DEFAULT_BUDGET);

    let mut summary = WarmSummary {
        considered,
        attempted: 0,
        succeeded: 0,
        failed: 0,
    };
    for (_, request) in ranked.into_iter().take(budget) {
        summary.attempted += 1;
        match warm_one_combination(&state, &request, request_id).await {
            Ok(()) => summary.succeeded += 1,
            Err(error) => {
                summary.failed += 1;
                // One combination the upstream will not serve must not end the
                // run: the rest are independent and the next run retries this
                // one first, because it is now the stalest.
                warn!(
                    variety1 = request.leg1.variety,
                    code1 = request.leg1.month,
                    variety2 = request.leg2.variety,
                    code2 = request.leg2.month,
                    ?error,
                    "warm combination failed"
                );
            }
        }
    }
    info!(
        considered = summary.considered,
        attempted = summary.attempted,
        succeeded = summary.succeeded,
        failed = summary.failed,
        "SPREAD_WARM_SUMMARY"
    );
    Ok(summary)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn months(list: &[&str]) -> Vec<String> {
        list.iter().map(|month| month.to_string()).collect()
    }

    fn all_twelve() -> Vec<String> {
        months(&[
            "01", "02", "03", "04", "05", "06", "07", "08", "09", "10", "11", "12",
        ])
    }

    /// The months the upstream actually lists, measured 2026-08-09.
    fn upstream_months() -> HashMap<String, Vec<String>> {
        HashMap::from([
            (
                "苹果".to_string(),
                months(&["01", "03", "04", "05", "10", "11", "12"]),
            ),
            ("鸡蛋".to_string(), all_twelve()),
            ("焦煤".to_string(), all_twelve()),
            ("玻璃".to_string(), all_twelve()),
            ("纯碱".to_string(), all_twelve()),
            (
                "生猪".to_string(),
                months(&["01", "03", "05", "07", "09", "11"]),
            ),
            // Gold and silver list none: the upstream carries no arbitrage
            // months for them at all, so no combination can be built.
            ("黄金".to_string(), Vec::new()),
            ("白银".to_string(), Vec::new()),
        ])
    }

    fn names(list: &[&str]) -> Vec<String> {
        list.iter().map(|name| name.to_string()).collect()
    }

    #[test]
    fn same_variety_covers_both_month_orders_but_never_a_month_against_itself() {
        let built = build_combinations(
            &HashMap::from([("焦煤".to_string(), months(&["01", "09"]))]),
            &names(&["焦煤"]),
            &[],
        );
        let pairs: Vec<_> = built
            .iter()
            .map(|request| (request.leg1.month.as_str(), request.leg2.month.as_str()))
            .collect();
        // 09-01 and 01-09 are different spreads, not one and its negation: the
        // first crosses into the following year, the second does not.
        assert_eq!(pairs, vec![("01", "09"), ("09", "01")]);
    }

    #[test]
    fn a_cross_pair_runs_one_direction_only() {
        // Here the reverse really is the same series negated, so fetching it
        // would double the upstream requests for no extra information.
        let built = build_combinations(
            &HashMap::from([
                ("玻璃".to_string(), months(&["01", "05"])),
                ("纯碱".to_string(), months(&["09"])),
            ]),
            &[],
            &names(&["玻璃:纯碱"]),
        );
        assert_eq!(built.len(), 2);
        assert!(built.iter().all(|request| request.leg1.variety == "玻璃"));
        assert!(built.iter().all(|request| request.leg2.variety == "纯碱"));
    }

    #[test]
    fn a_variety_the_upstream_does_not_carry_contributes_nothing() {
        let built = build_combinations(
            &upstream_months(),
            &names(&["黄金", "白银"]),
            &names(&["黄金:白银"]),
        );
        assert!(built.is_empty());
    }

    #[test]
    fn an_unlisted_variety_is_skipped_rather_than_guessed() {
        // Absent from the map means the months lookup failed. Inventing a month
        // list would send requests the upstream has already said no to.
        let built = build_combinations(&HashMap::new(), &names(&["焦煤"]), &names(&["玻璃:纯碱"]));
        assert!(built.is_empty());
    }

    #[test]
    fn a_malformed_cross_pair_is_skipped_rather_than_taken_as_a_variety() {
        let built = build_combinations(&upstream_months(), &[], &names(&["玻璃-纯碱"]));
        assert!(built.is_empty());
    }

    #[test]
    fn the_requested_scope_is_the_measured_seven_hundred_and_forty_four() {
        // Same-variety cross-month for six varieties, plus every month
        // combination between glass and soda ash. Pinning the number keeps a
        // silent change of scope — or of the upstream's month lists — visible.
        let built = build_combinations(
            &upstream_months(),
            &names(&["苹果", "鸡蛋", "焦煤", "玻璃", "纯碱", "生猪"]),
            &names(&["玻璃:纯碱"]),
        );
        let same_variety = 7 * 6 + 12 * 11 * 4 + 6 * 5;
        let cross = 12 * 12;
        assert_eq!(same_variety, 600);
        assert_eq!(cross, 144);
        assert_eq!(built.len(), 744);
    }

    #[test]
    fn every_combination_is_distinct() {
        let built = build_combinations(
            &upstream_months(),
            &names(&["苹果", "鸡蛋", "焦煤", "玻璃", "纯碱", "生猪"]),
            &names(&["玻璃:纯碱"]),
        );
        let mut seen = std::collections::BTreeSet::new();
        for request in &built {
            let key = (
                request.leg1.variety.clone(),
                request.leg1.month.clone(),
                request.leg2.variety.clone(),
                request.leg2.month.clone(),
            );
            assert!(seen.insert(key), "a combination would be fetched twice");
        }
    }
}
