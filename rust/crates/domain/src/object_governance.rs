use serde::{Deserialize, Serialize};
use utoipa::ToSchema;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ObjectConsistencyRun {
    pub run_id: Uuid,
    pub job_id: Uuid,
    pub status: String,
    pub scanned_object_count: u64,
    pub finding_count: u64,
    pub replayed: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ObjectConsistencyFinding {
    pub finding_id: Uuid,
    pub run_id: Uuid,
    pub stored_object_id: Option<Uuid>,
    pub finding_type: String,
    pub observed_object_key: Option<String>,
    pub observed_sha256: Option<String>,
    pub observed_size_bytes: Option<u64>,
    pub disposition_status: String,
    pub quarantine_eligible: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ObjectConsistencyReport {
    pub run: ObjectConsistencyRun,
    pub findings: Vec<ObjectConsistencyFinding>,
}

#[derive(Debug, Clone, Serialize, Deserialize, ToSchema)]
pub struct ObjectQuarantineResponse {
    pub quarantine_request_id: Uuid,
    pub finding_id: Uuid,
    pub job_id: Uuid,
    pub status: String,
    pub replayed: bool,
}
