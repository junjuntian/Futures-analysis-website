import { defineStore } from 'pinia'
import { getJson, type ApiEnvelope, type HealthStatus, type VersionInfo } from '../api'

export const useHealthStore = defineStore('health', {
  state: () => ({
    live: null as HealthStatus | null,
    ready: null as HealthStatus | null,
    version: null as VersionInfo | null,
    error: null as string | null,
    loading: false
  }),
  actions: {
    async refresh() {
      this.loading = true
      this.error = null
      try {
        const [live, ready, version] = await Promise.all([
          getJson<ApiEnvelope<HealthStatus>>('/api/v1/health/live'),
          getJson<ApiEnvelope<HealthStatus>>('/api/v1/health/ready'),
          getJson<ApiEnvelope<VersionInfo>>('/api/v1/version')
        ])
        this.live = live.data
        this.ready = ready.data
        this.version = version.data
      } catch (error) {
        this.error = error instanceof Error ? error.message : 'unknown error'
      } finally {
        this.loading = false
      }
    }
  }
})
