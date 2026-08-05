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
                | (Self::RollingBack, Self::RollbackConflict)
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

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ImportConflictPolicy {
    Skip,
    Overwrite,
    KeepConflict,
    Abort,
}

impl ImportConflictPolicy {
    pub const ALL: [Self; 4] = [Self::Skip, Self::Overwrite, Self::KeepConflict, Self::Abort];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Skip => "skip",
            Self::Overwrite => "overwrite",
            Self::KeepConflict => "keep_conflict",
            Self::Abort => "abort",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "skip" => Self::Skip,
            "overwrite" => Self::Overwrite,
            "keep_conflict" => Self::KeepConflict,
            "abort" => Self::Abort,
            _ => return None,
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ImportJobStatus {
    Queued,
    Running,
    Succeeded,
    Failed,
    DeadLetter,
}

impl ImportJobStatus {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Running => "running",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::DeadLetter => "dead_letter",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "queued" => Self::Queued,
            "running" => Self::Running,
            "succeeded" => Self::Succeeded,
            "failed" => Self::Failed,
            "dead_letter" => Self::DeadLetter,
            _ => return None,
        })
    }

    pub const fn is_terminal(self) -> bool {
        matches!(self, Self::Succeeded | Self::Failed | Self::DeadLetter)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ImportJobEventType {
    Queued,
    Running,
    Progress,
    Succeeded,
    Failed,
    DeadLetter,
    RollbackQueued,
    RollbackRunning,
    RollbackConflict,
    RolledBack,
    RollbackFailed,
}

impl ImportJobEventType {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Queued => "queued",
            Self::Running => "running",
            Self::Progress => "progress",
            Self::Succeeded => "succeeded",
            Self::Failed => "failed",
            Self::DeadLetter => "dead_letter",
            Self::RollbackQueued => "rollback_queued",
            Self::RollbackRunning => "rollback_running",
            Self::RollbackConflict => "rollback_conflict",
            Self::RolledBack => "rolled_back",
            Self::RollbackFailed => "rollback_failed",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Some(match value {
            "queued" => Self::Queued,
            "running" => Self::Running,
            "progress" => Self::Progress,
            "succeeded" => Self::Succeeded,
            "failed" => Self::Failed,
            "dead_letter" => Self::DeadLetter,
            "rollback_queued" => Self::RollbackQueued,
            "rollback_running" => Self::RollbackRunning,
            "rollback_conflict" => Self::RollbackConflict,
            "rolled_back" => Self::RolledBack,
            "rollback_failed" => Self::RollbackFailed,
            _ => return None,
        })
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ImportJobType {
    ImportConfirm,
    ImportRollback,
    ObjectConsistencyScan,
    ObjectQuarantine,
}

impl ImportJobType {
    pub const ALL: [Self; 4] = [
        Self::ImportConfirm,
        Self::ImportRollback,
        Self::ObjectConsistencyScan,
        Self::ObjectQuarantine,
    ];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::ImportConfirm => "import_confirm",
            Self::ImportRollback => "import_rollback",
            Self::ObjectConsistencyScan => "object_consistency_scan",
            Self::ObjectQuarantine => "object_quarantine",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|item| item.as_str() == value)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum RollbackCapability {
    CompensationOnly,
    Direct,
}

impl RollbackCapability {
    pub const ALL: [Self; 2] = [Self::CompensationOnly, Self::Direct];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::CompensationOnly => "compensation_only",
            Self::Direct => "direct",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|item| item.as_str() == value)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ImportChangeOperation {
    Insert,
    Update,
    SoftDelete,
}

impl ImportChangeOperation {
    pub const ALL: [Self; 3] = [Self::Insert, Self::Update, Self::SoftDelete];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Insert => "insert",
            Self::Update => "update",
            Self::SoftDelete => "soft_delete",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|item| item.as_str() == value)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ImportRollbackRequestStatus {
    Prechecked,
    PrecheckConflict,
    Queued,
    Running,
    Succeeded,
    WorkerConflict,
    Failed,
}

impl ImportRollbackRequestStatus {
    pub const ALL: [Self; 7] = [
        Self::Prechecked,
        Self::PrecheckConflict,
        Self::Queued,
        Self::Running,
        Self::Succeeded,
        Self::WorkerConflict,
        Self::Failed,
    ];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Prechecked => "prechecked",
            Self::PrecheckConflict => "precheck_conflict",
            Self::Queued => "queued",
            Self::Running => "running",
            Self::Succeeded => "succeeded",
            Self::WorkerConflict => "worker_conflict",
            Self::Failed => "failed",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|item| item.as_str() == value)
    }

    pub const fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::PrecheckConflict | Self::Succeeded | Self::WorkerConflict | Self::Failed
        )
    }

    pub const fn requires_job(self) -> bool {
        matches!(
            self,
            Self::Queued | Self::Running | Self::Succeeded | Self::WorkerConflict | Self::Failed
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ImportRollbackConflictType {
    RollbackNotAvailable,
    TargetMissing,
    TargetVersionChanged,
    TargetDataChanged,
    LaterImport,
    LaterModification,
    DownstreamDependency,
    ChangeLogIncomplete,
    SourceChainBroken,
    IllegalChange,
}

impl ImportRollbackConflictType {
    pub const ALL: [Self; 10] = [
        Self::RollbackNotAvailable,
        Self::TargetMissing,
        Self::TargetVersionChanged,
        Self::TargetDataChanged,
        Self::LaterImport,
        Self::LaterModification,
        Self::DownstreamDependency,
        Self::ChangeLogIncomplete,
        Self::SourceChainBroken,
        Self::IllegalChange,
    ];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::RollbackNotAvailable => "rollback_not_available",
            Self::TargetMissing => "target_missing",
            Self::TargetVersionChanged => "target_version_changed",
            Self::TargetDataChanged => "target_data_changed",
            Self::LaterImport => "later_import",
            Self::LaterModification => "later_modification",
            Self::DownstreamDependency => "downstream_dependency",
            Self::ChangeLogIncomplete => "change_log_incomplete",
            Self::SourceChainBroken => "source_chain_broken",
            Self::IllegalChange => "illegal_change",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|item| item.as_str() == value)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ObjectConsistencyRunStatus {
    Running,
    Completed,
    Failed,
}

impl ObjectConsistencyRunStatus {
    pub const ALL: [Self; 3] = [Self::Running, Self::Completed, Self::Failed];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Running => "running",
            Self::Completed => "completed",
            Self::Failed => "failed",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|item| item.as_str() == value)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ObjectConsistencyFindingType {
    DatabaseObjectMissing,
    OrphanObject,
    SizeMismatch,
    Sha256Mismatch,
    BackendMismatch,
    StateMismatch,
    WorkspacePathMismatch,
    StaleTemporaryObject,
    StalePendingObject,
    CommitOutcomeUnknown,
}

impl ObjectConsistencyFindingType {
    pub const ALL: [Self; 10] = [
        Self::DatabaseObjectMissing,
        Self::OrphanObject,
        Self::SizeMismatch,
        Self::Sha256Mismatch,
        Self::BackendMismatch,
        Self::StateMismatch,
        Self::WorkspacePathMismatch,
        Self::StaleTemporaryObject,
        Self::StalePendingObject,
        Self::CommitOutcomeUnknown,
    ];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::DatabaseObjectMissing => "database_object_missing",
            Self::OrphanObject => "orphan_object",
            Self::SizeMismatch => "size_mismatch",
            Self::Sha256Mismatch => "sha256_mismatch",
            Self::BackendMismatch => "backend_mismatch",
            Self::StateMismatch => "state_mismatch",
            Self::WorkspacePathMismatch => "workspace_path_mismatch",
            Self::StaleTemporaryObject => "stale_temporary_object",
            Self::StalePendingObject => "stale_pending_object",
            Self::CommitOutcomeUnknown => "commit_outcome_unknown",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|item| item.as_str() == value)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ObjectDispositionStatus {
    Detected,
    Quarantined,
    Acknowledged,
}

impl ObjectDispositionStatus {
    pub const ALL: [Self; 3] = [Self::Detected, Self::Quarantined, Self::Acknowledged];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Detected => "detected",
            Self::Quarantined => "quarantined",
            Self::Acknowledged => "acknowledged",
        }
    }

    pub fn parse(value: &str) -> Option<Self> {
        Self::ALL.into_iter().find(|item| item.as_str() == value)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum ImportWorkflowErrorCode {
    DatasetNotConfirmable,
    ValidationRequired,
    ValidationStale,
    BlockingErrorsPresent,
    ConflictPolicyNotAllowed,
    IdempotencyKeyReused,
    ConfirmationConflict,
    EventIdInvalid,
    EventNotVisible,
    RollbackNotAllowed,
    RollbackNotAvailable,
    RollbackPreconditionStale,
    RollbackConflict,
    RollbackAlreadyCompleted,
    RollbackInProgress,
    RollbackIdempotencyKeyReused,
    CompensationNotAllowed,
    CompensationCycle,
    CompensationLineageTooDeep,
    ObjectConsistencyError,
}

impl ImportWorkflowErrorCode {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::DatasetNotConfirmable => "dataset_not_confirmable",
            Self::ValidationRequired => "validation_required",
            Self::ValidationStale => "validation_stale",
            Self::BlockingErrorsPresent => "blocking_errors_present",
            Self::ConflictPolicyNotAllowed => "conflict_policy_not_allowed",
            Self::IdempotencyKeyReused => "idempotency_key_reused",
            Self::ConfirmationConflict => "confirmation_conflict",
            Self::EventIdInvalid => "event_id_invalid",
            Self::EventNotVisible => "event_not_visible",
            Self::RollbackNotAllowed => "rollback_not_allowed",
            Self::RollbackNotAvailable => "rollback_not_available",
            Self::RollbackPreconditionStale => "rollback_precondition_stale",
            Self::RollbackConflict => "rollback_conflict",
            Self::RollbackAlreadyCompleted => "rollback_already_completed",
            Self::RollbackInProgress => "rollback_in_progress",
            Self::RollbackIdempotencyKeyReused => "rollback_idempotency_key_reused",
            Self::CompensationNotAllowed => "compensation_not_allowed",
            Self::CompensationCycle => "compensation_cycle",
            Self::CompensationLineageTooDeep => "compensation_lineage_too_deep",
            Self::ObjectConsistencyError => "object_consistency_error",
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportValidationSummary {
    pub validation_version: u32,
    pub staging_version: u64,
    pub blocking_error_count: u32,
    pub warning_count: u32,
    pub duplicate_count: u32,
    pub conflict_count: u32,
    pub allowed_conflict_policies: Vec<ImportConflictPolicy>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportValidateResponse {
    pub import_id: uuid::Uuid,
    pub status: ImportBatchStatus,
    pub validation: ImportValidationSummary,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportConfirmRequest {
    pub conflict_policy: ImportConflictPolicy,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportConfirmResponse {
    pub import_id: uuid::Uuid,
    pub job_id: uuid::Uuid,
    pub batch_status: ImportBatchStatus,
    pub job_status: ImportJobStatus,
    pub conflict_policy: ImportConflictPolicy,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportRollbackRequest {
    pub precheck_request_id: uuid::Uuid,
    pub precheck_fingerprint: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportRollbackConflict {
    pub conflict_seq: u64,
    pub conflict_type: ImportRollbackConflictType,
    pub target_kind: Option<String>,
    pub target_id: Option<uuid::Uuid>,
    pub expected_row_version: Option<u64>,
    pub current_row_version: Option<u64>,
    pub dependency_kind: Option<String>,
    pub detail_code: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportRollbackCheckResponse {
    pub import_id: uuid::Uuid,
    pub precheck_request_id: uuid::Uuid,
    pub precheck_fingerprint: String,
    pub rollback_capability: RollbackCapability,
    pub change_log_version: Option<u32>,
    pub can_rollback: bool,
    pub compensation_recommended: bool,
    pub affected_count: u32,
    pub conflict_count: u32,
    pub conflicts: Vec<ImportRollbackConflict>,
    pub next_cursor: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportRollbackConflictsResponse {
    pub import_id: uuid::Uuid,
    pub precheck_request_id: uuid::Uuid,
    pub items: Vec<ImportRollbackConflict>,
    pub next_cursor: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportRollbackResponse {
    pub import_id: uuid::Uuid,
    pub precheck_request_id: uuid::Uuid,
    pub job_id: uuid::Uuid,
    pub status: ImportRollbackRequestStatus,
    pub replayed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportCompensationFile {
    pub file_id: uuid::Uuid,
    pub original_filename: String,
    pub detected_format: String,
    pub sha256: String,
    pub size_bytes: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportCompensationResponse {
    pub original_import_id: uuid::Uuid,
    pub compensation_import_id: uuid::Uuid,
    pub status: ImportBatchStatus,
    pub reason: String,
    pub requested_by: uuid::Uuid,
    pub file: ImportCompensationFile,
    pub replayed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportLineageFile {
    pub file_id: uuid::Uuid,
    pub object_id: uuid::Uuid,
    pub original_filename: String,
    pub detected_format: String,
    pub sha256: String,
    pub size_bytes: u64,
    pub object_state: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportLineageJob {
    pub job_id: uuid::Uuid,
    pub job_type: String,
    pub status: String,
    pub attempt_count: u32,
    pub error_code: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportLineageRollback {
    pub rollback_request_id: uuid::Uuid,
    pub status: ImportRollbackRequestStatus,
    pub conflict_count: u32,
    pub requested_by: uuid::Uuid,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportLineageNode {
    pub import_id: uuid::Uuid,
    pub status: ImportBatchStatus,
    pub compensates_import_id: Option<uuid::Uuid>,
    pub compensation_reason: Option<String>,
    pub created_by: uuid::Uuid,
    pub confirmed_by: Option<uuid::Uuid>,
    pub rollback_capability: RollbackCapability,
    pub mapping_id: Option<uuid::Uuid>,
    pub created_at: String,
    pub confirmed_at: Option<String>,
    pub rolled_back_at: Option<String>,
    pub file: ImportLineageFile,
    pub jobs: Vec<ImportLineageJob>,
    pub rollbacks: Vec<ImportLineageRollback>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportLineageAudit {
    pub audit_id: uuid::Uuid,
    pub import_id: uuid::Uuid,
    pub event_type: String,
    pub outcome: String,
    pub actor_user_id: Option<uuid::Uuid>,
    pub created_at: String,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportLineageResponse {
    pub requested_import_id: uuid::Uuid,
    pub root_import_id: uuid::Uuid,
    pub nodes: Vec<ImportLineageNode>,
    pub audits: Vec<ImportLineageAudit>,
}

#[derive(Debug, Clone, Default, Serialize, Deserialize, ToSchema)]
pub struct ImportProgress {
    pub processed_count: u32,
    pub total_count: u32,
    pub imported_count: u32,
    pub skipped_count: u32,
    pub overwritten_count: u32,
    pub conflict_count: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportJobSummary {
    pub job_id: uuid::Uuid,
    pub status: ImportJobStatus,
    pub attempt_count: u32,
    pub max_attempts: u32,
    pub progress: ImportProgress,
    pub error_code: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ImportJobEvent {
    pub event_seq: u64,
    pub event_type: ImportJobEventType,
    pub payload: ImportProgress,
    pub error_code: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct ImportErrorCursor {
    pub workspace_id: uuid::Uuid,
    pub import_id: uuid::Uuid,
    pub row_number: Option<u32>,
    pub created_at: String,
    pub id: uuid::Uuid,
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
pub struct ImportPreviewCell {
    pub column: String,
    pub raw_value: String,
    pub normalized_value: Option<String>,
    pub target_field: Option<String>,
    pub errors: Vec<String>,
    pub warnings: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, ToSchema)]
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
        transforms: &["trim", "decimal", "empty_to_null"],
    },
    DatasetFieldRule {
        code: "price",
        label: "价格",
        transforms: &["trim", "decimal", "empty_to_null"],
    },
    DatasetFieldRule {
        code: "quantity",
        label: "数量",
        transforms: &["trim", "integer", "decimal", "empty_to_null"],
    },
    DatasetFieldRule {
        code: "amount",
        label: "金额",
        transforms: &["trim", "decimal", "empty_to_null"],
    },
    DatasetFieldRule {
        code: "note",
        label: "备注",
        transforms: &["trim", "empty_to_null"],
    },
    DatasetFieldRule {
        code: "reference_type",
        label: "引用类型",
        transforms: &["trim", "empty_to_null"],
    },
];

macro_rules! dataset_fields {
    ($($code:literal => $label:literal),+ $(,)?) => {
        &[$(DatasetFieldRule {
            code: $code,
            label: $label,
            transforms: &["trim", "empty_to_null"],
        }),+]
    };
}

const CATALOG_DATASET_FIELDS: &[DatasetFieldRule] = dataset_fields! {
    "exchange_code" => "交易所代码",
    "exchange_name" => "交易所名称",
    "timezone" => "时区",
    "instrument_code" => "品种代码",
    "instrument_name" => "品种名称",
    "currency_code" => "币种",
    "contract_multiplier" => "合约乘数",
    "price_tick" => "最小变动价位",
    "contract_code" => "合约代码",
    "delivery_month" => "交割月份",
    "listed_at" => "上市日",
    "expires_at" => "到期日",
    "source_record_ref" => "来源定位"
};

const CALENDAR_DATASET_FIELDS: &[DatasetFieldRule] = dataset_fields! {
    "exchange_code" => "交易所代码",
    "calendar_version" => "日历版本",
    "effective_from" => "生效日期",
    "trade_date" => "交易日",
    "is_trading_day" => "是否交易日",
    "day_session_json" => "日盘时段",
    "night_session_json" => "夜盘时段",
    "source_record_ref" => "来源定位"
};

const MARKET_DATASET_FIELDS: &[DatasetFieldRule] = dataset_fields! {
    "exchange_code" => "交易所代码",
    "contract_code" => "合约代码",
    "trade_date" => "交易日",
    "session_type" => "时段类型",
    "observed_at" => "观测时间",
    "granularity" => "粒度",
    "close_price" => "收盘价",
    "settlement_price" => "结算价",
    "currency_code" => "币种",
    "calendar_version" => "日历版本",
    "revision_no" => "修订号",
    "source_record_ref" => "来源定位"
};

const SEAT_DATASET_FIELDS: &[DatasetFieldRule] = dataset_fields! {
    "exchange_code" => "交易所代码",
    "contract_code" => "合约代码",
    "trade_date" => "交易日",
    "seat_name" => "席位名称",
    "rank_type" => "排名类型",
    "rank" => "名次",
    "volume" => "成交量",
    "long_position" => "持买量",
    "short_position" => "持卖量",
    "source_record_ref" => "来源定位"
};

pub fn import_dataset_definitions() -> Vec<ImportDatasetDefinition> {
    [
        ("generic", GENERIC_DATASET_FIELDS),
        ("futures_catalog_v1", CATALOG_DATASET_FIELDS),
        ("trading_calendar_v1", CALENDAR_DATASET_FIELDS),
        ("daily_market_prices_v1", MARKET_DATASET_FIELDS),
        ("seat_positions_v1", SEAT_DATASET_FIELDS),
    ]
    .into_iter()
    .map(|(dataset_type, fields)| ImportDatasetDefinition {
        dataset_type: dataset_type.to_string(),
        fields: fields
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
    })
    .collect()
}

pub fn validate_mapping_fields(
    dataset_type: &str,
    fields: &[ImportMappingField],
) -> Result<(), ImportMappingDefinitionError> {
    let dataset_fields = match dataset_type {
        "generic" => GENERIC_DATASET_FIELDS,
        "futures_catalog_v1" => CATALOG_DATASET_FIELDS,
        "trading_calendar_v1" => CALENDAR_DATASET_FIELDS,
        "daily_market_prices_v1" => MARKET_DATASET_FIELDS,
        "seat_positions_v1" => SEAT_DATASET_FIELDS,
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
    pub next_cursor: Option<String>,
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
                ImportBatchStatus::RollbackConflict,
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
    fn parameterized_transforms_are_not_advertised_or_accepted() {
        let definition = import_dataset_definitions().pop().unwrap();
        assert!(definition.fields.iter().all(|field| {
            !field
                .transforms
                .iter()
                .any(|transform| matches!(transform.as_str(), "enum_map" | "unit_placeholder"))
        }));
        for transform in ["enum_map", "unit_placeholder"] {
            assert_eq!(
                validate_mapping_fields(
                    "generic",
                    &[ImportMappingField {
                        source_column: "source".into(),
                        target_field: "note".into(),
                        transform: Some(transform.into()),
                    }],
                ),
                Err(ImportMappingDefinitionError::UnsupportedTransform)
            );
        }
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

    #[test]
    fn phase_3c_contract_enums_round_trip_database_values() {
        for policy in ImportConflictPolicy::ALL {
            assert_eq!(ImportConflictPolicy::parse(policy.as_str()), Some(policy));
        }

        for status in [
            ImportJobStatus::Queued,
            ImportJobStatus::Running,
            ImportJobStatus::Succeeded,
            ImportJobStatus::Failed,
            ImportJobStatus::DeadLetter,
        ] {
            assert_eq!(ImportJobStatus::parse(status.as_str()), Some(status));
        }

        for event_type in [
            ImportJobEventType::Queued,
            ImportJobEventType::Running,
            ImportJobEventType::Progress,
            ImportJobEventType::Succeeded,
            ImportJobEventType::Failed,
            ImportJobEventType::DeadLetter,
            ImportJobEventType::RollbackQueued,
            ImportJobEventType::RollbackRunning,
            ImportJobEventType::RollbackConflict,
            ImportJobEventType::RolledBack,
            ImportJobEventType::RollbackFailed,
        ] {
            assert_eq!(
                ImportJobEventType::parse(event_type.as_str()),
                Some(event_type)
            );
        }
    }

    #[test]
    fn phase_3d_contract_enums_round_trip_database_values() {
        for job_type in ImportJobType::ALL {
            assert_eq!(ImportJobType::parse(job_type.as_str()), Some(job_type));
        }
        for capability in RollbackCapability::ALL {
            assert_eq!(
                RollbackCapability::parse(capability.as_str()),
                Some(capability)
            );
        }
        for operation in ImportChangeOperation::ALL {
            assert_eq!(
                ImportChangeOperation::parse(operation.as_str()),
                Some(operation)
            );
        }
        for status in ImportRollbackRequestStatus::ALL {
            assert_eq!(
                ImportRollbackRequestStatus::parse(status.as_str()),
                Some(status)
            );
        }
        for conflict in ImportRollbackConflictType::ALL {
            assert_eq!(
                ImportRollbackConflictType::parse(conflict.as_str()),
                Some(conflict)
            );
        }
        for status in ObjectConsistencyRunStatus::ALL {
            assert_eq!(
                ObjectConsistencyRunStatus::parse(status.as_str()),
                Some(status)
            );
        }
        for finding in ObjectConsistencyFindingType::ALL {
            assert_eq!(
                ObjectConsistencyFindingType::parse(finding.as_str()),
                Some(finding)
            );
        }
        for disposition in ObjectDispositionStatus::ALL {
            assert_eq!(
                ObjectDispositionStatus::parse(disposition.as_str()),
                Some(disposition)
            );
        }
    }

    #[test]
    fn rollback_request_terminal_states_are_explicit() {
        assert!(!ImportRollbackRequestStatus::Prechecked.is_terminal());
        assert!(ImportRollbackRequestStatus::PrecheckConflict.is_terminal());
        assert!(!ImportRollbackRequestStatus::Queued.is_terminal());
        assert!(!ImportRollbackRequestStatus::Running.is_terminal());
        assert!(ImportRollbackRequestStatus::Succeeded.is_terminal());
        assert!(ImportRollbackRequestStatus::WorkerConflict.is_terminal());
        assert!(ImportRollbackRequestStatus::Failed.is_terminal());
    }

    #[test]
    fn soft_delete_is_a_first_class_change_operation() {
        assert_eq!(
            ImportChangeOperation::parse("soft_delete"),
            Some(ImportChangeOperation::SoftDelete)
        );
        assert_eq!(ImportChangeOperation::SoftDelete.as_str(), "soft_delete");
        assert_eq!(ImportChangeOperation::ALL.len(), 3);
    }

    #[test]
    fn only_async_rollback_states_require_a_job() {
        assert!(!ImportRollbackRequestStatus::Prechecked.requires_job());
        assert!(!ImportRollbackRequestStatus::PrecheckConflict.requires_job());
        assert!(ImportRollbackRequestStatus::Queued.requires_job());
        assert!(ImportRollbackRequestStatus::Running.requires_job());
        assert!(ImportRollbackRequestStatus::Succeeded.requires_job());
        assert!(ImportRollbackRequestStatus::WorkerConflict.requires_job());
        assert!(ImportRollbackRequestStatus::Failed.requires_job());
    }

    #[test]
    fn phase_3d_errors_have_stable_external_codes() {
        let cases = [
            (
                ImportWorkflowErrorCode::RollbackNotAllowed,
                "rollback_not_allowed",
            ),
            (
                ImportWorkflowErrorCode::RollbackNotAvailable,
                "rollback_not_available",
            ),
            (
                ImportWorkflowErrorCode::RollbackPreconditionStale,
                "rollback_precondition_stale",
            ),
            (
                ImportWorkflowErrorCode::RollbackConflict,
                "rollback_conflict",
            ),
            (
                ImportWorkflowErrorCode::RollbackAlreadyCompleted,
                "rollback_already_completed",
            ),
            (
                ImportWorkflowErrorCode::RollbackInProgress,
                "rollback_in_progress",
            ),
            (
                ImportWorkflowErrorCode::RollbackIdempotencyKeyReused,
                "rollback_idempotency_key_reused",
            ),
            (
                ImportWorkflowErrorCode::CompensationNotAllowed,
                "compensation_not_allowed",
            ),
            (
                ImportWorkflowErrorCode::CompensationCycle,
                "compensation_cycle",
            ),
            (
                ImportWorkflowErrorCode::CompensationLineageTooDeep,
                "compensation_lineage_too_deep",
            ),
            (
                ImportWorkflowErrorCode::ObjectConsistencyError,
                "object_consistency_error",
            ),
        ];
        for (error, code) in cases {
            assert_eq!(error.as_str(), code);
        }
    }

    #[test]
    fn only_final_job_states_are_terminal() {
        assert!(!ImportJobStatus::Queued.is_terminal());
        assert!(!ImportJobStatus::Running.is_terminal());
        assert!(ImportJobStatus::Succeeded.is_terminal());
        assert!(ImportJobStatus::Failed.is_terminal());
        assert!(ImportJobStatus::DeadLetter.is_terminal());
    }
}
