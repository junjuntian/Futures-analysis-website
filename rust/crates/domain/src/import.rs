use serde::{Deserialize, Serialize};
use utoipa::ToSchema;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ImportBatchStatus {
    Uploaded,
    Inspected,
    Mapped,
    PreviewReady,
    Confirmed,
    Importing,
    Succeeded,
    Failed,
    Cancelled,
    RollbackCheck,
    RollingBack,
    RollbackConflict,
    RolledBack,
    RollbackFailed,
    Expired,
}

impl ImportBatchStatus {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Uploaded => "uploaded",
            Self::Inspected => "inspected",
            Self::Mapped => "mapped",
            Self::PreviewReady => "preview_ready",
            Self::Confirmed => "confirmed",
            Self::Importing => "importing",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
            Self::RollbackCheck => "rollback_check",
            Self::RollingBack => "rolling_back",
            Self::RollbackConflict => "rollback_conflict",
            Self::RolledBack => "rolled_back",
            Self::RollbackFailed => "rollback_failed",
            Self::Expired => "expired",
        }
    }

    pub const fn can_transition_to(self, target: Self) -> bool {
        matches!(
            (self, target),
            (Self::Uploaded, Self::Inspected)
                | (Self::Uploaded, Self::Expired)
                | (Self::Inspected, Self::Mapped)
                | (Self::Inspected, Self::Expired)
                | (Self::Mapped, Self::PreviewReady)
                | (Self::Mapped, Self::Expired)
                // A new inspection or mapping invalidates persisted preview rows.
                | (Self::PreviewReady, Self::Mapped)
                | (Self::PreviewReady, Self::Confirmed)
                | (Self::PreviewReady, Self::Expired)
                | (Self::Confirmed, Self::Importing)
                | (Self::Importing, Self::Succeeded)
                | (Self::Importing, Self::Failed)
                | (Self::Importing, Self::Cancelled)
                | (Self::Succeeded, Self::RollbackCheck)
                | (Self::RollbackCheck, Self::RollingBack)
                | (Self::RollbackCheck, Self::RollbackConflict)
                | (Self::RollingBack, Self::RolledBack)
                | (Self::RollingBack, Self::RollbackFailed)
        )
    }

    pub fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "uploaded" => Self::Uploaded,
            "inspected" => Self::Inspected,
            "mapped" => Self::Mapped,
            "preview_ready" => Self::PreviewReady,
            "confirmed" => Self::Confirmed,
            "importing" => Self::Importing,
            "succeeded" => Self::Succeeded,
            "failed" => Self::Failed,
            "cancelled" => Self::Cancelled,
            "rollback_check" => Self::RollbackCheck,
            "rolling_back" => Self::RollingBack,
            "rollback_conflict" => Self::RollbackConflict,
            "rolled_back" => Self::RolledBack,
            "rollback_failed" => Self::RollbackFailed,
            "expired" => Self::Expired,
            _ => return None,
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct InvalidStatusTransition {
    pub from: ImportBatchStatus,
    pub to: ImportBatchStatus,
}

