import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { describe, expect, it } from 'vitest'
import ImportCompensationPanel from './ImportCompensationPanel.vue'
import ImportConfirmPanel from './ImportConfirmPanel.vue'
import ImportRollbackPanel from './ImportRollbackPanel.vue'
import type { ImportLineage, ImportRollbackCheck, ImportValidationSummary } from '../api'

const plugins = [ElementPlus]

describe('import confirmation panel', () => {
  it('requires an explicit secondary confirmation', async () => {
    const validation: ImportValidationSummary = {
      import_id: 'batch',
      validation_version: '1',
      blocking_error_count: 0,
      warning_count: 1,
      duplicate_count: 0,
      conflict_count: 0,
      allowed_conflict_policies: ['skip']
    }
    const wrapper = mount(ImportConfirmPanel, {
      props: {
        validation,
        modelValue: 'skip',
        loading: false,
        frozenParameters: [{ label: '校验版本', value: '1' }]
      },
      global: { plugins }
    })
    const button = wrapper.get('[data-testid="confirm-import"]')
    expect(button.attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="confirm-acknowledgement"]').setValue(true)
    expect(button.attributes('disabled')).toBeUndefined()
    await button.trigger('click')
    expect(wrapper.emitted('confirm')).toHaveLength(1)
  })
})

describe('rollback confirmation panel', () => {
  const base: ImportRollbackCheck = {
    import_id: 'batch',
    precheck_request_id: 'check',
    precheck_fingerprint: 'a'.repeat(64),
    rollback_capability: 'direct',
    change_log_version: 1,
    can_rollback: true,
    compensation_recommended: false,
    affected_count: 3,
    conflict_count: 0,
    conflicts: [],
    next_cursor: null
  }

  it('requires confirmation before emitting a rollback', async () => {
    const wrapper = mount(ImportRollbackPanel, {
      props: { check: base, loading: false, submitted: false },
      global: { plugins }
    })
    const button = wrapper.get('[data-testid="confirm-rollback"]')
    expect(button.attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="rollback-acknowledgement"]').setValue(true)
    await button.trigger('click')
    expect(wrapper.emitted('confirm')).toHaveLength(1)
  })

  it('blocks conflict and zero-change prechecks', () => {
    const wrapper = mount(ImportRollbackPanel, {
      props: {
        check: {
          ...base,
          can_rollback: false,
          compensation_recommended: true,
          affected_count: 0,
          conflict_count: 1,
          conflicts: [{ conflict_seq: 1, conflict_type: 'later_modification',
            detail_code: 'target_data_changed' }]
        },
        loading: false,
        submitted: false
      },
      global: { plugins }
    })
    expect(wrapper.text()).toContain('不会创建空回滚任务')
    expect(wrapper.get('[data-testid="confirm-rollback"]').attributes('disabled')).toBeDefined()
  })
})

describe('compensation panel', () => {
  it('shows lineage and emits only after file, reason, and acknowledgement', async () => {
    const lineage: ImportLineage = {
      requested_import_id: 'compensation',
      root_import_id: 'original',
      nodes: [{
        import_id: 'original',
        status: 'succeeded',
        compensates_import_id: null,
        compensation_reason: null,
        created_by: 'user',
        confirmed_by: 'user',
        rollback_capability: 'direct',
        mapping_id: null,
        created_at: '2026-07-26T00:00:00Z',
        confirmed_at: null,
        rolled_back_at: null,
        file: {
          file_id: 'file', object_id: 'object', original_filename: 'original.csv',
          detected_format: 'csv', sha256: 'a'.repeat(64), size_bytes: 10, object_state: 'committed'
        },
        jobs: [],
        rollbacks: []
      }],
      audits: []
    }
    const wrapper = mount(ImportCompensationPanel, {
      props: { lineage, loading: false },
      global: { plugins }
    })
    expect(wrapper.text()).toContain('original.csv')
    const input = wrapper.get('[data-testid="compensation-file"]').element as HTMLInputElement
    const file = new File(['row'], 'fix.csv', { type: 'text/csv' })
    Object.defineProperty(input, 'files', { value: [file] })
    await wrapper.get('[data-testid="compensation-file"]').trigger('change')
    await wrapper.get('textarea').setValue('修正原批次错误数据')
    await wrapper.get('[data-testid="compensation-acknowledgement"]').setValue(true)
    await wrapper.get('[data-testid="submit-compensation"]').trigger('click')
    expect(wrapper.emitted('submit')?.[0]).toEqual([file, '修正原批次错误数据'])
  })
})
