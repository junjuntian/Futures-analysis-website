export interface ApiEnvelope<T> {
  data: T
  meta: { request_id: string }
}

interface ApiErrorBody {
  code?: string
  message?: string
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId?: string
  readonly data?: unknown

  constructor(status: number, code: string, message: string, requestId?: string, data?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.requestId = requestId
    this.data = data
  }
}

export function isAuthorizationError(error: unknown): error is ApiError {
  return error instanceof ApiError && (error.status === 401 || error.status === 403)
}

export type ImportConflictPolicy = 'skip' | 'overwrite' | 'keep_conflict' | 'abort'
export type ImportEventType =
  | 'queued'
  | 'running'
  | 'progress'
  | 'succeeded'
  | 'failed'
  | 'dead_letter'
  | 'rollback_queued'
  | 'rollback_running'
  | 'rollback_conflict'
  | 'rolled_back'
  | 'rollback_failed'
export type ImportJobStatus = ImportEventType

export interface HealthStatus { status: string; checked_at: string }
export interface VersionInfo { name: string; version: string; git_sha: string }
export interface UserSummary { id: string; username: string; roles: string[]; permissions: string[] }
export interface WorkspaceSummary { id: string; name: string }
export interface MeResponse { user: UserSummary; workspace: WorkspaceSummary }
export interface SessionSummary {
  id: string
  current: boolean
  created_at: string
  last_seen_at: string
  absolute_expires_at: string
  idle_expires_at: string
  user_agent?: string | null
}

export interface ImportFileSummary {
  id: string
  original_filename: string
  declared_mime_type: string
  detected_format: string
  sha256: string
  size_bytes: number
}

export interface ImportValidationSummary {
  import_id: string
  validation_version: string
  staging_version?: number
  blocking_error_count: number
  warning_count: number
  duplicate_count: number
  conflict_count: number
  allowed_conflict_policies: ImportConflictPolicy[]
}

export interface ImportProgress {
  processed_count: number
  total_count: number
  imported_count: number
  skipped_count: number
  overwritten_count: number
  conflict_count: number
}

export interface ImportJobSummary {
  job_id: string
  status: ImportJobStatus
  processed_rows: number
  total_rows: number
  inserted_count: number
  updated_count: number
  skipped_count: number
  conflict_count: number
  error_code?: string | null
  attempt_count?: number
  max_attempts?: number
}

export interface NormalizedImportJob {
  job_id: string
  status: ImportJobStatus
  attempt_count: number
  max_attempts: number
  progress: ImportProgress
  error_code?: string | null
}

export interface ImportSummary {
  id: string
  status: string
  file: ImportFileSummary
  created_at: string
  updated_at: string
  validation?: ImportValidationSummary | null
  job?: ImportJobSummary | null
  conflict_policy?: ImportConflictPolicy | null
}

export function normalizeImportJob(
  job: ImportSummary['job'],
  fallbackTotal = 0
): NormalizedImportJob | null {
  if (!job) return null
  return {
    job_id: job.job_id,
    status: job.status,
    attempt_count: job.attempt_count ?? 0,
    max_attempts: job.max_attempts ?? 5,
    progress: {
      processed_count: job.processed_rows,
      total_count: job.total_rows || fallbackTotal,
      imported_count: job.inserted_count,
      overwritten_count: job.updated_count,
      skipped_count: job.skipped_count,
      conflict_count: job.conflict_count
    },
    error_code: job.error_code
  }
}

export interface ImportMappingField {
  source_column: string
  target_field: string
  transform?: string | null
}
export interface ImportPreviewCell {
  column: string
  raw_value: string
  normalized_value: string | null
  target_field?: string | null
  errors: string[]
  warnings: string[]
}
export interface ImportPreviewRow {
  row_number: number
  cells: ImportPreviewCell[]
  errors: string[]
  warnings: string[]
}
export interface ImportErrorItem {
  row_number?: number | null
  field_name?: string | null
  severity: 'error' | 'warning' | 'duplicate' | 'conflict'
  error_code: string
  raw_value?: string | null
  message: string
}
export interface ImportInspectResponse {
  import_id: string
  status: string
  detected_format: string
  encoding: { value?: string | null; confidence: number; candidates: string[]; overridden: boolean }
  delimiter: { value?: string | null; confidence: number; candidates: string[]; overridden: boolean }
  sheets: Array<{ name: string; row_count: number; column_count: number; selected: boolean }>
  selected_sheet?: string | null
  header_row: number
  columns: Array<{ index: number; name: string }>
  preview_rows: ImportPreviewRow[]
  total_rows: number
  preview_row_count: number
  preview_invalidated: boolean
  errors: ImportErrorItem[]
  warnings: ImportErrorItem[]
}
export interface ImportConfirmationSummary {
  import_id: string
  job_id: string
  status: ImportJobStatus
  conflict_policy: ImportConflictPolicy
  replayed: boolean
}
export interface ImportJobEvent {
  event_seq: number
  event_type: ImportEventType
  status: ImportJobStatus
  processed_rows: number
  total_rows: number
  inserted_count: number
  updated_count: number
  skipped_count: number
  conflict_count: number
  error_code?: string | null
}
export interface ImportErrorPage {
  import_id: string
  items: ImportErrorItem[]
  next_cursor?: string | null
}
export interface ImportTemplateSummary {
  id: string
  dataset_type: string
  name: string
  description?: string | null
  latest_version_id: string
  latest_version_number: number
  fields: ImportMappingField[]
}
export interface ImportMappingResponse {
  import_id: string
  status: string
  dataset_type: string
  template_version_id?: string | null
  fields: ImportMappingField[]
  preview_invalidated: boolean
}
export interface ImportDatasetFieldDefinition { code: string; label: string; transforms: string[] }
export interface ImportDatasetDefinition {
  dataset_type: string
  fields: ImportDatasetFieldDefinition[]
}

