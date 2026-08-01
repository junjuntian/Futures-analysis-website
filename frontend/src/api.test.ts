import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, confirmImport, getJson, requestRollback, streamImportEvents } from './api'
import type { ImportJobEvent, ImportRollbackCheck } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Phase 3D import API', () => {
  it('sends the caller-owned idempotency key unchanged when confirming', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
      ok: true,
      json: async () => ({
        data: {
          import_id: 'batch-1',
          job_id: 'job-1',
          status: 'queued',
          conflict_policy: 'skip',
          replayed: false
        },
        meta: { request_id: 'request-1' }
      })
    }))
    vi.stubGlobal('fetch', fetchMock)

    await confirmImport('batch-1', 'skip', 'csrf-value', 'stable-key')

    expect(fetchMock).toHaveBeenCalledOnce()
    expect(fetchMock.mock.calls[0]?.[0]).toBe('/api/v1/imports/batch-1/confirm')
    expect(fetchMock.mock.calls[0]?.[1]?.headers).toMatchObject({
      'Idempotency-Key': 'stable-key',
      'x-csrf-token': 'csrf-value'
    })
  })

  it('uses Last-Event-ID and dispatches replayed SSE data', async () => {
    const expected: ImportJobEvent = {
      event_seq: 8,
      event_type: 'progress',
      status: 'running',
      processed_rows: 5,
      total_rows: 10,
      inserted_count: 4,
      updated_count: 0,
      skipped_count: 1,
      conflict_count: 0
    }
    const encoded = new TextEncoder().encode(`id: 8\ndata: ${JSON.stringify(expected)}\n\n`)
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
      ok: true,
      body: new ReadableStream<Uint8Array>({
        start(controller) {
          controller.enqueue(encoded)
          controller.close()
        }
      })
    }))
    vi.stubGlobal('fetch', fetchMock)
    const received: ImportJobEvent[] = []

    await streamImportEvents(
      'batch-1',
      7,
      (event) => received.push(event),
      new AbortController().signal
    )

    expect(fetchMock.mock.calls[0]?.[1]?.headers).toMatchObject({
      accept: 'text/event-stream',
      'Last-Event-ID': '7'
    })
    expect(received).toEqual([expected])
  })

  it('preserves stable API error codes and authorization status', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      data: { code: 'permission_denied', message: 'forbidden' },
      meta: { request_id: 'request-2' }
    }), { status: 403, headers: { 'content-type': 'application/json' } })))

    await expect(getJson('/api/v1/imports/batch')).rejects.toMatchObject({
      status: 403,
      code: 'permission_denied',
      requestId: 'request-2'
    })
  })

  it('returns a changed rollback precheck as structured conflict data', async () => {
    const changed: ImportRollbackCheck = {
      import_id: 'batch',
      precheck_request_id: 'new-check',
      precheck_fingerprint: 'b'.repeat(64),
      rollback_capability: 'direct',
      change_log_version: 2,
      can_rollback: false,
      compensation_recommended: true,
      affected_count: 2,
      conflict_count: 1,
      conflicts: [{ conflict_seq: 1, conflict_type: 'later_modification',
        detail_code: 'target_data_changed' }],
      next_cursor: null
    }
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
      data: changed,
      meta: { request_id: 'request-3' }
    }), { status: 409, headers: { 'content-type': 'application/json' } })))

    const original = { ...changed, precheck_request_id: 'old-check', can_rollback: true,
      conflict_count: 0, conflicts: [] }
    let caught: unknown
    try {
      await requestRollback('batch', original, 'csrf', 'stable-key')
    } catch (error) {
      caught = error
    }
    expect(caught).toBeInstanceOf(ApiError)
    expect(caught).toMatchObject({ code: 'rollback_conflict', data: changed })
  })
})
