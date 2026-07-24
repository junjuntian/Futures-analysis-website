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
