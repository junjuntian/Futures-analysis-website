import { defineStore } from 'pinia'
import { authErrorText } from '../authErrors'
import {
  ApiError,
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
          // 读返回体再抛。原来这里直接抛「request failed: 400」,把后端说明问题的
          // code 整个丢掉——2026-08-13 新站建号时密码差几位,界面只有一个 400,
          // 运营者根本看不出是密码、用户名还是 token 的问题。
          let code: string | undefined
          try {
            const body = (await response.json()) as { data?: { code?: string } }
            code = body?.data?.code
          } catch {
            code = undefined
          }
          throw new Error(authErrorText(code, response.status))
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
        // sendJson 抛的是 ApiError,带着后端的 code——同样翻成人话。
        this.error =
          error instanceof ApiError
            ? authErrorText(error.code, error.status)
            : error instanceof Error
              ? error.message
              : 'unknown error'
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
    /** 改密码。旧密码由后端校验;成功后别的设备上的会话全部作废,当前这台留着。 */
    async changePassword(currentPassword: string, newPassword: string) {
      if (!this.csrfToken) {
        await this.loadCsrf()
      }
      const envelope = await sendJson<ApiEnvelope<{ ok: boolean; revoked_sessions: number }>>(
        '/api/v1/auth/password',
        { current_password: currentPassword, new_password: newPassword },
        this.csrfToken ?? undefined
      )
      return envelope.data.revoked_sessions
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
