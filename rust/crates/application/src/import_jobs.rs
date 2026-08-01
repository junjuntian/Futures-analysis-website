use domain::import::{ImportConflictPolicy, ImportErrorPreview, ImportErrorSeverity};
use serde_json::{Map, Value};
use std::collections::{BTreeMap, BTreeSet};
use uuid::Uuid;

pub const IMPORT_VALIDATION_VERSION: i32 = 1;

#[derive(Debug, Clone)]
pub struct StagingRowInput {
    pub id: Uuid,
    pub row_number: u32,
    pub normalized_values: Value,
    pub target_fields: Value,
    pub preview_warnings: Value,
}

#[derive(Debug, Clone)]
pub struct ValidatedImportRow {
    pub staging_row_id: Uuid,
    pub row_number: u32,
    pub business_key: Option<String>,
    pub record_data: Value,
    pub blocking_errors: Vec<ImportErrorPreview>,
    pub warnings: Vec<ImportErrorPreview>,
    pub duplicate: bool,
}

#[derive(Debug, Clone)]
pub struct ValidationOutcome {
    pub rows: Vec<ValidatedImportRow>,
    pub blocking_error_count: u32,
    pub warning_count: u32,
    pub duplicate_count: u32,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ValidationDefinitionError {
    DatasetNotConfirmable,
    InvalidStagingShape,
}

pub fn allowed_conflict_policies(dataset_type: &str) -> &'static [ImportConflictPolicy] {
    match dataset_type {
        "generic" => &[
            ImportConflictPolicy::Skip,
            ImportConflictPolicy::Overwrite,
            ImportConflictPolicy::KeepConflict,
            ImportConflictPolicy::Abort,
        ],
        "futures_catalog_v1"
        | "trading_calendar_v1"
        | "daily_market_prices_v1"
        | "seat_positions_v1" => &[ImportConflictPolicy::Skip],
        _ => &[],
    }
}

pub fn validate_staging_rows(
    dataset_type: &str,
    staging_rows: Vec<StagingRowInput>,
) -> Result<ValidationOutcome, ValidationDefinitionError> {
    if dataset_type != "generic" {
        return validate_automatic_rows(dataset_type, staging_rows);
    }

    let mut rows = Vec::with_capacity(staging_rows.len());
    for staging in staging_rows {
        let normalized = staging
            .normalized_values
            .as_object()
            .ok_or(ValidationDefinitionError::InvalidStagingShape)?;
        let targets = staging
            .target_fields
            .as_object()
            .ok_or(ValidationDefinitionError::InvalidStagingShape)?;
        let mut record_data = Map::new();
        for (source, target) in targets {
            let Some(target) = target.as_str().filter(|value| !value.is_empty()) else {
                continue;
            };
            record_data.insert(
                target.to_string(),
                normalized.get(source).cloned().unwrap_or(Value::Null),
            );
        }

        let mut blocking_errors = Vec::new();
        let mut warnings = Vec::new();
        let code = normalized_string(record_data.get("code"));
        let trade_date = normalized_string(record_data.get("trade_date"));
        let record_date = normalized_string(record_data.get("record_date"));
        let date = trade_date.as_deref().or(record_date.as_deref());

        if code.is_none() {
            blocking_errors.push(message(
                staging.row_number,
                Some("code"),
                ImportErrorSeverity::Error,
                "required_code_missing",
                "代码不能为空",
            ));
        }
        if date.is_none() {
            blocking_errors.push(message(
                staging.row_number,
                Some("trade_date"),
                ImportErrorSeverity::Error,
                "required_date_missing",
                "交易日期或记录日期不能为空",
            ));
        } else if !date.is_some_and(valid_iso_date) {
            blocking_errors.push(message(
                staging.row_number,
                Some("trade_date"),
                ImportErrorSeverity::Error,
                "invalid_date",
                "日期必须使用 YYYY-MM-DD",
            ));
        }
        if let (Some(trade_date), Some(record_date)) = (&trade_date, &record_date)
            && trade_date != record_date
        {
            blocking_errors.push(message(
                staging.row_number,
                None,
                ImportErrorSeverity::Error,
                "date_fields_mismatch",
                "交易日期与记录日期不一致",
            ));
        }

        for field in ["value", "price", "quantity", "amount"] {
            if let Some(value) = normalized_string(record_data.get(field))
                && !valid_controlled_decimal(&value)
            {
                blocking_errors.push(message(
                    staging.row_number,
                    Some(field),
                    ImportErrorSeverity::Error,
                    "invalid_decimal",
                    "数值格式无效",
                ));
            }
        }

        if normalized_string(record_data.get("name")).is_none() {
            warnings.push(message(
                staging.row_number,
                Some("name"),
                ImportErrorSeverity::Warning,
                "name_missing",
                "名称为空",
            ));
        }
        if let Some(note) = normalized_string(record_data.get("note"))
            && note.len() > 1024
        {
            blocking_errors.push(message(
                staging.row_number,
                Some("note"),
                ImportErrorSeverity::Error,
                "note_too_long",
                "备注长度超过限制",
            ));
        }
        if let Some(reference_type) = normalized_string(record_data.get("reference_type"))
            && !["manual", "exchange", "broker"].contains(&reference_type.as_str())
        {
            blocking_errors.push(message(
                staging.row_number,
                Some("reference_type"),
                ImportErrorSeverity::Error,
                "controlled_reference_invalid",
                "引用类型不在允许范围内",
            ));
        }
        if staging
            .preview_warnings
            .as_array()
            .is_some_and(|values| !values.is_empty())
        {
            warnings.push(message(
                staging.row_number,
                None,
                ImportErrorSeverity::Warning,
                "preview_warning",
                "预览阶段存在警告",
            ));
        }

        let business_key = match (date, code) {
            (Some(date), Some(code)) if blocking_errors.is_empty() => Some(format!(
                "{}|{}",
                date.trim(),
                code.trim().to_ascii_uppercase()
            )),
            _ => None,
        };
        rows.push(ValidatedImportRow {
            staging_row_id: staging.id,
            row_number: staging.row_number,
            business_key,
            record_data: Value::Object(record_data),
            blocking_errors,
            warnings,
            duplicate: false,
        });
    }

    let mut groups: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (index, row) in rows.iter().enumerate() {
        if let Some(key) = &row.business_key {
            groups.entry(key.clone()).or_default().push(index);
        }
    }
    for indexes in groups.values().filter(|indexes| indexes.len() > 1) {
        for index in indexes {
            rows[*index].duplicate = true;
        }
    }

    let blocking_error_count = rows
        .iter()
        .map(|row| row.blocking_errors.len() as u32)
        .sum();
    let warning_count = rows.iter().map(|row| row.warnings.len() as u32).sum();
    let duplicate_count = rows.iter().filter(|row| row.duplicate).count() as u32;
    Ok(ValidationOutcome {
        rows,
        blocking_error_count,
        warning_count,
        duplicate_count,
    })
}

