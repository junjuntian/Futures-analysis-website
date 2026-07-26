export interface ApiEnvelope<T> {
  data: T
  meta: {
    request_id: string
  }
}

export type ImportConflictPolicy = 'skip' | 'overwrite' | 'keep_conflict' | 'abort'

export type ImportJobStatus =
  | 'queued'
  | 'running'
  | 'progress'
  | 'succeeded'
  | 'failed'
  | 'dead_letter'

export interface HealthStatus {
  status: string
  checked_at: string
}

export interface VersionInfo {
  name: string
  version: string
  git_sha: string
}

export interface UserSummary {
  id: string
  username: string
  roles: string[]
  permissions: string[]
}

export interface WorkspaceSummary {
  id: string
  name: string
}

export interface MeResponse {
  user: UserSummary
  workspace: WorkspaceSummary
}

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

export interface ImportSummary {
  id: string
  status: string
  file: ImportFileSummary
  created_at: string
  updated_at: string
  validation?: ImportValidationSummary | null
  validation_summary?: ImportValidationSummary | null
  job?: ImportJobSummary | null
  job_id?: string | null
  conflict_policy?: ImportConflictPolicy | null
  processed_rows?: number
  total_rows?: number
  inserted_count?: number
  updated_count?: number
  skipped_count?: number
  conflict_count?: number
  error_code?: string | null
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

export interface ImportErrorItem {
  row_number?: number | null
  field_name?: string | null
  severity: 'error' | 'warning' | 'duplicate' | 'conflict'
  error_code: string
  raw_value?: string | null
  message: string
}

export interface ImportValidationSummary {
  import_id: string
  validation_version: string
  blocking_error_count: number
  warning_count: number
  duplicate_count: number
  conflict_count: number
  allowed_conflict_policies: ImportConflictPolicy[]
}

export interface ImportConfirmationSummary {
  import_id: string
  job_id: string
  status: ImportJobStatus
  conflict_policy: ImportConflictPolicy
  replayed: boolean
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
}

export interface ImportJobEvent {
  event_seq: number
  event_type: ImportJobStatus
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

export interface ImportDatasetFieldDefinition {
  code: string
  label: string
  transforms: string[]
}

export interface ImportDatasetDefinition {
  dataset_type: string
  fields: ImportDatasetFieldDefinition[]
}

export async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: 'include' })
  if (!response.ok) {
    throw new Error(`request failed: ${response.status}`)
  }
  return response.json() as Promise<T>
}

export async function sendJson<T>(
  path: string,
  body: unknown,
  csrfToken?: string,
  method = 'POST',
  extraHeaders: Record<string, string> = {}
): Promise<T> {
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    ...extraHeaders
  }
  if (csrfToken) {
    headers['x-csrf-token'] = csrfToken
  }
  const response = await fetch(path, {
    method,
    headers,
    credentials: 'include',
    body: JSON.stringify(body ?? {})
  })
  if (!response.ok) {
    throw new Error(`request failed: ${response.status}`)
  }
  return response.json() as Promise<T>
}

export async function confirmImport(
  importId: string,
  conflictPolicy: ImportConflictPolicy,
  csrfToken: string,
  idempotencyKey: string
): Promise<ApiEnvelope<ImportConfirmationSummary>> {
  return sendJson<ApiEnvelope<ImportConfirmationSummary>>(
    `/api/v1/imports/${encodeURIComponent(importId)}/confirm`,
    { conflict_policy: conflictPolicy },
    csrfToken,
    'POST',
    { 'Idempotency-Key': idempotencyKey }
  )
}

export async function getImportErrors(
  importId: string,
  cursor?: string | null
): Promise<ApiEnvelope<ImportErrorPage>> {
  const query = cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''
  return getJson<ApiEnvelope<ImportErrorPage>>(
    `/api/v1/imports/${encodeURIComponent(importId)}/errors${query}`
  )
}

export async function streamImportEvents(
  importId: string,
  lastEventSequence: number | null,
  onEvent: (event: ImportJobEvent) => void,
  signal: AbortSignal
): Promise<void> {
  const headers: Record<string, string> = { accept: 'text/event-stream' }
  if (lastEventSequence !== null) {
    headers['Last-Event-ID'] = String(lastEventSequence)
  }

  const response = await fetch(
    `/api/v1/imports/${encodeURIComponent(importId)}/events`,
    {
      credentials: 'include',
      headers,
      signal
    }
  )
  if (!response.ok) {
    throw new Error(`event stream failed: ${response.status}`)
  }
  if (!response.body) {
    throw new Error('event stream unavailable')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  const dispatchBlock = (block: string) => {
    const data = block
      .split(/\r?\n/)
      .filter((line) => line.startsWith('data:'))
      .map((line) => line.slice(5).trimStart())
      .join('\n')
    if (!data) return
    const event = JSON.parse(data) as ImportJobEvent
    if (!Number.isSafeInteger(event.event_seq) || event.event_seq < 0) {
      throw new Error('event stream returned an invalid sequence')
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
  if (!response.ok) {
    throw new Error(`request failed: ${response.status}`)
  }
  return response.json() as Promise<ApiEnvelope<ImportSummary>>
}
