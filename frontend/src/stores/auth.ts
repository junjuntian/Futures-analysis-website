import { defineStore } from 'pinia'
import {
  getJson,
  sendJson,
  type ApiEnvelope,
  type MeResponse,
  type SessionSummary,
  type WorkspaceSummary
} from '../api'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    me: null as MeResponse | null,
    workspace: null as WorkspaceSummary | null,
    sessions: [] as SessionSummary[],
    csrfToken: null as string | null,
    error: null as string | null,
    loading: false
  }),
  actions: {
    async refresh() {
      this.loading = true
      this.error = null
      try {
        const [me, workspace] = await Promise.all([
          getJson<ApiEnvelope<MeResponse>>('/api/v1/auth/me'),
          getJson<ApiEnvelope<WorkspaceSummary>>('/api/v1/workspace')
        ])
        this.me = me.data
        this.workspace = workspace.data
      } catch {
        this.me = null
        this.workspace = null
      } finally {
        this.loading = false
      }
    },
    async bootstrap(username: string, password: string, token: string) {
      this.loading = true
      this.error = null
      try {
        const response = await fetch('/api/v1/auth/bootstrap', {
          method: 'POST',
          credentials: 'include',
          headers: {
            'content-type': 'application/json',
            'x-bootstrap-token': token
          },
          body: JSON.stringify({ username, password })
        })
        if (!response.ok) {
          throw new Error(`request failed: ${response.status}`)
        }
        const envelope = (await response.json()) as ApiEnvelope<MeResponse>
        this.me = envelope.data
        this.workspace = envelope.data.workspace
        await this.loadCsrf()
      } catch (error) {
        this.error = error instanceof Error ? error.message : 'unknown error'
        throw error
      } finally {
        this.loading = false
      }
    },
    async login(username: string, password: string) {
      this.loading = true
      this.error = null
      try {
        const envelope = await sendJson<ApiEnvelope<MeResponse>>('/api/v1/auth/login', {
          username,
          password
        })
        this.me = envelope.data
        this.workspace = envelope.data.workspace
        await this.loadCsrf()
      } catch (error) {
        this.error = error instanceof Error ? error.message : 'unknown error'
        throw error
      } finally {
        this.loading = false
      }
    },
    async loadCsrf() {
      const envelope = await getJson<ApiEnvelope<{ csrf_token: string }>>('/api/v1/auth/csrf')
      this.csrfToken = envelope.data.csrf_token
    },
    async loadSessions() {
      const envelope = await getJson<ApiEnvelope<SessionSummary[]>>('/api/v1/sessions')
      this.sessions = envelope.data
    },
    async revokeSession(sessionId: string) {
      if (!this.csrfToken) {
        await this.loadCsrf()
      }
      await sendJson<ApiEnvelope<{ ok: boolean }>>(
        `/api/v1/sessions/${sessionId}`,
        {},
        this.csrfToken ?? undefined,
        'DELETE'
      )
      await this.loadSessions()
    },
    async logout() {
      if (!this.csrfToken) {
        await this.loadCsrf()
      }
      await sendJson<ApiEnvelope<{ ok: boolean }>>('/api/v1/auth/logout', {}, this.csrfToken ?? undefined)
      this.me = null
      this.workspace = null
      this.sessions = []
      this.csrfToken = null
    }
  }
})
