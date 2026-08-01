import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { followImportProgress } from '../importProgress'
import { useAuthStore } from '../stores/auth'
import ImportCenterView from './ImportCenterView.vue'
import type { ImportRollbackCheck, ImportSummary } from '../api'

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api')
  return {
    ...actual,
    confirmImport: vi.fn(),
    createCompensation: vi.fn(),
    getImportErrors: vi.fn(),
    getImportLineage: vi.fn(),
    getJson: vi.fn(),
    getRollbackConflicts: vi.fn(),
    requestRollback: vi.fn(),
    rollbackCheck: vi.fn(),
    sendJson: vi.fn(),
    streamImportEvents: vi.fn(),
    uploadImport: vi.fn()
  }
})

vi.mock('../importProgress', async () => {
  const actual = await vi.importActual<typeof import('../importProgress')>('../importProgress')
  return { ...actual, followImportProgress: vi.fn() }
})

const UploadStub = defineComponent({
  name: 'ElUpload',
  emits: ['update:fileList'],
  setup(_, { emit }) {
    function select(event: Event) {
      const file = (event.target as HTMLInputElement).files?.[0]
      emit('update:fileList', file ? [{ name: file.name, raw: file }] : [])
    }
    return { select }
  },
  template: '<input data-testid="view-upload-file" type="file" @change="select">'
})

const summary: ImportSummary = {
  id: '00000000-0000-7000-8000-000000000101',
  status: 'succeeded',
  file: {
    id: '00000000-0000-7000-8000-000000000102',
    original_filename: 'source.csv',
    declared_mime_type: 'text/csv',
    detected_format: 'csv',
    sha256: 'a'.repeat(64),
    size_bytes: 12
  },
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-01T00:00:00Z',
  validation: null,
  job: null,
  conflict_policy: null
}

const rollbackBase: ImportRollbackCheck = {
  import_id: summary.id,
  precheck_request_id: '00000000-0000-7000-8000-000000000103',
  precheck_fingerprint: 'b'.repeat(64),
  rollback_capability: 'direct',
  change_log_version: 1,
  can_rollback: true,
  compensation_recommended: false,
  affected_count: 1,
  conflict_count: 0,
  conflicts: [],
  next_cursor: null
}

function envelope<T>(data: T): api.ApiEnvelope<T> {
  return { data, meta: { request_id: '00000000-0000-7000-8000-000000000104' } }
}

function button(wrapper: ReturnType<typeof mount>, label: string) {
  const found = wrapper.findAll('button').find((item) => item.text().includes(label))
  if (!found) throw new Error(`button not found: ${label}`)
  return found
}

async function mountLoadedView() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.csrfToken = 'test-csrf'
  auth.me = {
    user: {
      id: '00000000-0000-7000-8000-000000000105',
      username: 'view-test',
      roles: ['admin'],
      permissions: ['import.rollback', 'import.compensate']
    },
    workspace: { id: '00000000-0000-7000-8000-000000000106', name: 'test' }
  }
  const wrapper = mount(ImportCenterView, {
    global: { plugins: [pinia, ElementPlus], stubs: { ElUpload: UploadStub } }
  })
  await flushPromises()
  const input = wrapper.get('[data-testid="view-upload-file"]').element as HTMLInputElement
  Object.defineProperty(input, 'files', {
    configurable: true,
    value: [new File(['date,code\n'], 'source.csv', { type: 'text/csv' })]
  })
  await wrapper.get('[data-testid="view-upload-file"]').trigger('change')
  await button(wrapper, '上传').trigger('click')
  await flushPromises()
  return wrapper
}

async function runRollbackCheck(wrapper: Awaited<ReturnType<typeof mountLoadedView>>) {
  await button(wrapper, '执行回滚预检').trigger('click')
  await flushPromises()
}

async function acknowledgeRollback(wrapper: Awaited<ReturnType<typeof mountLoadedView>>) {
  await wrapper.get('[data-testid="rollback-acknowledgement"]').setValue(true)
  await nextTick()
}

