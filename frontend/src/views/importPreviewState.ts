import type { ImportInspectResponse } from '../api'

export function clearPreviewRows(
  inspection: ImportInspectResponse | null
): ImportInspectResponse | null {
  return inspection?.preview_invalidated
    ? { ...inspection, preview_rows: [], preview_row_count: 0 }
    : inspection
}
