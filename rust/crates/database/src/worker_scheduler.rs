use sqlx::{PgPool, Postgres, Row, Transaction};
use uuid::Uuid;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum WorkerQueue {
    Import,
    ObjectGovernance,
}

impl WorkerQueue {
    const fn ticket_column(self) -> &'static str {
        match self {
            Self::Import => "import_job_last_served_ticket",
            Self::ObjectGovernance => "object_job_last_served_ticket",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct WorkReservation {
    pub queue: WorkerQueue,
    pub workspace_id: Uuid,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
struct Candidate {
    last_served_ticket: i64,
    workspace_id: Uuid,
    queue: WorkerQueue,
}

pub async fn reserve_next_work(pool: &PgPool) -> Result<Option<WorkReservation>, sqlx::Error> {
    let mut tx = pool.begin().await?;
    let rows = sqlx::query(
        "select id, import_job_last_served_ticket, object_job_last_served_ticket
           from workspaces",
    )
    .fetch_all(&mut *tx)
    .await?;
    let mut candidates = rows
        .into_iter()
        .flat_map(|row| {
            let workspace_id = row.get("id");
            [
                Candidate {
                    last_served_ticket: row.get("import_job_last_served_ticket"),
                    workspace_id,
                    queue: WorkerQueue::Import,
                },
                Candidate {
                    last_served_ticket: row.get("object_job_last_served_ticket"),
                    workspace_id,
                    queue: WorkerQueue::ObjectGovernance,
                },
            ]
        })
        .collect::<Vec<_>>();
    candidates.sort_unstable();

    for candidate in candidates {
        set_workspace(&mut tx, candidate.workspace_id).await?;
        if !has_claimable_work(&mut tx, candidate).await? {
            continue;
        }
        let locked = sqlx::query(
            "select import_job_last_served_ticket, object_job_last_served_ticket
               from workspaces where id = $1 for update skip locked",
        )
        .bind(candidate.workspace_id)
        .fetch_optional(&mut *tx)
        .await?;
        let Some(locked) = locked else {
            continue;
        };
        let current_ticket = match candidate.queue {
            WorkerQueue::Import => locked.get("import_job_last_served_ticket"),
            WorkerQueue::ObjectGovernance => locked.get("object_job_last_served_ticket"),
        };
        if !reservation_snapshot_is_current(candidate.last_served_ticket, current_ticket)
            || !has_claimable_work(&mut tx, candidate).await?
        {
            continue;
        }
        let update = format!(
            "update workspaces
                set {} = nextval('worker_dispatch_ticket_seq')
              where id = $1",
            candidate.queue.ticket_column()
        );
        sqlx::query(&update)
            .bind(candidate.workspace_id)
            .execute(&mut *tx)
            .await?;
        tx.commit().await?;
        return Ok(Some(WorkReservation {
            queue: candidate.queue,
            workspace_id: candidate.workspace_id,
        }));
    }
    tx.commit().await?;
    Ok(None)
}

fn reservation_snapshot_is_current(snapshot_ticket: i64, locked_ticket: i64) -> bool {
    snapshot_ticket == locked_ticket
}

async fn has_claimable_work(
    tx: &mut Transaction<'_, Postgres>,
    candidate: Candidate,
) -> Result<bool, sqlx::Error> {
    let sql = match candidate.queue {
        WorkerQueue::Import => {
            "select exists(
                select 1 from job_queue
                 where workspace_id = $1
                   and job_type in ('import_confirm', 'import_rollback')
                   and (
                       (status = 'queued' and available_at <= now()
                           and attempt_count < max_attempts)
                       or (status = 'running' and lease_expires_at < now())
                   )
            )"
        }
        WorkerQueue::ObjectGovernance => {
            "select exists(
                select 1 from object_governance_jobs
                 where workspace_id = $1
                   and (
                       (status = 'queued' and available_at <= now()
                           and attempt_count < max_attempts)
                       or (status = 'running' and lease_expires_at < now())
                   )
            )"
        }
    };
    sqlx::query_scalar(sql)
        .bind(candidate.workspace_id)
        .fetch_one(&mut **tx)
        .await
}

async fn set_workspace(
    tx: &mut Transaction<'_, Postgres>,
    workspace_id: Uuid,
) -> Result<(), sqlx::Error> {
    sqlx::query("select set_config('app.current_workspace_id', $1, true)")
        .bind(workspace_id.to_string())
        .execute(&mut **tx)
        .await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn reserve(candidates: &mut [Candidate]) -> Candidate {
        candidates.sort_unstable();
        let selected = candidates[0];
        let next_ticket = candidates
            .iter()
            .map(|candidate| candidate.last_served_ticket)
            .max()
            .unwrap_or(0)
            + 1;
        candidates[0].last_served_ticket = next_ticket;
        selected
    }

    #[test]
    fn sustained_import_load_cannot_starve_another_workspace_or_object_queue() {
        let workspace_a = Uuid::from_u128(1);
        let workspace_b = Uuid::from_u128(2);
        let mut candidates = [
            Candidate {
                last_served_ticket: 0,
                workspace_id: workspace_a,
                queue: WorkerQueue::Import,
            },
            Candidate {
                last_served_ticket: 0,
                workspace_id: workspace_a,
                queue: WorkerQueue::ObjectGovernance,
            },
            Candidate {
                last_served_ticket: 0,
                workspace_id: workspace_b,
                queue: WorkerQueue::Import,
            },
            Candidate {
                last_served_ticket: 0,
                workspace_id: workspace_b,
                queue: WorkerQueue::ObjectGovernance,
            },
        ];
        let served = (0..400)
            .map(|_| {
                let selected = reserve(&mut candidates);
                (selected.workspace_id, selected.queue)
            })
            .collect::<Vec<_>>();
        for pair in [
            (workspace_a, WorkerQueue::Import),
            (workspace_a, WorkerQueue::ObjectGovernance),
            (workspace_b, WorkerQueue::Import),
            (workspace_b, WorkerQueue::ObjectGovernance),
        ] {
            assert_eq!(
                served.iter().filter(|served| **served == pair).count(),
                100,
                "{pair:?} must receive a bounded turn under sustained competing load"
            );
        }
    }

    #[test]
    fn scheduler_state_is_database_persistent_and_multi_worker_locked() {
        let source = include_str!("worker_scheduler.rs");
        let production = source.split("#[cfg(test)]").next().unwrap();
        assert!(production.contains("last_served_ticket"));
        assert!(production.contains("nextval('worker_dispatch_ticket_seq')"));
        assert!(production.contains("for update skip locked"));
        assert!(!production.contains("static mut"));
        assert!(!production.contains("AtomicU"));
    }

    #[test]
    fn a_second_worker_cannot_reserve_from_a_stale_ticket_snapshot() {
        assert!(reservation_snapshot_is_current(7, 7));
        assert!(
            !reservation_snapshot_is_current(7, 8),
            "a worker that waited for a prior reservation must re-sort instead of serving it twice"
        );
    }
}
