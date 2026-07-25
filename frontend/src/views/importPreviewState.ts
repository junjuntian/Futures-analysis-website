import type { ImportInspectResponse } from '../api'

export function clearPreviewRows(
  inspection: ImportInspectResponse | null
): ImportInspectResponse | null {
  return inspection
    ? { ...inspection, preview_rows: [], preview_row_count: 0 }
    : null
}
