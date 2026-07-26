import { afterEach, describe, expect, it, vi } from 'vitest'
import { confirmImport, streamImportEvents } from './api'
import type { ImportJobEvent } from './api'

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Phase 3C import API', () => {
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
})
