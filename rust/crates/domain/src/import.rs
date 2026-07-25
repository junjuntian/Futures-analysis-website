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
}