fn validate_automatic_rows(
    dataset_type: &str,
    staging_rows: Vec<StagingRowInput>,
) -> Result<ValidationOutcome, ValidationDefinitionError> {
    if !matches!(
        dataset_type,
        "futures_catalog_v1"
            | "trading_calendar_v1"
            | "daily_market_prices_v1"
            | "seat_positions_v1"
    ) {
        return Err(ValidationDefinitionError::DatasetNotConfirmable);
    }
    let mut rows = Vec::with_capacity(staging_rows.len());
    for staging in staging_rows {
        let normalized = staging
            .normalized_values
            .as_object()
            .ok_or(ValidationDefinitionError::InvalidStagingShape)?;
        let targets = staging
            .target_fields
            .as_object()
            .ok_or(ValidationDefinitionError::InvalidStagingShape)?;
        let mut record_data = Map::new();
        for (source, target) in targets {
            let Some(target) = target.as_str().filter(|value| !value.is_empty()) else {
                continue;
            };
            record_data.insert(
                target.to_string(),
                normalized.get(source).cloned().unwrap_or(Value::Null),
            );
        }
        let mut blocking_errors = Vec::new();
        let mut warnings = Vec::new();
        let required: &[&str] = match dataset_type {
            "futures_catalog_v1" => &[
                "exchange_code",
                "exchange_name",
                "timezone",
                "instrument_code",
                "instrument_name",
                "currency_code",
                "contract_code",
                "source_record_ref",
            ],
            "trading_calendar_v1" => &[
                "exchange_code",
                "calendar_version",
                "effective_from",
                "trade_date",
                "is_trading_day",
                "day_session_json",
                "night_session_json",
                "source_record_ref",
            ],
            "daily_market_prices_v1" => &[
                "exchange_code",
                "contract_code",
                "trade_date",
                "session_type",
                "observed_at",
                "granularity",
                "currency_code",
                "calendar_version",
                "revision_no",
                "source_record_ref",
            ],
            "seat_positions_v1" => &[
                "exchange_code",
                "contract_code",
                "trade_date",
                "seat_name",
                "rank_type",
                "rank",
                "source_record_ref",
            ],
            _ => unreachable!(),
        };
        for field in required {
            if normalized_string(record_data.get(*field)).is_none() {
                blocking_errors.push(message(
                    staging.row_number,
                    Some(field),
                    ImportErrorSeverity::Error,
                    "required_field_missing",
                    "自动采集固定模板必填字段不能为空",
                ));
            }
        }
        for field in ["trade_date", "effective_from", "listed_at", "expires_at"] {
            if let Some(value) = normalized_string(record_data.get(field))
                && !valid_iso_date(&value)
            {
                blocking_errors.push(message(
                    staging.row_number,
                    Some(field),
                    ImportErrorSeverity::Error,
                    "invalid_date",
                    "日期必须使用 YYYY-MM-DD",
                ));
            }
        }
        for field in [
            "contract_multiplier",
            "price_tick",
            "close_price",
            "settlement_price",
        ] {
            if let Some(value) = normalized_string(record_data.get(field))
                && !valid_controlled_decimal(&value)
            {
                blocking_errors.push(message(
                    staging.row_number,
                    Some(field),
                    ImportErrorSeverity::Error,
                    "invalid_decimal",
                    "数值格式无效",
                ));
            }
        }
        let exchange = normalized_string(record_data.get("exchange_code"))
            .map(|value| value.to_ascii_uppercase());
        if exchange
            .as_deref()
            .is_some_and(|value| !["DCE", "SHFE", "CZCE", "GFEX", "CFFEX"].contains(&value))
        {
            blocking_errors.push(message(
                staging.row_number,
                Some("exchange_code"),
                ImportErrorSeverity::Error,
                "exchange_not_whitelisted",
                "交易所不在五交易所白名单内",
            ));
        }
        let business_key = match dataset_type {
            "futures_catalog_v1" => key(
                &record_data,
                &["exchange_code", "instrument_code", "contract_code"],
            ),
            "trading_calendar_v1" => key(
                &record_data,
                &["exchange_code", "calendar_version", "trade_date"],
            ),
            "daily_market_prices_v1" => {
                if normalized_string(record_data.get("close_price")).is_none()
                    && normalized_string(record_data.get("settlement_price")).is_none()
                {
                    blocking_errors.push(message(
                        staging.row_number,
                        Some("close_price"),
                        ImportErrorSeverity::Error,
                        "market_price_missing",
                        "收盘价与结算价不可同时为空",
                    ));
                }
                if normalized_string(record_data.get("session_type")).as_deref() != Some("daily")
                    || normalized_string(record_data.get("granularity")).as_deref() != Some("1d")
                    || !positive_integer(record_data.get("revision_no"))
                {
                    blocking_errors.push(message(
                        staging.row_number,
                        None,
                        ImportErrorSeverity::Error,
                        "market_key_invalid",
                        "行情时段、粒度或修订号不符合固定模板",
                    ));
                }
                key(
                    &record_data,
                    &[
                        "exchange_code",
                        "contract_code",
                        "trade_date",
                        "session_type",
                        "granularity",
                        "revision_no",
                    ],
                )
            }
            "seat_positions_v1" => {
                let rank_type = normalized_string(record_data.get("rank_type"));
                let payload_ok = match rank_type.as_deref() {
                    Some("volume") => nonnegative_integer(record_data.get("volume")),
                    Some("long") => nonnegative_integer(record_data.get("long_position")),
                    Some("short") => nonnegative_integer(record_data.get("short_position")),
                    _ => false,
                };
                if !positive_integer(record_data.get("rank")) || !payload_ok {
                    blocking_errors.push(message(
                        staging.row_number,
                        None,
                        ImportErrorSeverity::Error,
                        "seat_rank_invalid",
                        "席位排名或排名值无效",
                    ));
                }
                key(
                    &record_data,
                    &[
                        "exchange_code",
                        "contract_code",
                        "trade_date",
                        "seat_name",
                        "rank_type",
                        "rank",
                    ],
                )
            }
            _ => unreachable!(),
        };
        if dataset_type == "futures_catalog_v1" {
            for field in ["contract_multiplier", "price_tick"] {
                if normalized_string(record_data.get(field)).is_none() {
                    warnings.push(message(
                        staging.row_number,
                        Some(field),
                        ImportErrorSeverity::Warning,
                        "optional_contract_parameter_missing",
                        "交易所响应未提供可选合约参数",
                    ));
                }
            }
        }
        rows.push(ValidatedImportRow {
            staging_row_id: staging.id,
            row_number: staging.row_number,
            business_key: business_key.filter(|_| blocking_errors.is_empty()),
            record_data: Value::Object(record_data),
            blocking_errors,
            warnings,
            duplicate: false,
        });
    }
    let mut groups: BTreeMap<String, Vec<usize>> = BTreeMap::new();
    for (index, row) in rows.iter().enumerate() {
        if let Some(key) = &row.business_key {
            groups.entry(key.clone()).or_default().push(index);
        }
    }
    for indexes in groups.values().filter(|indexes| indexes.len() > 1) {
        for index in indexes {
            rows[*index].duplicate = true;
        }
    }
    Ok(ValidationOutcome {
        blocking_error_count: rows
            .iter()
            .map(|row| row.blocking_errors.len() as u32)
            .sum(),
        warning_count: rows.iter().map(|row| row.warnings.len() as u32).sum(),
        duplicate_count: rows.iter().filter(|row| row.duplicate).count() as u32,
        rows,
    })
}

