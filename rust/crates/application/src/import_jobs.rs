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
        _ => &[],
    }
}

pub fn validate_staging_rows(
    dataset_type: &str,
    staging_rows: Vec<StagingRowInput>,
) -> Result<ValidationOutcome, ValidationDefinitionError> {
    if dataset_type != "generic" {
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
}
