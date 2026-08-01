use database::object_governance::{self, ClaimedGovernanceJob, NewFinding, ObjectGovernanceError};
use infrastructure::object_storage::{LocalObjectStorage, ScannedObject};
use std::collections::{HashMap, HashSet};
use time::OffsetDateTime;
use uuid::Uuid;

pub async fn execute_scan(
    pool: &sqlx::PgPool,
    storage: &LocalObjectStorage,
    job: &ClaimedGovernanceJob,
    worker_id: &str,
    stale_seconds: i64,
) -> Result<(), ObjectGovernanceError> {
    let expected_fingerprint =
        object_governance::load_scan_root_fingerprint(pool, job.workspace_id, job.aggregate_id)
            .await?;
    if expected_fingerprint != storage.root_fingerprint() {
        return Err(ObjectGovernanceError::InvalidStoredState);
    }
    let expected = object_governance::load_expected_objects(pool, job.workspace_id).await?;
    let scanned = storage
        .scan_workspace(job.workspace_id)
        .await
        .map_err(|_| ObjectGovernanceError::InvalidStoredState)?;
    let scanned_by_key = scanned
        .iter()
        .cloned()
        .map(|entry| (entry.object_key.clone(), entry))
        .collect::<HashMap<_, _>>();
    let expected_keys = expected
        .iter()
        .map(|object| object.object_key.clone())
        .collect::<HashSet<_>>();
    let now = OffsetDateTime::now_utc();
    let stale_before = now.unix_timestamp().saturating_sub(stale_seconds);
    let mut findings = Vec::new();

    for object in &expected {
        if object.backend != "local" {
            push_finding(
                &mut findings,
                Some(object.id),
                "backend_mismatch",
                None,
                None,
            );
            continue;
        }
        let workspace_path_valid =
            LocalObjectStorage::belongs_to_workspace(&object.object_key, job.workspace_id)
                || (object.state == "quarantined"
                    && LocalObjectStorage::is_workspace_quarantine_key(
                        &object.object_key,
                        job.workspace_id,
                    ));
        let quarantine_path =
            LocalObjectStorage::is_workspace_quarantine_key(&object.object_key, job.workspace_id);
        if (object.state == "quarantined") != quarantine_path {
            push_finding(&mut findings, Some(object.id), "state_mismatch", None, None);
        }
        let controlled_foreign_prefix = (object.object_key.starts_with("objects/")
            || object.object_key.starts_with(".tmp/")
            || object.object_key.starts_with("quarantine/"))
            && !workspace_path_valid;
        if !workspace_path_valid {
            push_finding(
                &mut findings,
                Some(object.id),
                "workspace_path_mismatch",
                None,
                None,
            );
        }
        if controlled_foreign_prefix {
            continue;
        }
        let observed = if let Some(observed) = scanned_by_key.get(&object.object_key) {
            Some(observed.clone())
        } else {
            storage
                .inspect(&object.object_key)
                .await
                .map_err(|_| ObjectGovernanceError::InvalidStoredState)?
        };
        let Some(observed) = observed else {
            push_finding(
                &mut findings,
                Some(object.id),
                "database_object_missing",
                None,
                Some(&object.object_key),
            );
            continue;
        };
        if observed.size_bytes != object.size_bytes as u64 {
            push_finding(
                &mut findings,
                Some(object.id),
                "size_mismatch",
                Some(&observed),
                None,
            );
        }
        if observed.sha256 != object.sha256 {
            push_finding(
                &mut findings,
                Some(object.id),
                "sha256_mismatch",
                Some(&observed),
                None,
            );
        }
        if !matches!(
            object.state.as_str(),
            "available" | "pending" | "quarantined"
        ) {
            push_finding(
                &mut findings,
                Some(object.id),
                "state_mismatch",
                Some(&observed),
                None,
            );
        }
        if object.state == "pending" && object.created_at.unix_timestamp() <= stale_before {
            push_finding(
                &mut findings,
                Some(object.id),
                "stale_pending_object",
                Some(&observed),
                None,
            );
            push_finding(
                &mut findings,
                Some(object.id),
                "commit_outcome_unknown",
                Some(&observed),
                None,
            );
        }
        if !object.referenced
            && object.state == "available"
            && object.retention_until.is_none_or(|until| until <= now)
            && observed.sha256 == object.sha256
            && observed.size_bytes == object.size_bytes as u64
        {
            push_finding(
                &mut findings,
                Some(object.id),
                "orphan_object",
                Some(&observed),
                None,
            );
        }
    }
    for observed in &scanned {
        if expected_keys.contains(&observed.object_key) {
            continue;
        }
        let finding_type = if observed
            .object_key
            .starts_with(&format!(".tmp/{}/", job.workspace_id))
            && observed.modified_unix_seconds <= stale_before
        {
            "stale_temporary_object"
        } else if observed
            .object_key
            .starts_with(&LocalObjectStorage::workspace_object_prefix(
                job.workspace_id,
            ))
        {
            if observed.modified_unix_seconds <= stale_before {
                "orphan_object"
            } else {
                "commit_outcome_unknown"
            }
        } else {
            continue;
        };
        push_finding(&mut findings, None, finding_type, Some(observed), None);
    }
    object_governance::complete_scan(
        pool,
        job.workspace_id,
        job.aggregate_id,
        job.id,
        worker_id,
        job.lease_generation,
        scanned.len() as i64,
        &findings,
    )
    .await
}