fn key(record: &Map<String, Value>, fields: &[&str]) -> Option<String> {
    fields
        .iter()
        .map(|field| normalized_string(record.get(*field)))
        .collect::<Option<Vec<_>>>()
        .map(|parts| parts.join("|").to_ascii_uppercase())
}

fn positive_integer(value: Option<&Value>) -> bool {
    normalized_string(value)
        .and_then(|value| value.parse::<u64>().ok())
        .is_some_and(|value| value > 0)
}

fn nonnegative_integer(value: Option<&Value>) -> bool {
    normalized_string(value)
        .and_then(|value| value.parse::<u64>().ok())
        .is_some()
}

pub fn select_file_candidates(
    rows: &[ValidatedImportRow],
    policy: ImportConflictPolicy,
) -> Vec<&ValidatedImportRow> {
    let mut indexes_by_key: BTreeMap<&str, Vec<usize>> = BTreeMap::new();
    for (index, row) in rows.iter().enumerate() {
        if row.blocking_errors.is_empty()
            && let Some(key) = row.business_key.as_deref()
        {
            indexes_by_key.entry(key).or_default().push(index);
        }
    }
    let mut selected = BTreeSet::new();
    for indexes in indexes_by_key.values() {
        match policy {
            ImportConflictPolicy::Skip => {
                selected.insert(indexes[0]);
            }
            ImportConflictPolicy::Overwrite => {
                selected.insert(*indexes.last().expect("group is not empty"));
            }
            ImportConflictPolicy::KeepConflict if indexes.len() == 1 => {
                selected.insert(indexes[0]);
            }
            ImportConflictPolicy::KeepConflict | ImportConflictPolicy::Abort => {}
        }
    }
    selected.into_iter().map(|index| &rows[index]).collect()
}

