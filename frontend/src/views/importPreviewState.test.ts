import { describe, expect, it } from 'vitest'
import type { ImportInspectResponse } from '../api'
import { clearPreviewRows } from './importPreviewState'

describe('clearPreviewRows', () => {
  it('removes an invalidated persisted preview while keeping fresh inspection metadata', () => {
    const inspection: ImportInspectResponse = {
      import_id: '00000000-0000-7000-8000-000000000001',
      status: 'preview_ready',
      detected_format: 'csv',
      encoding: { value: 'utf-8', confidence: 1, candidates: ['utf-8'], overridden: false },
      delimiter: { value: ',', confidence: 1, candidates: [','], overridden: false },
      sheets: [],
      selected_sheet: null,
      header_row: 1,
      columns: [{ index: 1, name: 'price' }],
      preview_rows: [{ row_number: 2, cells: [], errors: [], warnings: [] }],
      total_rows: 1,
      preview_row_count: 1,
      preview_invalidated: true,
      errors: [],
      warnings: []
    }

    expect(clearPreviewRows(inspection)).toMatchObject({
      columns: [{ index: 1, name: 'price' }],
      preview_rows: [],
      preview_row_count: 0
    })
  })
})
