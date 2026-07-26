import { isAuthorizationError } from './api'
import type { ImportJobEvent, ImportJobStatus, ImportSummary } from './api'

export const terminalImportStatuses = new Set<ImportJobStatus>([
  'succeeded',
  'failed',
  'dead_letter',
  'rolled_back',
  'rollback_conflict',
  'rollback_failed'
])

export interface ImportProgressFollower {
  stream: (
    importId: string,
    after: number | null,
    onEvent: (event: ImportJobEvent) => void,
    signal: AbortSignal
  ) => Promise<void>
  refresh: (importId: string) => Promise<ImportSummary>
  delay?: (signal: AbortSignal) => Promise<void>
}

export interface ImportProgressCallbacks {
  onEvent: (event: ImportJobEvent) => void
  onRefresh: (summary: ImportSummary) => void
  onDisconnected: (disconnected: boolean) => void
  onAccessEnded: () => void
  onRetryError?: () => void
}

function defaultDelay(signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const timer = window.setTimeout(resolve, 3000)
    signal.addEventListener('abort', () => {
      window.clearTimeout(timer)
      resolve()
    }, { once: true })
  })
}

export async function followImportProgress(
  importId: string,
  initialSequence: number | null,
  initialStatus: ImportJobStatus | null,
  signal: AbortSignal,
  follower: ImportProgressFollower,
  callbacks: ImportProgressCallbacks
): Promise<void> {
  let sequence = initialSequence
  let status = initialStatus
  const delay = follower.delay ?? defaultDelay

  while (!signal.aborted && (!status || !terminalImportStatuses.has(status))) {
    try {
      await follower.stream(importId, sequence, (event) => {
        if (sequence !== null && event.event_seq <= sequence) return
        sequence = event.event_seq
        status = event.status
        callbacks.onDisconnected(false)
        callbacks.onEvent(event)
      }, signal)
      if (status && terminalImportStatuses.has(status)) return
      callbacks.onDisconnected(true)
    } catch (error) {
      if (signal.aborted) return
      if (isAuthorizationError(error)) {
        callbacks.onAccessEnded()
        return
      }
      callbacks.onDisconnected(true)
    }

    try {
      const summary = await follower.refresh(importId)
      callbacks.onRefresh(summary)
      status = summary.job?.status ?? (summary.status as ImportJobStatus)
      if (terminalImportStatuses.has(status)) return
    } catch (error) {
      if (isAuthorizationError(error)) {
        callbacks.onAccessEnded()
        return
      }
      callbacks.onRetryError?.()
    }
    await delay(signal)
  }
}