export type RollbackCapability = 'direct' | 'compensation_only'
export interface ImportRollbackConflict {
  conflict_seq: number
  conflict_type: string
  target_kind?: string | null
  target_id?: string | null
  expected_row_version?: number | null
  current_row_version?: number | null
  dependency_kind?: string | null
  detail_code: string
}
export interface ImportRollbackCheck {
  import_id: string
  precheck_request_id: string
  precheck_fingerprint: string
  rollback_capability: RollbackCapability
  change_log_version?: number | null
  can_rollback: boolean
  compensation_recommended: boolean
  affected_count: number
  conflict_count: number
  conflicts: ImportRollbackConflict[]
  next_cursor?: string | null
}
export interface ImportRollbackConflictPage {
  import_id: string
  precheck_request_id: string
  items: ImportRollbackConflict[]
  next_cursor?: string | null
}
export interface ImportRollbackResult {
  import_id: string
  precheck_request_id: string
  job_id: string
  status: ImportJobStatus
  replayed: boolean
}
export interface ImportCompensationResult {
  original_import_id: string
  compensation_import_id: string
  status: string
  reason: string
  requested_by: string
  file: {
    file_id: string
    original_filename: string
    detected_format: string
    sha256: string
    size_bytes: number
  }
  replayed: boolean
}
export interface ImportLineageNode {
  import_id: string
  status: string
  compensates_import_id?: string | null
  compensation_reason?: string | null
  created_by: string
  confirmed_by?: string | null
  rollback_capability: RollbackCapability
  mapping_id?: string | null
  created_at: string
  confirmed_at?: string | null
  rolled_back_at?: string | null
  file: {
    file_id: string
    object_id: string
    original_filename: string
    detected_format: string
    sha256: string
    size_bytes: number
    object_state: string
  }
  jobs: Array<{ job_id: string; job_type: string; status: string; attempt_count: number; error_code?: string | null }>
  rollbacks: Array<{ rollback_request_id: string; status: string; conflict_count: number; requested_by: string }>
}
export interface ImportLineageAudit {
  audit_id: string
  import_id: string
  event_type: string
  outcome: string
  actor_user_id?: string | null
  created_at: string
}
export interface ImportLineage {
  requested_import_id: string
  root_import_id: string
  nodes: ImportLineageNode[]
  audits: ImportLineageAudit[]
}

async function parseApiError(response: Response, fallback: string): Promise<ApiError> {
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    payload = undefined
  }
  const envelope = payload as Partial<ApiEnvelope<ApiErrorBody>> | undefined
  const body = envelope?.data
  const code = body?.code ?? `http_${response.status}`
  return new ApiError(
    response.status,
    code,
    body?.message ?? fallback,
    envelope?.meta?.request_id,
    body && !body.code ? body : payload
  )
}

export async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: 'include' })
  if (!response.ok) throw await parseApiError(response, `request failed: ${response.status}`)
  return response.json() as Promise<T>
}

export async function sendJson<T>(
  path: string,
  body: unknown,
  csrfToken?: string,
  method = 'POST',
  extraHeaders: Record<string, string> = {}
): Promise<T> {
  const headers: Record<string, string> = { 'content-type': 'application/json', ...extraHeaders }
  if (csrfToken) headers['x-csrf-token'] = csrfToken
  const response = await fetch(path, {
    method,
    headers,
    credentials: 'include',
    body: JSON.stringify(body ?? {})
  })
  if (!response.ok) throw await parseApiError(response, `request failed: ${response.status}`)
  return response.json() as Promise<T>
}

export async function confirmImport(
  importId: string,
  conflictPolicy: ImportConflictPolicy,
  csrfToken: string,
  idempotencyKey: string
): Promise<ApiEnvelope<ImportConfirmationSummary>> {
  return sendJson(`/api/v1/imports/${encodeURIComponent(importId)}/confirm`,
    { conflict_policy: conflictPolicy }, csrfToken, 'POST', { 'Idempotency-Key': idempotencyKey })
}

