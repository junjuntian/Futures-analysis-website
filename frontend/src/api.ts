export interface ApiEnvelope<T> {
  data: T
  meta: {
    request_id: string
  }
}

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
}

export interface ImportMappingField {
  source_column: string
  target_field: string
  transform?: string | null
}

export interface ImportPreviewCell {
  column: string
  raw_value: string
  normalized_value: string
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
  severity: 'error' | 'warning'
  error_code: string
  raw_value?: string | null
  message: string
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
  method = 'POST'
): Promise<T> {
  const headers: Record<string, string> = {
    'content-type': 'application/json'
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