describe('ImportCenterView Phase 3D recovery interactions', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.stubGlobal('crypto', { randomUUID: vi.fn(() => '00000000-0000-7000-8000-000000000107') })
    vi.mocked(api.getJson).mockImplementation(async (path: string) => {
      if (path === '/api/v1/import-datasets') return envelope({ items: [] }) as never
      if (path === '/api/v1/import-templates') return envelope({ items: [] }) as never
      if (path === `/api/v1/imports/${summary.id}`) return envelope(summary) as never
      throw new Error(`unexpected GET ${path}`)
    })
    vi.mocked(api.uploadImport).mockResolvedValue(envelope(summary))
    vi.mocked(api.rollbackCheck).mockResolvedValue(envelope(rollbackBase))
    vi.mocked(api.getImportLineage).mockResolvedValue(envelope({
      requested_import_id: summary.id,
      root_import_id: summary.id,
      nodes: [],
      audits: []
    }))
    vi.mocked(followImportProgress).mockResolvedValue(undefined)
  })

  it('disables duplicate rollback submission while the first request is pending', async () => {
    let resolveRequest!: (value: api.ApiEnvelope<api.ImportRollbackResult>) => void
    vi.mocked(api.requestRollback).mockReturnValue(new Promise((resolve) => {
      resolveRequest = resolve
    }))
    const wrapper = await mountLoadedView()
    await runRollbackCheck(wrapper)
    await acknowledgeRollback(wrapper)

    const confirm = button(wrapper, '二次确认回滚')
    await confirm.trigger('click')
    await nextTick()
    expect(confirm.attributes('disabled')).toBeDefined()
    await confirm.trigger('click')
    expect(api.requestRollback).toHaveBeenCalledTimes(1)

    resolveRequest(envelope({
      import_id: summary.id,
      precheck_request_id: rollbackBase.precheck_request_id,
      job_id: '00000000-0000-7000-8000-000000000108',
      status: 'queued',
      replayed: false
    }))
    await flushPromises()
  })

  it('appends rollback conflict cursor pages without replacing the first page', async () => {
    vi.mocked(api.rollbackCheck).mockResolvedValue(envelope({
      ...rollbackBase,
      can_rollback: false,
      compensation_recommended: true,
      conflict_count: 2,
      conflicts: [{ conflict_seq: 1, conflict_type: 'later_modification', detail_code: 'first-conflict' }],
      next_cursor: 'cursor-2'
    }))
    vi.mocked(api.getRollbackConflicts).mockResolvedValue(envelope({
      import_id: summary.id,
      precheck_request_id: rollbackBase.precheck_request_id,
      items: [{ conflict_seq: 2, conflict_type: 'later_import', detail_code: 'second-conflict' }],
      next_cursor: null
    }))
    const wrapper = await mountLoadedView()
    await runRollbackCheck(wrapper)
    await button(wrapper, '加载更多冲突').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('first-conflict')
    expect(wrapper.text()).toContain('second-conflict')
    expect(api.getRollbackConflicts).toHaveBeenCalledWith(
      summary.id,
      rollbackBase.precheck_request_id,
      'cursor-2'
    )
  })

  it('recovers a stale precheck by returning to an enabled recheck action', async () => {
    vi.mocked(api.requestRollback).mockRejectedValue(new api.ApiError(
      409,
      'rollback_precondition_stale',
      'stale'
    ))
    const wrapper = await mountLoadedView()
    await runRollbackCheck(wrapper)
    await acknowledgeRollback(wrapper)
    await button(wrapper, '二次确认回滚').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('回滚预检已过期，请重新预检后再确认。')
    expect(button(wrapper, '执行回滚预检').attributes('disabled')).toBeUndefined()
    vi.mocked(api.rollbackCheck).mockResolvedValue(envelope({
      ...rollbackBase,
      precheck_request_id: '00000000-0000-7000-8000-000000000109',
      precheck_fingerprint: 'c'.repeat(64)
    }))
    await button(wrapper, '执行回滚预检').trigger('click')
    await flushPromises()
    expect(api.rollbackCheck).toHaveBeenCalledTimes(2)
    expect(wrapper.get('[data-testid="confirm-rollback"]').attributes('disabled')).toBeDefined()
  })

  it('shows a Worker error terminal state and its stable error code', async () => {
    vi.mocked(api.requestRollback).mockResolvedValue(envelope({
      import_id: summary.id,
      precheck_request_id: rollbackBase.precheck_request_id,
      job_id: '00000000-0000-7000-8000-000000000110',
      status: 'queued',
      replayed: false
    }))
    vi.mocked(followImportProgress).mockImplementation(async (...args) => {
      const callbacks = args[5]
      callbacks.onEvent({
        event_seq: 2,
        event_type: 'rollback_failed',
        status: 'rollback_failed',
        processed_rows: 0,
        total_rows: 1,
        inserted_count: 0,
        updated_count: 0,
        skipped_count: 0,
        conflict_count: 0,
        error_code: 'rollback_storage_failure'
      })
    })
    const wrapper = await mountLoadedView()
    await runRollbackCheck(wrapper)
    await acknowledgeRollback(wrapper)
    await button(wrapper, '二次确认回滚').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('rollback_failed')
    expect(wrapper.text()).toContain('rollback_storage_failure')
  })

  it('opens compensation from a conflict and allows retry after request failure', async () => {
    vi.mocked(api.rollbackCheck).mockResolvedValue(envelope({
      ...rollbackBase,
      can_rollback: false,
      compensation_recommended: true,
      conflict_count: 1,
      conflicts: [{ conflict_seq: 1, conflict_type: 'downstream_dependency', detail_code: 'dependency' }]
    }))
    const wrapper = await mountLoadedView()
    await runRollbackCheck(wrapper)
    await button(wrapper, '改用补偿批次').trigger('click')
    await nextTick()
    expect(wrapper.find('[data-testid="compensation-panel"]').exists()).toBe(true)

    vi.mocked(api.rollbackCheck).mockResolvedValue(envelope(rollbackBase))
    await button(wrapper, '执行回滚预检').trigger('click')
    await flushPromises()
    await acknowledgeRollback(wrapper)
    vi.mocked(api.requestRollback)
      .mockRejectedValueOnce(new Error('temporary request failure'))
      .mockResolvedValueOnce(envelope({
        import_id: summary.id,
        precheck_request_id: rollbackBase.precheck_request_id,
        job_id: '00000000-0000-7000-8000-000000000111',
        status: 'queued',
        replayed: false
      }))
    await button(wrapper, '二次确认回滚').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('temporary request failure')
    expect(wrapper.get('[data-testid="confirm-rollback"]').attributes('disabled')).toBeUndefined()
    await button(wrapper, '二次确认回滚').trigger('click')
    await flushPromises()
    expect(api.requestRollback).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('整批回滚任务已进入队列。')
  })
})
