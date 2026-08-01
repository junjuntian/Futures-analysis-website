<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ImportConflictPolicy, ImportValidationSummary } from '../api'

const props = defineProps<{
  validation: ImportValidationSummary
  modelValue: ImportConflictPolicy | null
  loading: boolean
  frozenParameters: Array<{ label: string; value: string }>
}>()
const emit = defineEmits<{
  'update:modelValue': [value: ImportConflictPolicy]
  confirm: []
}>()
const acknowledged = ref(false)
watch(() => props.validation.validation_version, () => { acknowledged.value = false })
const disabled = computed(() =>
  props.loading || props.validation.blocking_error_count > 0 || !props.modelValue || !acknowledged.value
)
const labels: Record<ImportConflictPolicy, string> = {
  skip: '跳过冲突记录',
  overwrite: '覆盖已有记录',
  keep_conflict: '保留冲突候选，不写入正式记录',
  abort: '发现冲突即终止'
}
</script>

<template>
  <div class="confirm-panel" data-testid="import-confirm-panel">
    <p>确认时将冻结当前映射、识别参数、校验版本和冲突策略。</p>
    <el-descriptions :column="2" border>
      <el-descriptions-item
        v-for="parameter in frozenParameters"
        :key="parameter.label"
        :label="parameter.label"
      >
        {{ parameter.value }}
      </el-descriptions-item>
    </el-descriptions>
    <el-radio-group
      :model-value="modelValue"
      @update:model-value="emit('update:modelValue', $event as ImportConflictPolicy)"
    >
      <el-radio v-for="policy in validation.allowed_conflict_policies" :key="policy" :value="policy">
        {{ labels[policy] }}
      </el-radio>
    </el-radio-group>
    <label class="acknowledgement">
      <input v-model="acknowledged" type="checkbox" data-testid="confirm-acknowledgement">
      我已复核预览统计与冻结参数，并确认执行导入。
    </label>
    <el-button
      type="primary"
      :disabled="disabled"
      :loading="loading"
      data-testid="confirm-import"
      @click="emit('confirm')"
    >
      二次确认并提交
    </el-button>
  </div>
</template>

<style scoped>
.confirm-panel { display: grid; gap: 12px; }
.acknowledgement { display: flex; gap: 8px; align-items: flex-start; }
</style>
