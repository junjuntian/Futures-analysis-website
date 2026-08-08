use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use std::collections::{BTreeMap, BTreeSet, HashMap};
use time::{Date, Month};
use utoipa::ToSchema;
use uuid::Uuid;

/// Stable API serialization for date-only values. `time::serde::iso8601` is
/// intentionally reserved for offset date-times in current `time` releases.
pub mod date_serde {
    use serde::Serializer;
    use time::Date;

    pub fn serialize<S>(date: &Date, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&date.to_string())
    }

    pub mod option {
        use serde::Serializer;
        use time::Date;

        pub fn serialize<S>(date: &Option<Date>, serializer: S) -> Result<S::Ok, S::Error>
        where
            S: Serializer,
        {
            match date {
                Some(date) => serializer.serialize_some(&date.to_string()),
                None => serializer.serialize_none(),
            }
        }
    }
}

pub const WINDOW_ALGORITHM_VERSION: &str = "retail_window_v1";
pub const STATISTICS_ALGORITHM_VERSION: &str = "spread_window_stats_v1";
pub const DEFAULT_RULE_VERSION: &str = "retail-window-default-v1";

#[derive(Debug, Clone, PartialEq)]
pub struct RawSpreadPoint {
    pub trade_date: Date,
    pub value: Decimal,
    pub from_code: String,
    pub to_code: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ContractWindowInfo {
    pub code: String,
    pub instrument_code: String,
    pub exchange_code: String,
    pub delivery_year: i32,
    pub delivery_month: u8,
    pub retail_deadline: Date,
    pub calendar_version_id: Uuid,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ExclusionReason {
    ContractMetadataMissing,
    OutsideRetailWindow,
    EmptyRetailWindow,
}

impl ExclusionReason {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ContractMetadataMissing => "contract_metadata_missing",
            Self::OutsideRetailWindow => "outside_retail_window",
            Self::EmptyRetailWindow => "empty_retail_window",
        }
    }
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct WindowedObservation {
    pub point_seq: u32,
    #[serde(serialize_with = "date_serde::serialize")]
    #[schema(value_type = String, format = Date)]
    pub trade_date: Date,
    #[schema(value_type = f64)]
    pub value: Decimal,
    pub from_code: String,
    pub to_code: String,
    pub segment_no: Option<u32>,
    pub retained: bool,
    pub exclusion_reason: Option<ExclusionReason>,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct ContinuousPoint {
    #[serde(serialize_with = "date_serde::serialize")]
    #[schema(value_type = String, format = Date)]
    pub trade_date: Date,
    #[schema(value_type = f64)]
    pub value: Decimal,
    pub from_code: String,
    pub to_code: String,
    pub segment_no: u32,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct SegmentBoundary {
    pub segment_no: u32,
    #[serde(serialize_with = "date_serde::serialize")]
    #[schema(value_type = String, format = Date)]
    pub trade_date: Date,
    pub from_code: String,
    pub to_code: String,
    pub previous_from_code: Option<String>,
    pub previous_to_code: Option<String>,
    pub reason: String,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct WindowSegment {
    pub segment_no: u32,
    pub window_year: Option<i32>,
    pub from_code: String,
    pub to_code: String,
    #[serde(serialize_with = "date_serde::serialize")]
    #[schema(value_type = String, format = Date)]
    pub candidate_start: Date,
    #[serde(serialize_with = "date_serde::serialize")]
    #[schema(value_type = String, format = Date)]
    pub candidate_end: Date,
    #[serde(serialize_with = "date_serde::option::serialize")]
    #[schema(value_type = Option<String>, format = Date)]
    pub window_start: Option<Date>,
    #[serde(serialize_with = "date_serde::option::serialize")]
    #[schema(value_type = Option<String>, format = Date)]
    pub window_end: Option<Date>,
    pub calendar_version_ids: Vec<Uuid>,
    pub retained_point_count: u32,
    pub excluded_point_count: u32,
    pub boundary_reason: String,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct SeasonalYearSeries {
    pub year: i32,
    #[schema(value_type = Vec<Option<f64>>)]
    pub values: Vec<Option<Decimal>>,
    pub sample_count: u32,
    pub missing_count: u32,
    pub segment_nos: Vec<u32>,
    pub rule_version: String,
    #[serde(serialize_with = "date_serde::option::serialize")]
    #[schema(value_type = Option<String>, format = Date)]
    pub sample_start: Option<Date>,
    #[serde(serialize_with = "date_serde::option::serialize")]
    #[schema(value_type = Option<String>, format = Date)]
    pub sample_end: Option<Date>,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct SeasonalSeries {
    pub axis: Vec<String>,
    pub years: Vec<SeasonalYearSeries>,
    pub current_year: Option<i32>,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct MonthlyCell {
    pub month: u8,
    #[schema(value_type = Option<f64>)]
    pub delta: Option<Decimal>,
    pub sample_count: u32,
    pub is_partial: bool,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct MonthlyYearRow {
    pub year: i32,
    pub months: Vec<MonthlyCell>,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct MonthlyUpRatio {
    pub month: u8,
    pub ratio: Option<f64>,
    pub positive_year_count: u32,
    pub eligible_year_count: u32,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct MonthlyMatrix {
    pub years: Vec<MonthlyYearRow>,
    pub up_ratios: Vec<MonthlyUpRatio>,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct WindowQuality {
    pub status: String,
    pub input_point_count: u32,
    pub retained_point_count: u32,
    pub excluded_point_count: u32,
    pub missing_contract_point_count: u32,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct WindowedSpreadAnalytics {
    pub observations: Vec<WindowedObservation>,
    pub continuous_points: Vec<ContinuousPoint>,
    pub segment_boundaries: Vec<SegmentBoundary>,
    pub segments: Vec<WindowSegment>,
    pub seasonal: SeasonalSeries,
    pub monthly: MonthlyMatrix,
    #[schema(value_type = Option<f64>)]
    pub current_value: Option<Decimal>,
    pub quality: WindowQuality,
}

#[derive(Debug, thiserror::Error, PartialEq, Eq)]
pub enum WindowError {
    #[error("spread dates must be strictly increasing")]
    DatesNotStrictlyIncreasing,
    #[error("contract code must not be blank")]
    BlankContractCode,
}

pub fn calculate_windowed_analytics(
    points: &[RawSpreadPoint],
    contracts: &HashMap<String, ContractWindowInfo>,
    data_cutoff_at: Option<Date>,
) -> Result<WindowedSpreadAnalytics, WindowError> {
    validate_points(points)?;
    if points.is_empty() {
        return Ok(empty_analytics());
    }

    // The stored catalog only holds contracts the collector has actually seen,
    // so historical legs (e.g. jm1309 from 2013) are absent and every point
    // that references them would be dropped, leaving only the most recent
    // contract cycle. Chinese exchange codes encode the delivery month
    // (letters + YYMM), so derive a window for any leg missing from the
    // catalog. Catalog rows always take precedence; the derived deadline is
    // the last weekday before the delivery month, which only ever lands on
    // holidays that carry no price points, so window contents are unchanged.
    let contracts = {
        let mut resolved = contracts.clone();
        for point in points {
            for code in [&point.from_code, &point.to_code] {
                let key = code.to_ascii_uppercase();
                if let std::collections::hash_map::Entry::Vacant(entry) = resolved.entry(key)
                    && let Some(info) = derive_contract_window(entry.key())
                {
                    entry.insert(info);
                }
            }
        }
        resolved
    };
    let contracts = &contracts;

    let mut observations = Vec::with_capacity(points.len());
    let mut continuous_points = Vec::new();
    let mut segment_boundaries = Vec::new();
    let mut segments = Vec::new();
    let mut cursor = 0usize;
    let mut segment_no = 0u32;
    let mut previous_retained_pair: Option<(String, String)> = None;

    while cursor < points.len() {
        let start = cursor;
        let pair = (
            points[cursor].from_code.to_ascii_uppercase(),
            points[cursor].to_code.to_ascii_uppercase(),
        );
        cursor += 1;
        while cursor < points.len()
            && points[cursor].from_code.eq_ignore_ascii_case(&pair.0)
            && points[cursor].to_code.eq_ignore_ascii_case(&pair.1)
        {
            cursor += 1;
        }
        segment_no += 1;
        let segment_points = &points[start..cursor];
        let from = contracts.get(&pair.0);
        let to = contracts.get(&pair.1);
        let candidate_start = segment_points[0].trade_date;
        let candidate_end = segment_points[segment_points.len() - 1].trade_date;

        let (window_year, window_end, calendars) = match (from, to) {
            (Some(from), Some(to)) => {
                let from_delivery = (from.delivery_year, from.delivery_month);
                let to_delivery = (to.delivery_year, to.delivery_month);
                let (year, deadline) = if from_delivery < to_delivery {
                    (from.delivery_year, from.retail_deadline)
                } else if to_delivery < from_delivery {
                    (to.delivery_year, to.retail_deadline)
                } else {
                    (
                        from.delivery_year,
                        from.retail_deadline.min(to.retail_deadline),
                    )
                };
                let mut ids = vec![from.calendar_version_id, to.calendar_version_id];
                ids.sort_unstable();
                ids.dedup();
                (Some(year), Some(deadline), ids)
            }
            _ => (None, None, Vec::new()),
        };

        let window_start = (from.is_some() && to.is_some()).then_some(candidate_start);
        let empty_window =
            matches!((window_start, window_end), (Some(start), Some(end)) if start > end);
        let mut retained_count = 0u32;
        let mut excluded_count = 0u32;
        let mut first_retained_date = None;

        for (offset, point) in segment_points.iter().enumerate() {
            let reason = if from.is_none() || to.is_none() {
                Some(ExclusionReason::ContractMetadataMissing)
            } else if empty_window {
                Some(ExclusionReason::EmptyRetailWindow)
            } else if window_end.is_some_and(|end| point.trade_date > end) {
                Some(ExclusionReason::OutsideRetailWindow)
            } else {
                None
            };
            let retained = reason.is_none();
            if retained {
                retained_count += 1;
                first_retained_date.get_or_insert(point.trade_date);
                continuous_points.push(ContinuousPoint {
                    trade_date: point.trade_date,
                    value: point.value,
                    from_code: pair.0.clone(),
                    to_code: pair.1.clone(),
                    segment_no,
                });
            } else {
                excluded_count += 1;
            }
            observations.push(WindowedObservation {
                point_seq: u32::try_from(start + offset + 1).unwrap_or(u32::MAX),
                trade_date: point.trade_date,
                value: point.value,
                from_code: pair.0.clone(),
                to_code: pair.1.clone(),
                segment_no: retained.then_some(segment_no),
                retained,
                exclusion_reason: reason,
            });
        }

        if let Some(trade_date) = first_retained_date {
            segment_boundaries.push(SegmentBoundary {
                segment_no,
                trade_date,
                from_code: pair.0.clone(),
                to_code: pair.1.clone(),
                previous_from_code: previous_retained_pair
                    .as_ref()
                    .map(|previous| previous.0.clone()),
                previous_to_code: previous_retained_pair
                    .as_ref()
                    .map(|previous| previous.1.clone()),
                reason: if previous_retained_pair.is_some() {
                    "contract_roll".to_string()
                } else {
                    "series_start".to_string()
                },
            });
            previous_retained_pair = Some(pair.clone());
        }

        segments.push(WindowSegment {
            segment_no,
            window_year,
            from_code: pair.0,
            to_code: pair.1,
            candidate_start,
            candidate_end,
            window_start,
            window_end,
            calendar_version_ids: calendars,
            retained_point_count: retained_count,
            excluded_point_count: excluded_count,
            boundary_reason: if from.is_none() || to.is_none() {
                "contract_metadata_missing".to_string()
            } else if empty_window {
                "empty_retail_window".to_string()
            } else {
                "retail_deadline".to_string()
            },
        });
    }

    let seasonal = build_seasonal(&continuous_points, &segments);
    let monthly = build_monthly(&continuous_points, &segments, data_cutoff_at);
    let current_value = continuous_points.last().map(|point| point.value);
    let missing_contract_point_count = observations
        .iter()
        .filter(|point| point.exclusion_reason == Some(ExclusionReason::ContractMetadataMissing))
        .count();
    let retained_point_count = continuous_points.len();
    let quality = WindowQuality {
        status: if retained_point_count == 0 {
            "empty".to_string()
        } else if missing_contract_point_count > 0 {
            "partial".to_string()
        } else {
            "ok".to_string()
        },
        input_point_count: count_u32(points.len()),
        retained_point_count: count_u32(retained_point_count),
        excluded_point_count: count_u32(points.len().saturating_sub(retained_point_count)),
        missing_contract_point_count: count_u32(missing_contract_point_count),
    };

    Ok(WindowedSpreadAnalytics {
        observations,
        continuous_points,
        segment_boundaries,
        segments,
        seasonal,
        monthly,
        current_value,
        quality,
    })
}

/// Derive a contract window from an exchange code of the form `<letters><YYMM>`
/// (e.g. `JM1309` -> delivery 2013-09). Returns `None` when the code does not
/// match that shape, letting the caller exclude the point instead of guessing.
/// The exchange is unknown from the code alone, so the calendar version is nil
/// and the deadline is the last weekday of the month before delivery.
fn derive_contract_window(code: &str) -> Option<ContractWindowInfo> {
    let digits_start = code.find(|c: char| c.is_ascii_digit())?;
    let (letters, digits) = code.split_at(digits_start);
    if letters.is_empty() || digits.len() != 4 || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return None;
    }
    let year = 2000 + digits[0..2].parse::<i32>().ok()?;
    let month = digits[2..4].parse::<u8>().ok()?;
    if !(1..=12).contains(&month) {
        return None;
    }
    let retail_deadline = last_weekday_before_delivery(year, month)?;
    Some(ContractWindowInfo {
        code: code.to_string(),
        instrument_code: letters.to_string(),
        exchange_code: String::new(),
        delivery_year: year,
        delivery_month: month,
        retail_deadline,
        calendar_version_id: Uuid::nil(),
    })
}

/// Last non-weekend day of the month before the delivery month. Divergence from
/// the true exchange calendar only occurs on holidays, which carry no price
/// points, so retail-window trimming is unaffected.
fn last_weekday_before_delivery(delivery_year: i32, delivery_month: u8) -> Option<Date> {
    let (year, month) = if delivery_month == 1 {
        (delivery_year - 1, 12u8)
    } else {
        (delivery_year, delivery_month - 1)
    };
    let month = Month::try_from(month).ok()?;
    let first = Date::from_calendar_date(year, month, 1).ok()?;
    let mut day = first
        .checked_add(time::Duration::days(31))?
        .replace_day(1)
        .ok()?
        .checked_sub(time::Duration::days(1))?;
    while matches!(
        day.weekday(),
        time::Weekday::Saturday | time::Weekday::Sunday
    ) {
        day = day.checked_sub(time::Duration::days(1))?;
    }
    Some(day)
}

fn validate_points(points: &[RawSpreadPoint]) -> Result<(), WindowError> {
    // Dates must increase strictly WITHIN a contract-pair segment.  Across a
    // segment boundary the upstream legitimately repeats the roll date: on the
    // day the pair rolls (e.g. jm1909&jm2001 -> jm2009&jm2101) it returns one
    // point for the old pair and one for the new pair with the same trade
    // date, so the whole-series strictness check rejected real data.
    let mut previous: Option<Date> = None;
    let mut current_pair: Option<(String, String)> = None;
    for point in points {
        if point.from_code.trim().is_empty() || point.to_code.trim().is_empty() {
            return Err(WindowError::BlankContractCode);
        }
        let pair = (
            point.from_code.to_ascii_uppercase(),
            point.to_code.to_ascii_uppercase(),
        );
        if current_pair.as_ref() != Some(&pair) {
            current_pair = Some(pair);
            previous = None;
        }
        if previous.is_some_and(|date| point.trade_date <= date) {
            return Err(WindowError::DatesNotStrictlyIncreasing);
        }
        previous = Some(point.trade_date);
    }
    Ok(())
}

fn empty_analytics() -> WindowedSpreadAnalytics {
    WindowedSpreadAnalytics {
        observations: Vec::new(),
        continuous_points: Vec::new(),
        segment_boundaries: Vec::new(),
        segments: Vec::new(),
        seasonal: SeasonalSeries {
            axis: Vec::new(),
            years: Vec::new(),
            current_year: None,
        },
        monthly: MonthlyMatrix {
            years: Vec::new(),
            up_ratios: (1..=12)
                .map(|month| MonthlyUpRatio {
                    month,
                    ratio: None,
                    positive_year_count: 0,
                    eligible_year_count: 0,
                })
                .collect(),
        },
        current_value: None,
        quality: WindowQuality {
            status: "empty".to_string(),
            input_point_count: 0,
            retained_point_count: 0,
            excluded_point_count: 0,
            missing_contract_point_count: 0,
        },
    }
}

fn build_seasonal(points: &[ContinuousPoint], segments: &[WindowSegment]) -> SeasonalSeries {
    let segment_years: HashMap<u32, i32> = segments
        .iter()
        .filter_map(|segment| segment.window_year.map(|year| (segment.segment_no, year)))
        .collect();
    let current_year = segment_years.values().copied().max();
    let anchor = current_year.and_then(|year| {
        points
            .iter()
            .filter(|point| segment_years.get(&point.segment_no) == Some(&year))
            .map(|point| month_day(point.trade_date))
            .next()
    });
    let mut axis_keys = BTreeSet::new();
    let mut grouped: BTreeMap<i32, BTreeMap<String, (Date, Decimal)>> = BTreeMap::new();
    let mut segments_by_year: BTreeMap<i32, Vec<u32>> = BTreeMap::new();
    for segment in segments {
        if let Some(year) = segment.window_year {
            segments_by_year
                .entry(year)
                .or_default()
                .push(segment.segment_no);
        }
    }
    for point in points {
        let Some(year) = segment_years.get(&point.segment_no).copied() else {
            continue;
        };
        let key = month_day(point.trade_date);
        axis_keys.insert(key.clone());
        grouped
            .entry(year)
            .or_default()
            .insert(key, (point.trade_date, point.value));
    }
    let anchor_ordinal = anchor.as_deref().map(month_day_ordinal).unwrap_or(1);
    let mut axis: Vec<_> = axis_keys.into_iter().collect();
    axis.sort_by_key(|key| {
        let ordinal = month_day_ordinal(key);
        if ordinal >= anchor_ordinal {
            ordinal - anchor_ordinal
        } else {
            366 + ordinal - anchor_ordinal
        }
    });
    let years = grouped
        .into_iter()
        .map(|(year, values)| {
            let dates: Vec<_> = values.values().map(|(date, _)| *date).collect();
            SeasonalYearSeries {
                year,
                values: axis
                    .iter()
                    .map(|key| values.get(key).map(|(_, value)| *value))
                    .collect(),
                sample_count: count_u32(values.len()),
                missing_count: count_u32(axis.len().saturating_sub(values.len())),
                segment_nos: segments_by_year.remove(&year).unwrap_or_default(),
                rule_version: DEFAULT_RULE_VERSION.to_string(),
                sample_start: dates.iter().min().copied(),
                sample_end: dates.iter().max().copied(),
            }
        })
        .collect();
    SeasonalSeries {
        axis,
        years,
        current_year,
    }
}

fn build_monthly(
    points: &[ContinuousPoint],
    segments: &[WindowSegment],
    data_cutoff_at: Option<Date>,
) -> MonthlyMatrix {
    let segment_years: HashMap<u32, i32> = segments
        .iter()
        .filter_map(|segment| segment.window_year.map(|year| (segment.segment_no, year)))
        .collect();
    let mut grouped: BTreeMap<i32, BTreeMap<u8, Vec<&ContinuousPoint>>> = BTreeMap::new();
    for point in points {
        let Some(year) = segment_years.get(&point.segment_no).copied() else {
            continue;
        };
        grouped
            .entry(year)
            .or_default()
            .entry(point.trade_date.month() as u8)
            .or_default()
            .push(point);
    }
    let years: Vec<_> = grouped
        .into_iter()
        .map(|(year, months)| {
            let cells = (1..=12)
                .map(|month| {
                    let month_points = months.get(&month);
                    let delta = month_points.and_then(|items| {
                        (items.len() >= 2).then(|| items[items.len() - 1].value - items[0].value)
                    });
                    let is_partial = month_points
                        .and_then(|items| items.last())
                        .zip(data_cutoff_at)
                        .is_some_and(|(last, cutoff)| {
                            last.trade_date.year() == cutoff.year()
                                && last.trade_date.month() == cutoff.month()
                        });
                    MonthlyCell {
                        month,
                        delta,
                        sample_count: month_points.map_or(0, |items| count_u32(items.len())),
                        is_partial,
                    }
                })
                .collect();
            MonthlyYearRow {
                year,
                months: cells,
            }
        })
        .collect();
    let up_ratios = (1..=12)
        .map(|month| {
            let values: Vec<_> = years
                .iter()
                .filter_map(|row| row.months[usize::from(month - 1)].delta)
                .collect();
            let positive_year_count = values
                .iter()
                .filter(|value| **value > Decimal::ZERO)
                .count();
            let eligible_year_count = values.len();
            MonthlyUpRatio {
                month,
                ratio: (eligible_year_count > 0)
                    .then(|| positive_year_count as f64 / eligible_year_count as f64),
                positive_year_count: count_u32(positive_year_count),
                eligible_year_count: count_u32(eligible_year_count),
            }
        })
        .collect();
    MonthlyMatrix { years, up_ratios }
}

fn month_day(date: Date) -> String {
    format!("{:02}-{:02}", date.month() as u8, date.day())
}

fn month_day_ordinal(value: &str) -> u16 {
    let (month, day) = value
        .split_once('-')
        .and_then(|(month, day)| Some((month.parse::<u8>().ok()?, day.parse::<u8>().ok()?)))
        .unwrap_or((1, 1));
    let month = Month::try_from(month).unwrap_or(Month::January);
    Date::from_calendar_date(2000, month, day)
        .map(Date::ordinal)
        .unwrap_or(1)
}

fn count_u32(value: usize) -> u32 {
    u32::try_from(value).unwrap_or(u32::MAX)
}

#[cfg(test)]
mod tests {
    use super::*;
    use time::macros::date;

    fn contract(
        code: &str,
        delivery_year: i32,
        delivery_month: u8,
        deadline: Date,
    ) -> ContractWindowInfo {
        ContractWindowInfo {
            code: code.to_string(),
            instrument_code: code
                .chars()
                .take_while(|c| c.is_ascii_alphabetic())
                .collect(),
            exchange_code: "DCE".to_string(),
            delivery_year,
            delivery_month,
            retail_deadline: deadline,
            calendar_version_id: Uuid::now_v7(),
        }
    }

    fn point(date: Date, value: f64, from: &str, to: &str) -> RawSpreadPoint {
        RawSpreadPoint {
            trade_date: date,
            value: value.to_string().parse().expect("fixture decimal"),
            from_code: from.to_string(),
            to_code: to.to_string(),
        }
    }

    fn decimal_point(date: Date, value: &str, from: &str, to: &str) -> RawSpreadPoint {
        RawSpreadPoint {
            trade_date: date,
            value: value.parse().expect("fixture decimal"),
            from_code: from.to_string(),
            to_code: to.to_string(),
        }
    }

    #[test]
    fn trims_each_segment_at_the_earlier_delivery_leg_deadline() {
        let mut contracts = HashMap::new();
        contracts.insert(
            "JM2509".into(),
            contract("JM2509", 2025, 9, date!(2025 - 08 - 29)),
        );
        contracts.insert(
            "JM2601".into(),
            contract("JM2601", 2026, 1, date!(2025 - 12 - 31)),
        );
        let points = vec![
            point(date!(2025 - 08 - 28), -2.0, "jm2509", "jm2601"),
            point(date!(2025 - 08 - 29), 1.0, "jm2509", "jm2601"),
            point(date!(2025 - 09 - 01), 3.0, "jm2509", "jm2601"),
        ];
        let result =
            calculate_windowed_analytics(&points, &contracts, Some(date!(2025 - 09 - 01))).unwrap();
        assert_eq!(result.continuous_points.len(), 2);
        assert_eq!(result.quality.excluded_point_count, 1);
        assert_eq!(result.segments[0].window_end, Some(date!(2025 - 08 - 29)));
        assert_eq!(
            result.observations[2].exclusion_reason,
            Some(ExclusionReason::OutsideRetailWindow)
        );
    }

    #[test]
    fn unparseable_contract_code_is_excluded_instead_of_guessed() {
        // A code that cannot be parsed into <letters><YYMM> has no derivable
        // delivery month, so the point is excluded rather than guessed.
        let result = calculate_windowed_analytics(
            &[point(date!(2025 - 01 - 02), 10.0, "MYSTERY", "OTHER")],
            &HashMap::new(),
            None,
        )
        .unwrap();
        assert!(result.continuous_points.is_empty());
        assert_eq!(result.quality.status, "empty");
        assert_eq!(result.quality.missing_contract_point_count, 1);
    }

    #[test]
    fn cross_year_seasonal_axis_keeps_business_cycle_order() {
        let mut contracts = HashMap::new();
        contracts.insert(
            "FG2501".into(),
            contract("FG2501", 2025, 1, date!(2024 - 12 - 31)),
        );
        contracts.insert(
            "SA2505".into(),
            contract("SA2505", 2025, 5, date!(2025 - 04 - 30)),
        );
        let points = vec![
            point(date!(2024 - 12 - 30), 1.0, "FG2501", "SA2505"),
            point(date!(2024 - 12 - 31), 2.0, "FG2501", "SA2505"),
        ];
        let result = calculate_windowed_analytics(&points, &contracts, None).unwrap();
        assert_eq!(result.seasonal.axis, vec!["12-30", "12-31"]);
        assert_eq!(result.seasonal.years[0].year, 2025);
    }

    #[test]
    fn monthly_matrix_uses_last_minus_first_and_zero_stays_in_denominator() {
        let mut contracts = HashMap::new();
        contracts.insert(
            "A2505".into(),
            contract("A2505", 2025, 5, date!(2025 - 04 - 30)),
        );
        contracts.insert(
            "B2509".into(),
            contract("B2509", 2025, 9, date!(2025 - 08 - 29)),
        );
        let points = vec![
            point(date!(2025 - 01 - 02), 10.0, "A2505", "B2509"),
            point(date!(2025 - 01 - 31), 12.0, "A2505", "B2509"),
            point(date!(2025 - 02 - 03), 8.0, "A2505", "B2509"),
            point(date!(2025 - 02 - 28), 8.0, "A2505", "B2509"),
        ];
        let result = calculate_windowed_analytics(&points, &contracts, None).unwrap();
        assert_eq!(
            result.monthly.years[0].months[0].delta,
            Some(Decimal::from(2))
        );
        assert_eq!(result.monthly.years[0].months[1].delta, Some(Decimal::ZERO));
        assert_eq!(result.monthly.up_ratios[0].ratio, Some(1.0));
        assert_eq!(result.monthly.up_ratios[1].ratio, Some(0.0));
    }

    #[test]
    fn rejects_duplicate_or_out_of_order_dates() {
        let points = vec![
            point(date!(2025 - 01 - 02), 1.0, "A2505", "B2505"),
            point(date!(2025 - 01 - 02), 2.0, "A2505", "B2505"),
        ];
        assert!(matches!(
            calculate_windowed_analytics(&points, &HashMap::new(), None),
            Err(WindowError::DatesNotStrictlyIncreasing)
        ));
    }

    #[test]
    fn derives_window_for_historical_leg_absent_from_catalog() {
        // No catalog rows at all: historical jm codes must still resolve via
        // code derivation so the full multi-year history is retained rather
        // than dropped as missing metadata.
        let points = vec![
            point(date!(2013 - 03 - 22), -53.0, "jm1309", "jm1401"),
            point(date!(2013 - 03 - 25), -47.0, "jm1309", "jm1401"),
        ];
        let result =
            calculate_windowed_analytics(&points, &HashMap::new(), Some(date!(2013 - 03 - 25)))
                .unwrap();
        assert_eq!(result.quality.missing_contract_point_count, 0);
        assert_eq!(result.continuous_points.len(), 2);
        assert_eq!(result.segments[0].window_year, Some(2013));
    }

    #[test]
    fn derive_contract_window_parses_code_shapes() {
        let jm = derive_contract_window("JM1309").unwrap();
        assert_eq!(jm.instrument_code, "JM");
        assert_eq!((jm.delivery_year, jm.delivery_month), (2013, 9));
        assert_eq!(jm.retail_deadline, date!(2013 - 08 - 30));
        let jan = derive_contract_window("A2701").unwrap();
        assert_eq!((jan.delivery_year, jan.delivery_month), (2027, 1));
        assert_eq!(jan.retail_deadline, date!(2026 - 12 - 31));
        assert!(derive_contract_window("JM13").is_none());
        assert!(derive_contract_window("1309").is_none());
        assert!(derive_contract_window("JM1399").is_none());
    }

    #[test]
    fn catalog_row_takes_precedence_over_derivation() {
        let mut contracts = HashMap::new();
        contracts.insert(
            "JM2509".into(),
            contract("JM2509", 2025, 9, date!(2025 - 08 - 15)),
        );
        contracts.insert(
            "JM2601".into(),
            contract("JM2601", 2026, 1, date!(2025 - 12 - 31)),
        );
        let points = vec![
            point(date!(2025 - 08 - 14), 1.0, "jm2509", "jm2601"),
            point(date!(2025 - 08 - 15), 2.0, "jm2509", "jm2601"),
            point(date!(2025 - 08 - 18), 3.0, "jm2509", "jm2601"),
        ];
        let result =
            calculate_windowed_analytics(&points, &contracts, Some(date!(2025 - 08 - 18))).unwrap();
        // Catalog deadline 2025-08-15 (not the derived 2025-08-29) governs.
        assert_eq!(result.segments[0].window_end, Some(date!(2025 - 08 - 15)));
        assert_eq!(result.quality.retained_point_count, 2);
    }

    #[test]
    fn accepts_repeated_roll_date_across_segment_boundary() {
        // Upstream returns the roll date twice: once for the outgoing pair and
        // once for the incoming pair (observed live on jm 09-01, e.g.
        // 2019-09-17).  That must not be rejected as out-of-order data, and the
        // outgoing pair's point past its retail deadline is excluded by the
        // window rule rather than by validation.
        let mut contracts = HashMap::new();
        contracts.insert(
            "JM1909".into(),
            contract("JM1909", 2019, 9, date!(2019 - 08 - 30)),
        );
        contracts.insert(
            "JM2001".into(),
            contract("JM2001", 2020, 1, date!(2019 - 12 - 31)),
        );
        contracts.insert(
            "JM2009".into(),
            contract("JM2009", 2020, 9, date!(2020 - 08 - 31)),
        );
        contracts.insert(
            "JM2101".into(),
            contract("JM2101", 2021, 1, date!(2020 - 12 - 31)),
        );
        let points = vec![
            point(date!(2019 - 08 - 29), -50.0, "jm1909", "jm2001"),
            point(date!(2019 - 09 - 17), -53.0, "jm1909", "jm2001"),
            point(date!(2019 - 09 - 17), -40.0, "jm2009", "jm2101"),
            point(date!(2019 - 09 - 18), -41.0, "jm2009", "jm2101"),
        ];
        let result =
            calculate_windowed_analytics(&points, &contracts, Some(date!(2019 - 09 - 18))).unwrap();
        assert_eq!(result.segments.len(), 2);
        assert_eq!(
            result.observations[1].exclusion_reason,
            Some(ExclusionReason::OutsideRetailWindow)
        );
        assert!(result.observations[2].retained);
        assert!(result.observations[3].retained);
    }

    #[test]
    fn cross_year_axis_orders_december_before_january_for_one_window_year() {
        let mut contracts = HashMap::new();
        contracts.insert(
            "A2509".into(),
            contract("A2509", 2025, 9, date!(2025 - 08 - 29)),
        );
        contracts.insert(
            "B2512".into(),
            contract("B2512", 2025, 12, date!(2025 - 11 - 28)),
        );
        let points = vec![
            point(date!(2024 - 12 - 30), 1.0, "A2509", "B2512"),
            point(date!(2025 - 01 - 02), 2.0, "A2509", "B2512"),
        ];
        let result = calculate_windowed_analytics(&points, &contracts, None).unwrap();
        assert_eq!(result.seasonal.axis, vec!["12-30", "01-02"]);
        assert_eq!(result.seasonal.years[0].year, 2025);
    }

    #[test]
    fn every_actual_leg_change_starts_a_new_segment() {
        let mut contracts = HashMap::new();
        for code in ["A2505", "A2509", "B2509", "B2512"] {
            let month = code[code.len() - 2..].parse::<u8>().unwrap();
            contracts.insert(
                code.into(),
                contract(code, 2025, month, date!(2025 - 04 - 30)),
            );
        }
        let points = vec![
            point(date!(2025 - 01 - 02), 1.0, "A2505", "B2509"),
            point(date!(2025 - 01 - 03), 2.0, "A2509", "B2509"),
            point(date!(2025 - 01 - 06), 3.0, "A2509", "B2512"),
        ];
        let result = calculate_windowed_analytics(&points, &contracts, None).unwrap();
        assert_eq!(result.segments.len(), 3);
        assert_eq!(result.segment_boundaries.len(), 3);
        assert_eq!(result.continuous_points[2].segment_no, 3);
    }

    #[test]
    fn a_segment_starting_after_its_deadline_is_a_deterministic_empty_window() {
        let mut contracts = HashMap::new();
        contracts.insert(
            "A2501".into(),
            contract("A2501", 2025, 1, date!(2024 - 12 - 31)),
        );
        contracts.insert(
            "B2505".into(),
            contract("B2505", 2025, 5, date!(2025 - 04 - 30)),
        );
        let result = calculate_windowed_analytics(
            &[point(date!(2025 - 01 - 02), 1.0, "A2501", "B2505")],
            &contracts,
            None,
        )
        .unwrap();
        assert!(result.continuous_points.is_empty());
        assert_eq!(
            result.observations[0].exclusion_reason,
            Some(ExclusionReason::EmptyRetailWindow)
        );
    }

    #[test]
    fn financial_deltas_remain_exact_decimal_values() {
        let mut contracts = HashMap::new();
        contracts.insert(
            "A2505".into(),
            contract("A2505", 2025, 5, date!(2025 - 04 - 30)),
        );
        contracts.insert(
            "B2509".into(),
            contract("B2509", 2025, 9, date!(2025 - 08 - 29)),
        );
        let points = vec![
            decimal_point(date!(2025 - 01 - 02), "0.1", "A2505", "B2509"),
            decimal_point(date!(2025 - 01 - 03), "0.3", "A2505", "B2509"),
        ];
        let result = calculate_windowed_analytics(&points, &contracts, None).unwrap();
        assert_eq!(
            result.monthly.years[0].months[0].delta,
            Some("0.2".parse().unwrap())
        );
    }

    #[test]
    fn later_listing_is_implicit_in_the_first_valid_two_leg_spread_point() {
        let mut contracts = HashMap::new();
        contracts.insert(
            "A2505".into(),
            contract("A2505", 2025, 5, date!(2025 - 04 - 30)),
        );
        contracts.insert(
            "B2509".into(),
            contract("B2509", 2025, 9, date!(2025 - 08 - 29)),
        );
        let points = vec![
            point(date!(2025 - 02 - 10), 1.0, "A2505", "B2509"),
            point(date!(2025 - 02 - 11), 2.0, "A2505", "B2509"),
        ];
        let result = calculate_windowed_analytics(&points, &contracts, None).unwrap();
        assert_eq!(result.segments[0].window_start, Some(date!(2025 - 02 - 10)));
        assert_eq!(
            result.continuous_points[0].trade_date,
            date!(2025 - 02 - 10)
        );
    }

    #[test]
    fn one_point_month_is_blank_and_cutoff_month_is_marked_partial() {
        let mut contracts = HashMap::new();
        contracts.insert(
            "A2505".into(),
            contract("A2505", 2025, 5, date!(2025 - 04 - 30)),
        );
        contracts.insert(
            "B2509".into(),
            contract("B2509", 2025, 9, date!(2025 - 08 - 29)),
        );
        let points = vec![
            point(date!(2025 - 02 - 28), 1.0, "A2505", "B2509"),
            point(date!(2025 - 03 - 03), 2.0, "A2505", "B2509"),
            point(date!(2025 - 03 - 04), 4.0, "A2505", "B2509"),
        ];
        let result =
            calculate_windowed_analytics(&points, &contracts, Some(date!(2025 - 03 - 04))).unwrap();
        assert_eq!(result.monthly.years[0].months[1].delta, None);
        assert_eq!(result.monthly.years[0].months[1].sample_count, 1);
        assert!(result.monthly.years[0].months[2].is_partial);
        assert_eq!(
            result.monthly.years[0].months[2].delta,
            Some(Decimal::from(2))
        );
    }

    #[test]
    fn seasonal_year_trace_records_missing_axis_slots_segments_and_rule() {
        let mut contracts = HashMap::new();
        contracts.insert(
            "A2505".into(),
            contract("A2505", 2025, 5, date!(2025 - 04 - 30)),
        );
        contracts.insert(
            "B2509".into(),
            contract("B2509", 2025, 9, date!(2025 - 08 - 29)),
        );
        contracts.insert(
            "A2605".into(),
            contract("A2605", 2026, 5, date!(2026 - 04 - 30)),
        );
        contracts.insert(
            "B2609".into(),
            contract("B2609", 2026, 9, date!(2026 - 08 - 31)),
        );
        let points = vec![
            point(date!(2025 - 01 - 02), 1.0, "A2505", "B2509"),
            point(date!(2026 - 01 - 02), 2.0, "A2605", "B2609"),
            point(date!(2026 - 01 - 05), 3.0, "A2605", "B2609"),
        ];
        let result = calculate_windowed_analytics(&points, &contracts, None).unwrap();
        assert_eq!(result.seasonal.axis, vec!["01-02", "01-05"]);
        assert_eq!(result.seasonal.years[0].missing_count, 1);
        assert_eq!(result.seasonal.years[0].segment_nos, vec![1]);
        assert_eq!(result.seasonal.years[0].rule_version, DEFAULT_RULE_VERSION);
    }
}
