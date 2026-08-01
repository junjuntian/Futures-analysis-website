import { defineComponent, h, onMounted, ref } from 'vue'
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'
import { ApiError } from './api'
import type { ImportJobEvent, ImportSummary } from './api'
import { followImportProgress } from './importProgress'

const progressEvent: ImportJobEvent = {
  event_seq: 8,
  event_type: 'progress',
  status: 'running',
  processed_rows: 1,
  total_rows: 2,
  inserted_count: 1,
  updated_count: 0,
  skipped_count: 0,
  conflict_count: 0
}
const terminalEvent: ImportJobEvent = {
  ...progressEvent,
  event_seq: 9,
  event_type: 'succeeded',
  status: 'succeeded',
  processed_rows: 2,
  inserted_count: 2
}
const runningSummary: ImportSummary = {
  id: 'batch',
  status: 'running',
  file: {
    id: 'file', original_filename: 'data.csv', declared_mime_type: 'text/csv',
    detected_format: 'csv', sha256: 'a'.repeat(64), size_bytes: 10
  },
  created_at: '2026-07-26T00:00:00Z',
  updated_at: '2026-07-26T00:00:00Z',
  validation: null,
  job: {
    job_id: 'job', status: 'running', processed_rows: 1, total_rows: 2,
    inserted_count: 1, updated_count: 0, skipped_count: 0, conflict_count: 0,
    attempt_count: 1, max_attempts: 5
  },
  conflict_policy: 'skip'
}

describe('import SSE follower component integration', () => {
  it('reconnects with the last event id after a clean disconnect', async () => {
    const afterValues: Array<number | null> = []
    const disconnects: boolean[] = []
    const stream = vi.fn(async (_id, after: number | null, onEvent: (value: ImportJobEvent) => void) => {
      afterValues.push(after)
      onEvent(stream.mock.calls.length === 1 ? progressEvent : terminalEvent)
    })
    let finished: Promise<void> | undefined
    const Harness = defineComponent({
      setup() {
        const status = ref('starting')
        onMounted(() => {
          finished = followImportProgress('batch', 7, 'running', new AbortController().signal,
            { stream, refresh: async () => runningSummary, delay: async () => undefined },
            {
              onEvent: (value) => { status.value = value.status },
              onRefresh: () => undefined,
              onDisconnected: (value) => disconnects.push(value),
              onAccessEnded: () => { status.value = 'access-ended' }
            })
        })
        return () => h('div', status.value)
      }
    })
    const wrapper = mount(Harness)
    await finished
    expect(afterValues).toEqual([7, 8])
    expect(disconnects).toContain(true)
    expect(wrapper.text()).toBe('succeeded')
  })

  it('stops reconnecting when fallback detects revoked permission', async () => {
    const stream = vi.fn(async () => undefined)
    const delay = vi.fn(async () => undefined)
    let finished: Promise<void> | undefined
    const Harness = defineComponent({
      setup() {
        const status = ref('starting')
        onMounted(() => {
          finished = followImportProgress('batch', null, 'running', new AbortController().signal,
            {
              stream,
              refresh: async () => { throw new ApiError(403, 'permission_denied', 'denied') },
              delay
            },
            {
              onEvent: () => undefined,
              onRefresh: () => undefined,
              onDisconnected: () => undefined,
              onAccessEnded: () => { status.value = 'access-ended' }
            })
        })
        return () => h('div', status.value)
      }
    })
    const wrapper = mount(Harness)
    await finished
    expect(stream).toHaveBeenCalledOnce()
    expect(delay).not.toHaveBeenCalled()
    expect(wrapper.text()).toBe('access-ended')
  })
})