export async function getImportErrors(importId: string, cursor?: string | null): Promise<ApiEnvelope<ImportErrorPage>> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''
  return getJson(`/api/v1/imports/${encodeURIComponent(importId)}/errors${query}`)
}

export async function rollbackCheck(importId: string, csrfToken: string): Promise<ApiEnvelope<ImportRollbackCheck>> {
  return sendJson(`/api/v1/imports/${encodeURIComponent(importId)}/rollback-check`, {}, csrfToken)
}

export async function getRollbackConflicts(
  importId: string,
  precheckRequestId: string,
  cursor?: string | null
): Promise<ApiEnvelope<ImportRollbackConflictPage>> {
  const params = new URLSearchParams({ precheck_request_id: precheckRequestId })
  if (cursor) params.set('cursor', cursor)
  return getJson(`/api/v1/imports/${encodeURIComponent(importId)}/rollback-conflicts?${params}`)
}

export async function requestRollback(
  importId: string,
  check: ImportRollbackCheck,
  csrfToken: string,
  idempotencyKey: string
): Promise<ApiEnvelope<ImportRollbackResult>> {
  const response = await fetch(`/api/v1/imports/${encodeURIComponent(importId)}/rollback`, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'content-type': 'application/json',
      'x-csrf-token': csrfToken,
      'Idempotency-Key': idempotencyKey
    },
    body: JSON.stringify({
      precheck_request_id: check.precheck_request_id,
      precheck_fingerprint: check.precheck_fingerprint
    })
  })
  if (response.ok) return response.json() as Promise<ApiEnvelope<ImportRollbackResult>>
  const payload = await response.clone().json().catch(() => undefined) as ApiEnvelope<unknown> | undefined
  if (response.status === 409 && payload?.data && 'precheck_fingerprint' in Object(payload.data)) {
    throw new ApiError(409, 'rollback_conflict', 'rollback conflicts with current state',
      payload.meta?.request_id, payload.data)
  }
  throw await parseApiError(response, `rollback failed: ${response.status}`)
}

export async function createCompensation(
  importId: string,
  file: File,
  reason: string,
  csrfToken: string,
  idempotencyKey: string
): Promise<ApiEnvelope<ImportCompensationResult>> {
  const form = new FormData()
  form.append('file', file)
  form.append('reason', reason)
  const response = await fetch(`/api/v1/imports/${encodeURIComponent(importId)}/compensations`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'x-csrf-token': csrfToken, 'Idempotency-Key': idempotencyKey },
    body: form
  })
  if (!response.ok) throw await parseApiError(response, `compensation failed: ${response.status}`)
  return response.json() as Promise<ApiEnvelope<ImportCompensationResult>>
}

export async function getImportLineage(importId: string): Promise<ApiEnvelope<ImportLineage>> {
  return getJson(`/api/v1/imports/${encodeURIComponent(importId)}/lineage`)
}

export async function streamImportEvents(
  importId: string,
  lastEventSequence: number | null,
  onEvent: (event: ImportJobEvent) => void,
  signal: AbortSignal
): Promise<void> {
  const headers: Record<string, string> = { accept: 'text/event-stream' }
  if (lastEventSequence !== null) headers['Last-Event-ID'] = String(lastEventSequence)
  const response = await fetch(`/api/v1/imports/${encodeURIComponent(importId)}/events`, {
    credentials: 'include', headers, signal
  })
  if (!response.ok) throw await parseApiError(response, `event stream failed: ${response.status}`)
  if (!response.body) throw new ApiError(0, 'event_stream_unavailable', 'event stream unavailable')
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  const dispatchBlock = (block: string) => {
    const data = block.split(/\r?\n/).filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart()).join('\n')
    if (!data) return
    const event = JSON.parse(data) as ImportJobEvent
    if (!Number.isSafeInteger(event.event_seq) || event.event_seq < 0) {
      throw new ApiError(0, 'event_sequence_invalid', 'event stream returned an invalid sequence')
    }
    onEvent(event)
  }
  while (!signal.aborted) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const blocks = buffer.split(/\r?\n\r?\n/)
    buffer = blocks.pop() ?? ''
    blocks.forEach(dispatchBlock)
    if (done) {
      if (buffer.trim()) dispatchBlock(buffer)
      return
    }
  }
}

export async function uploadImport(file: File, csrfToken: string): Promise<ApiEnvelope<ImportSummary>> {
  const form = new FormData()
  form.append('file', file)
  const response = await fetch('/api/v1/imports', {
    method: 'POST',
    headers: { 'x-csrf-token': csrfToken },
    credentials: 'include',
    body: form
  })
  if (!response.ok) throw await parseApiError(response, `upload failed: ${response.status}`)
  return response.json() as Promise<ApiEnvelope<ImportSummary>>
}
