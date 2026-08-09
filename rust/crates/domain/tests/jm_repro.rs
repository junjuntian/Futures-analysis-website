use domain::spread_analytics::{ContractWindowInfo, RawSpreadPoint, calculate_windowed_analytics};
use rust_decimal::Decimal;
use std::collections::HashMap;
use time::Date;
use time::macros::{date, format_description};
use uuid::Uuid;

#[test]
#[ignore = "manual repro harness, needs JM_JSON env pointing at a live payload"]
fn jm_live_payload_repro() {
    let path = std::env::var("JM_JSON").expect("set JM_JSON");
    let raw: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let data = &raw["data"];
    let dates = data["dates"].as_array().unwrap();
    let spreads = data["spreads"].as_array().unwrap();
    let fmt = format_description!("[year]-[month]-[day]");
    let points: Vec<RawSpreadPoint> = dates
        .iter()
        .zip(spreads)
        .map(|(d, s)| RawSpreadPoint {
            trade_date: Date::parse(d.as_str().unwrap(), &fmt).unwrap(),
            value: s["value"].to_string().parse::<Decimal>().unwrap(),
            from_code: s["from_code"].as_str().unwrap().to_string(),
            to_code: s["to_code"].as_str().unwrap().to_string(),
        })
        .collect();
    let mk = |code: &str, year: i32, month: u8, deadline: Date| ContractWindowInfo {
        code: code.into(),
        instrument_code: "JM".into(),
        exchange_code: "DCE".into(),
        delivery_year: year,
        delivery_month: month,
        retail_deadline: deadline,
        calendar_version_id: Uuid::nil(),
    };
    let mut contracts = HashMap::new();
    contracts.insert(
        "JM2609".into(),
        mk("JM2609", 2026, 9, date!(2026 - 08 - 05)),
    );
    // JM2701 missing: its deadline month (2026-12) has no calendar rows in prod
    let cutoff = points.last().map(|p| p.trade_date);
    let result = calculate_windowed_analytics(&points, &contracts, cutoff).unwrap();
    println!(
        "quality: status={} input={} retained={} excluded={} missing_contract={}",
        result.quality.status,
        result.quality.input_point_count,
        result.quality.retained_point_count,
        result.quality.excluded_point_count,
        result.quality.missing_contract_point_count
    );
    println!(
        "segments={} continuous_points={} boundaries={}",
        result.segments.len(),
        result.continuous_points.len(),
        result.segment_boundaries.len()
    );
}

#[test]
#[ignore = "manual repro harness, needs JM_JSON env pointing at a live payload"]
fn jm_live_payload_segments_satisfy_stored_invariants() {
    let path = std::env::var("JM_JSON").expect("set JM_JSON");
    let raw: serde_json::Value =
        serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let data = &raw["data"];
    let fmt = format_description!("[year]-[month]-[day]");
    let points: Vec<RawSpreadPoint> = data["dates"]
        .as_array()
        .unwrap()
        .iter()
        .zip(data["spreads"].as_array().unwrap())
        .map(|(d, s)| RawSpreadPoint {
            trade_date: Date::parse(d.as_str().unwrap(), &fmt).unwrap(),
            value: s["value"].to_string().parse::<Decimal>().unwrap(),
            from_code: s["from_code"].as_str().unwrap().to_string(),
            to_code: s["to_code"].as_str().unwrap().to_string(),
        })
        .collect();
    let cutoff = points.last().map(|p| p.trade_date);
    let result = calculate_windowed_analytics(&points, &HashMap::new(), cutoff).unwrap();
    // Mirrors the database CHECK constraints that rejected the whole series and
    // surfaced as a 500 on the free-spread query.
    for segment in &result.segments {
        assert!(
            segment.candidate_end >= segment.candidate_start,
            "segment {} candidate range inverted",
            segment.segment_no
        );
        if let (Some(start), Some(end)) = (segment.window_start, segment.window_end) {
            assert!(
                end >= start,
                "segment {} window range inverted: {start} > {end}",
                segment.segment_no
            );
        }
    }
    println!(
        "segments={} retained={} ok",
        result.segments.len(),
        result.quality.retained_point_count
    );
}