pub fn ensure_status_transition(
    from: ImportBatchStatus,
    to: ImportBatchStatus,
) -> Result<(), InvalidStatusTransition> {
    if from.can_transition_to(to) {
        Ok(())
    } else {
        Err(InvalidStatusTransition { from, to })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ImportErrorSeverity {
    Error,
    Warning,
}

impl ImportErrorSeverity {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Error => "error",
            Self::Warning => "warning",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "error" => Self::Error,
            "warning" => Self::Warning,
            _ => return None,
        })
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportInspectRequest {
    pub encoding: Option<String>,
    pub delimiter: Option<String>,
    pub selected_sheet: Option<String>,
    pub header_row: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportPreviewRequest {
    pub encoding: Option<String>,
    pub delimiter: Option<String>,
    pub selected_sheet: Option<String>,
    pub header_row: Option<u32>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportDetection {
    pub value: Option<String>,
    pub confidence: f32,
    pub candidates: Vec<String>,
    pub overridden: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportSheetInfo {
    pub name: String,
    pub row_count: u32,
    pub column_count: u32,
    pub selected: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportColumnPreview {
    pub index: u32,
    pub name: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportPreviewCell {
    pub column: String,
    pub raw_value: String,
    pub normalized_value: String,
    pub target_field: Option<String>,
    pub errors: Vec<String>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportPreviewRow {
    pub row_number: u32,
    pub cells: Vec<ImportPreviewCell>,
    pub errors: Vec<String>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportErrorPreview {
    pub row_number: Option<u32>,
    pub field_name: Option<String>,
    pub severity: ImportErrorSeverity,
    pub error_code: String,
    pub raw_value: Option<String>,
    pub message: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportInspectResponse {
    pub import_id: uuid::Uuid,
    pub status: ImportBatchStatus,
    pub detected_format: String,
    pub encoding: ImportDetection,
    pub delimiter: ImportDetection,
    pub sheets: Vec<ImportSheetInfo>,
    pub selected_sheet: Option<String>,
    pub header_row: u32,
    pub columns: Vec<ImportColumnPreview>,
    pub preview_rows: Vec<ImportPreviewRow>,
    pub total_rows: u32,
    pub preview_row_count: u32,
    /// Whether a previous persisted preview was discarded by this request.
    pub preview_invalidated: bool,
    pub errors: Vec<ImportErrorPreview>,
    pub warnings: Vec<ImportErrorPreview>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
pub struct ImportMappingField {
    pub source_column: String,
    pub target_field: String,
    pub transform: Option<String>,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct ImportDatasetDefinition {
    pub dataset_type: String,
    pub fields: Vec<ImportDatasetFieldDefinition>,
}

#[derive(Debug, Clone, Serialize, ToSchema)]
pub struct ImportDatasetFieldDefinition {
    pub code: String,
    pub label: String,
    pub transforms: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ImportMappingDefinitionError {
    UnknownDatasetType,
    UnknownTargetField,
    UnsupportedTransform,
}

struct DatasetFieldRule {
    code: &'static str,
    label: &'static str,
    transforms: &'static [&'static str],
}

const GENERIC_DATASET_FIELDS: &[DatasetFieldRule] = &[
    DatasetFieldRule {
        code: "trade_date",
        label: "交易日期",
        transforms: &["trim", "date_ymd", "empty_to_null"],
    },
    DatasetFieldRule {
        code: "record_date",
        label: "记录日期",
        transforms: &["trim", "date_ymd", "empty_to_null"],
    },
    DatasetFieldRule {
        code: "code",
        label: "代码",
        transforms: &["trim", "empty_to_null"],
    },
    DatasetFieldRule {
        code: "name",
        label: "名称",
        transforms: &["trim", "empty_to_null"],
    },
    DatasetFieldRule {
        code: "value",
        label: "数值",
        transforms: &["trim", "decimal", "empty_to_null", "unit_placeholder"],
    },
    DatasetFieldRule {
        code: "price",
        label: "价格",
        transforms: &["trim", "decimal", "empty_to_null", "unit_placeholder"],
    },
    DatasetFieldRule {
        code: "quantity",
        label: "数量",
        transforms: &["trim", "integer", "decimal", "empty_to_null"],
    },
    DatasetFieldRule {
        code: "amount",
        label: "金额",
        transforms: &["trim", "decimal", "empty_to_null", "unit_placeholder"],
    },
    DatasetFieldRule {
        code: "note",
        label: "备注",
        transforms: &["trim", "empty_to_null", "enum_map"],
    },
];

pub fn import_dataset_definitions() -> Vec<ImportDatasetDefinition> {
    vec![ImportDatasetDefinition {
        dataset_type: "generic".to_string(),
        fields: GENERIC_DATASET_FIELDS
            .iter()
            .map(|field| ImportDatasetFieldDefinition {
                code: field.code.to_string(),
                label: field.label.to_string(),
                transforms: field
                    .transforms
                    .iter()
                    .map(|value| (*value).to_string())
                    .collect(),
            })
            .collect(),
    }]
}

pub fn validate_mapping_fields(
    dataset_type: &str,
    fields: &[ImportMappingField],
) -> Result<(), ImportMappingDefinitionError> {
    let dataset_fields = match dataset_type {
        "generic" => GENERIC_DATASET_FIELDS,
        _ => return Err(ImportMappingDefinitionError::UnknownDatasetType),
    };

    for mapping in fields {
        let target = dataset_fields
            .iter()
            .find(|field| field.code == mapping.target_field)
            .ok_or(ImportMappingDefinitionError::UnknownTargetField)?;
        if let Some(transform) = &mapping.transform
            && !target.transforms.contains(&transform.as_str())
        {
            return Err(ImportMappingDefinitionError::UnsupportedTransform);
        }
    }
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportMappingRequest {
    pub dataset_type: String,
    pub template_version_id: Option<uuid::Uuid>,
    pub fields: Vec<ImportMappingField>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportMappingResponse {
    pub import_id: uuid::Uuid,
    pub status: ImportBatchStatus,
    pub dataset_type: String,
    pub template_version_id: Option<uuid::Uuid>,
    pub fields: Vec<ImportMappingField>,
    pub preview_invalidated: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportTemplateCreateRequest {
    pub dataset_type: String,
    pub name: String,
    pub description: Option<String>,
    pub fields: Vec<ImportMappingField>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportTemplateSummary {
    pub id: uuid::Uuid,
    pub dataset_type: String,
    pub name: String,
    pub description: Option<String>,
    pub latest_version_id: uuid::Uuid,
    pub latest_version_number: i32,
    pub fields: Vec<ImportMappingField>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportTemplateVersionResponse {
    pub id: uuid::Uuid,
    pub template_id: uuid::Uuid,
    pub version_number: i32,
    pub dataset_type: String,
    pub fields: Vec<ImportMappingField>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportErrorsResponse {
    pub import_id: uuid::Uuid,
    pub items: Vec<ImportErrorPreview>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn all_documented_transitions_are_allowed() {
        let allowed = [
            (ImportBatchStatus::Uploaded, ImportBatchStatus::Inspected),
            (ImportBatchStatus::Uploaded, ImportBatchStatus::Expired),
            (ImportBatchStatus::Inspected, ImportBatchStatus::Mapped),
            (ImportBatchStatus::Inspected, ImportBatchStatus::Expired),
            (ImportBatchStatus::Mapped, ImportBatchStatus::PreviewReady),
            (ImportBatchStatus::Mapped, ImportBatchStatus::Expired),
            (ImportBatchStatus::PreviewReady, ImportBatchStatus::Mapped),
            (
                ImportBatchStatus::PreviewReady,
                ImportBatchStatus::Confirmed,
            ),
            (ImportBatchStatus::PreviewReady, ImportBatchStatus::Expired),
            (ImportBatchStatus::Confirmed, ImportBatchStatus::Importing),
            (ImportBatchStatus::Importing, ImportBatchStatus::Succeeded),
            (ImportBatchStatus::Importing, ImportBatchStatus::Failed),
            (ImportBatchStatus::Importing, ImportBatchStatus::Cancelled),
            (
                ImportBatchStatus::Succeeded,
                ImportBatchStatus::RollbackCheck,
            ),
            (
                ImportBatchStatus::RollbackCheck,
                ImportBatchStatus::RollingBack,
            ),
            (
                ImportBatchStatus::RollbackCheck,
                ImportBatchStatus::RollbackConflict,
            ),
            (
                ImportBatchStatus::RollingBack,
                ImportBatchStatus::RolledBack,
            ),
            (
                ImportBatchStatus::RollingBack,
                ImportBatchStatus::RollbackFailed,
            ),
        ];

        for (from, to) in allowed {
            assert!(
                ensure_status_transition(from, to).is_ok(),
                "{from:?} -> {to:?}"
            );
        }
    }

    #[test]
    fn representative_invalid_transitions_are_rejected() {
        let invalid = [
            (ImportBatchStatus::Uploaded, ImportBatchStatus::Succeeded),
            (ImportBatchStatus::Inspected, ImportBatchStatus::Importing),
            (ImportBatchStatus::Confirmed, ImportBatchStatus::Mapped),
            (ImportBatchStatus::Failed, ImportBatchStatus::Importing),
            (ImportBatchStatus::RolledBack, ImportBatchStatus::Uploaded),
        ];

        for (from, to) in invalid {
            assert_eq!(
                ensure_status_transition(from, to),
                Err(InvalidStatusTransition { from, to })
            );
        }
    }

    #[test]
    fn every_status_rejects_self_transition() {
        let statuses = [
            ImportBatchStatus::Uploaded,
            ImportBatchStatus::Inspected,
            ImportBatchStatus::Mapped,
            ImportBatchStatus::PreviewReady,
            ImportBatchStatus::Confirmed,
            ImportBatchStatus::Importing,
            ImportBatchStatus::Succeeded,
            ImportBatchStatus::Failed,
            ImportBatchStatus::Cancelled,
            ImportBatchStatus::RollbackCheck,
            ImportBatchStatus::RollingBack,
            ImportBatchStatus::RollbackConflict,
            ImportBatchStatus::RolledBack,
            ImportBatchStatus::RollbackFailed,
            ImportBatchStatus::Expired,
        ];

        for status in statuses {
            assert!(ensure_status_transition(status, status).is_err());
        }
    }

    #[test]
    fn mapping_definition_rejects_unknown_dataset_field_and_transform() {
        let valid = [ImportMappingField {
            source_column: "日期".into(),
            target_field: "record_date".into(),
            transform: Some("date_ymd".into()),
        }];
        assert!(validate_mapping_fields("generic", &valid).is_ok());
        assert_eq!(
            validate_mapping_fields("unknown", &valid),
            Err(ImportMappingDefinitionError::UnknownDatasetType)
        );
        assert_eq!(
            validate_mapping_fields(
                "generic",
                &[ImportMappingField {
                    target_field: "arbitrary_sql".into(),
                    ..valid[0].clone()
                }]
            ),
            Err(ImportMappingDefinitionError::UnknownTargetField)
        );
        assert_eq!(
            validate_mapping_fields(
                "generic",
                &[ImportMappingField {
                    transform: Some("run_sql".into()),
                    ..valid[0].clone()
                }]
            ),
            Err(ImportMappingDefinitionError::UnsupportedTransform)
        );
    }

    #[test]
    fn generic_definition_keeps_existing_phase_3b_trade_date_and_price_contract() {
        let existing_e2e_mapping = [
            ImportMappingField {
                source_column: "date".into(),
                target_field: "trade_date".into(),
                transform: None,
            },
            ImportMappingField {
                source_column: "price".into(),
                target_field: "price".into(),
                transform: None,
            },
        ];
        assert!(validate_mapping_fields("generic", &existing_e2e_mapping).is_ok());
        let fields = import_dataset_definitions().remove(0).fields;
        assert!(fields.iter().any(|field| field.code == "trade_date"));
        assert!(fields.iter().any(|field| field.code == "price"));
    }
}
