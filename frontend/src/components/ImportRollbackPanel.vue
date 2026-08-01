<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import type { ImportRollbackCheck } from '../api'

const props = defineProps<{
  check: ImportRollbackCheck
  loading: boolean
  submitted?: boolean
  result?: string | null
}>()
const emit = defineEmits<{ confirm: []; more: []; compensate: [] }>()
const acknowledged = ref(false)
watch(() => props.check.precheck_fingerprint, () => { acknowledged.value = false })
const rollbackDisabled = computed(() =>
  props.loading || props.submitted || !props.check.can_rollback ||
  props.check.affected_count === 0 || !acknowledged.value
)
</script>

<template>
  <div data-testid="rollback-panel">
    <el-alert
      v-if="check.affected_count === 0"
      title="预检未发现可回滚变更，不会创建空回滚任务。"
      type="info"
      :closable="false"
    />
    <el-alert
      v-else-if="!check.can_rollback"
      title="存在后续修改或依赖冲突，整批回滚已禁止；请创建补偿批次。"
      type="warning"
      :closable="false"
    />
    <el-descriptions :column="3" border>
      <el-descriptions-item label="受影响记录">{{ check.affected_count }}</el-descriptions-item>
      <el-descriptions-item label="冲突">{{ check.conflict_count }}</el-descriptions-item>
      <el-descriptions-item label="能力">{{ check.rollback_capability }}</el-descriptions-item>
    </el-descriptions>
    <el-table v-if="check.conflicts.length" :data="check.conflicts" border>
      <el-table-column prop="conflict_type" label="冲突类型" />
      <el-table-column prop="target_kind" label="对象类型" />
      <el-table-column prop="detail_code" label="稳定代码" />
    </el-table>
    <el-button v-if="check.next_cursor" :loading="loading" @click="emit('more')">加载更多冲突</el-button>
    <label v-if="check.can_rollback && check.affected_count > 0" class="acknowledgement">
      <input v-model="acknowledged" type="checkbox" data-testid="rollback-acknowledgement">
      我确认这是整批原子回滚；任一冲突都会中止全部变更。
    </label>
    <div class="actions">
      <el-button
        type="danger"
        :disabled="rollbackDisabled"
        :loading="loading"
        data-testid="confirm-rollback"
        @click="emit('confirm')"
      >
        二次确认回滚
      </el-button>
      <el-button v-if="check.compensation_recommended" @click="emit('compensate')">改用补偿批次</el-button>
    </div>
    <el-alert v-if="result" :title="result" type="info" :closable="false" />
  </div>
</template>

<style scoped>
[data-testid="rollback-panel"] { display: grid; gap: 12px; }
.acknowledgement { display: flex; gap: 8px; align-items: flex-start; }
.actions { display: flex; gap: 8px; }
</style>