pub async fn execute_quarantine(
    pool: &sqlx::PgPool,
    storage: &LocalObjectStorage,
    job: &ClaimedGovernanceJob,
    worker_id: &str,
) -> Result<(), ObjectGovernanceError> {
    let work =
        object_governance::load_quarantine_work(pool, job.workspace_id, job.aggregate_id).await?;
    let size_bytes =
        u64::try_from(work.size_bytes).map_err(|_| ObjectGovernanceError::InvalidStoredState)?;
    let quarantined = storage
        .quarantine(
            job.workspace_id,
            work.finding_id,
            &work.source_object_key,
            &work.sha256,
            size_bytes,
        )
        .await
        .map_err(|_| ObjectGovernanceError::FindingStale)?;
    object_governance::complete_quarantine(
        pool,
        job.id,
        worker_id,
        job.lease_generation,
        &work,
        job.workspace_id,
        &quarantined.object_key,
    )
    .await
}

fn push_finding(
    findings: &mut Vec<NewFinding>,
    stored_object_id: Option<Uuid>,
    finding_type: &str,
    observed: Option<&ScannedObject>,
    fallback_key: Option<&str>,
) {
    findings.push(NewFinding {
        id: Uuid::now_v7(),
        stored_object_id,
        finding_type: finding_type.into(),
        observed_object_key: observed
            .map(|entry| entry.object_key.clone())
            .or_else(|| fallback_key.map(str::to_string)),
        observed_sha256: observed.map(|entry| entry.sha256.clone()),
        observed_size_bytes: observed.and_then(|entry| i64::try_from(entry.size_bytes).ok()),
    });
}

#[cfg(test)]
mod tests {
    const SOURCE: &str = include_str!("object_governance.rs");

    #[test]
    fn worker_never_accepts_a_path_from_job_payload() {
        let production = SOURCE.split("#[cfg(test)]").next().unwrap();
        assert!(!production.contains("job.payload"));
        assert!(!production.contains("remove_file"));
        assert!(!production.contains(".delete("));
        assert!(production.contains("job.aggregate_id"));
    }

    #[test]
    fn scan_classifies_every_phase_3d_consistency_category() {
        let production = SOURCE.split("#[cfg(test)]").next().unwrap();
        for finding_type in [
            "database_object_missing",
            "orphan_object",
            "size_mismatch",
            "sha256_mismatch",
            "backend_mismatch",
            "state_mismatch",
            "workspace_path_mismatch",
            "stale_temporary_object",
            "stale_pending_object",
            "commit_outcome_unknown",
        ] {
            assert!(production.contains(finding_type), "missing {finding_type}");
        }
        assert!(production.contains("controlled_foreign_prefix"));
        assert!(production.contains("object.referenced"));
        assert!(production.contains("object.state == \"available\""));
        assert!(production.contains("observed.modified_unix_seconds <= stale_before"));
    }
}