fn normalized_string(value: Option<&Value>) -> Option<String> {
    let value = value?.as_str()?.trim();
    (!value.is_empty()).then(|| value.to_string())
}

fn valid_iso_date(value: &str) -> bool {
    time::Date::parse(
        value,
        time::macros::format_description!("[year]-[month]-[day]"),
    )
    .is_ok()
}

fn valid_controlled_decimal(value: &str) -> bool {
    let unsigned = value.strip_prefix('-').unwrap_or(value);
    let mut parts = unsigned.split('.');
    let whole = parts.next().unwrap_or_default();
    let fraction = parts.next();
    !whole.is_empty()
        && whole.bytes().all(|byte| byte.is_ascii_digit())
        && fraction.is_none_or(|digits| {
            !digits.is_empty() && digits.bytes().all(|byte| byte.is_ascii_digit())
        })
        && parts.next().is_none()
}

fn message(
    row_number: u32,
    field_name: Option<&str>,
    severity: ImportErrorSeverity,
    error_code: &str,
    text: &str,
) -> ImportErrorPreview {
    ImportErrorPreview {
        row_number: Some(row_number),
        field_name: field_name.map(str::to_string),
        severity,
        error_code: error_code.to_string(),
        raw_value: None,
        message: text.to_string(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn row(id: Uuid, number: u32, code: &str, date: &str) -> StagingRowInput {
        StagingRowInput {
            id,
            row_number: number,
            normalized_values: json!({"c": code, "d": date, "n": "sample", "v": "1.25"}),
            target_fields: json!({
                "c": "code", "d": "trade_date", "n": "name", "v": "value"
            }),
            preview_warnings: json!([]),
        }
    }

    #[test]
    fn validates_required_cross_field_and_duplicate_rules() {
        let id = Uuid::now_v7();
        let result = validate_staging_rows(
            "generic",
            vec![
                row(id, 1, "au", "2026-07-25"),
                row(Uuid::now_v7(), 2, "AU", "2026-07-25"),
            ],
        )
        .unwrap();
        assert_eq!(result.blocking_error_count, 0);
        assert_eq!(result.duplicate_count, 2);
        assert_eq!(
            result.rows[0].business_key.as_deref(),
            Some("2026-07-25|AU")
        );
    }

    #[test]
    fn strategy_selection_is_deterministic() {
        let result = validate_staging_rows(
            "generic",
            vec![
                row(Uuid::now_v7(), 1, "AU", "2026-07-25"),
                row(Uuid::now_v7(), 2, "AU", "2026-07-25"),
            ],
        )
        .unwrap();
        assert_eq!(
            select_file_candidates(&result.rows, ImportConflictPolicy::Skip)[0].row_number,
            1
        );
        assert_eq!(
            select_file_candidates(&result.rows, ImportConflictPolicy::Overwrite)[0].row_number,
            2
        );
        assert!(
            select_file_candidates(&result.rows, ImportConflictPolicy::KeepConflict).is_empty()
        );
        assert!(select_file_candidates(&result.rows, ImportConflictPolicy::Abort).is_empty());
    }

    #[test]
    fn numeric_validation_rejects_non_finite_and_exponent_syntax() {
        for invalid in ["NaN", "inf", "-inf", "1e3", "1.", ".5", "--1"] {
            let mut input = row(Uuid::now_v7(), 1, "AU", "2026-07-25");
            input.normalized_values["v"] = json!(invalid);
            let outcome = validate_staging_rows("generic", vec![input]).unwrap();
            assert_eq!(
                outcome.blocking_error_count, 1,
                "{invalid} must not pass the controlled decimal grammar"
            );
        }
    }

    #[test]
    fn controlled_reference_is_server_defined() {
        let mut input = row(Uuid::now_v7(), 1, "AU", "2026-07-25");
        input.normalized_values["r"] = json!("arbitrary");
        input.target_fields["r"] = json!("reference_type");
        let result = validate_staging_rows("generic", vec![input]).unwrap();
        assert_eq!(result.blocking_error_count, 1);
        assert_eq!(
            result.rows[0].blocking_errors[0].error_code,
            "controlled_reference_invalid"
        );
    }

    #[test]
    fn automatic_market_validation_uses_real_business_key_and_rejects_empty_prices() {
        let fields = json!({
            "exchange_code": "exchange_code",
            "contract_code": "contract_code",
            "trade_date": "trade_date",
            "session_type": "session_type",
            "observed_at": "observed_at",
            "granularity": "granularity",
            "close_price": "close_price",
            "settlement_price": "settlement_price",
            "currency_code": "currency_code",
            "calendar_version": "calendar_version",
            "revision_no": "revision_no",
            "source_record_ref": "source_record_ref"
        });
        let mut values = json!({
            "exchange_code": "SHFE",
            "contract_code": "AU2610",
            "trade_date": "2026-08-01",
            "session_type": "daily",
            "observed_at": "2026-08-01T13:30:00Z",
            "granularity": "1d",
            "close_price": "",
            "settlement_price": "",
            "currency_code": "CNY",
            "calendar_version": "akshare-v1:SHFE:2026-08-01",
            "revision_no": "1",
            "source_record_ref": "SHFE:AU2610:2026-08-01:daily"
        });
        let invalid = validate_staging_rows(
            "daily_market_prices_v1",
            vec![StagingRowInput {
                id: Uuid::now_v7(),
                row_number: 1,
                normalized_values: values.clone(),
                target_fields: fields.clone(),
                preview_warnings: json!([]),
            }],
        )
        .unwrap();
        assert_eq!(invalid.blocking_error_count, 1);
        values["settlement_price"] = json!("799.5");
        let valid = validate_staging_rows(
            "daily_market_prices_v1",
            vec![StagingRowInput {
                id: Uuid::now_v7(),
                row_number: 1,
                normalized_values: values,
                target_fields: fields,
                preview_warnings: json!([]),
            }],
        )
        .unwrap();
        assert_eq!(valid.blocking_error_count, 0);
        assert_eq!(
            valid.rows[0].business_key.as_deref(),
            Some("SHFE|AU2610|2026-08-01|DAILY|1D|1")
        );
    }
}
