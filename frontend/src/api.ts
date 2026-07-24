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

export async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: 'include' })
  if (!response.ok) {
    throw new Error(`request failed: ${response.status}`)
  }
  return response.json() as Promise<T>
}
