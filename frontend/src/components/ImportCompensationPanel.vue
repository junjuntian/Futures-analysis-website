<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ImportLineage } from '../api'

const props = defineProps<{ lineage: ImportLineage | null; loading: boolean }>()
const emit = defineEmits<{ submit: [file: File, reason: string] }>()
const file = ref<File | null>(null)
const reason = ref('')
const acknowledged = ref(false)
const disabled = computed(() =>
  props.loading || !file.value || !reason.value.trim() || !acknowledged.value
)
function selectFile(event: Event) {
  file.value = (event.target as HTMLInputElement).files?.[0] ?? null
}
function submit() {
  if (file.value && reason.value.trim()) emit('submit', file.value, reason.value.trim())
}
</script>

<template>
  <div class="compensation-panel" data-testid="compensation-panel">
    <p>补偿会创建新的可追溯批次，不删除或改写原始批次。</p>
    <input type="file" accept=".txt,.csv,.xls,.xlsx" data-testid="compensation-file" @change="selectFile">
    <el-input v-model="reason" type="textarea" :rows="3" maxlength="1000" show-word-limit placeholder="补偿原因（必填）" />
    <label class="acknowledgement">
      <input v-model="acknowledged" type="checkbox" data-testid="compensation-acknowledgement">
      我确认文件和原因正确，并保留完整审计关系。
    </label>
    <el-button
      type="primary"
      :disabled="disabled"
      :loading="loading"
      data-testid="submit-compensation"
      @click="submit"
    >
      创建补偿批次
    </el-button>
    <el-timeline v-if="lineage?.nodes.length">
      <el-timeline-item v-for="node in lineage.nodes" :key="node.import_id" :timestamp="node.created_at">
        {{ node.file.original_filename }} · {{ node.status }}
        <span v-if="node.compensation_reason"> · 补偿原因：{{ node.compensation_reason }}</span>
      </el-timeline-item>
    </el-timeline>
  </div>
</template>

<style scoped>
.compensation-panel { display: grid; gap: 12px; }
.acknowledgement { display: flex; gap: 8px; align-items: flex-start; }
</style>
