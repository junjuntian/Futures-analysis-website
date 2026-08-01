<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { UploadUserFile } from 'element-plus'
import ImportCompensationPanel from '../components/ImportCompensationPanel.vue'
import ImportConfirmPanel from '../components/ImportConfirmPanel.vue'
import ImportRollbackPanel from '../components/ImportRollbackPanel.vue'
import {
  ApiError,
  confirmImport,
  createCompensation,
  getImportErrors,
  getImportLineage,
  getJson,
  getRollbackConflicts,
  normalizeImportJob,
  requestRollback,
  rollbackCheck,
  sendJson,
  streamImportEvents,
  uploadImport
} from '../api'
import type {
  ApiEnvelope,
  ImportConflictPolicy,
  ImportDatasetDefinition,
  ImportErrorItem,
  ImportInspectResponse,
  ImportJobEvent,
  ImportLineage,
  ImportMappingField,
  ImportMappingResponse,
  ImportRollbackCheck,
  ImportSummary,
  ImportTemplateSummary,
  ImportValidationSummary,
  NormalizedImportJob
} from '../api'
import { followImportProgress, terminalImportStatuses } from '../importProgress'
import { useAuthStore } from '../stores/auth'
import { clearPreviewRows } from './importPreviewState'

const auth = useAuthStore()
const files = ref<UploadUserFile[]>([])
const currentImport = ref<ImportSummary | null>(null)
const inspectResult = ref<ImportInspectResponse | null>(null)
const mappingFields = ref<ImportMappingField[]>([])
const errors = ref<ImportErrorItem[]>([])
const templates = ref<ImportTemplateSummary[]>([])
const datasets = ref<ImportDatasetDefinition[]>([])
const encoding = ref('')
const delimiter = ref('')
const selectedSheet = ref('')
const headerRow = ref(1)
const datasetType = ref('generic')
const templateName = ref('')
const selectedTemplateVersionId = ref<string | null>(null)
const validationSummary = ref<ImportValidationSummary | null>(null)
const selectedConflictPolicy = ref<ImportConflictPolicy | null>(null)
const errorNextCursor = ref<string | null>(null)
const jobProgress = ref<NormalizedImportJob | null>(null)
const lastEventSequence = ref<number | null>(null)
const sseDisconnected = ref(false)
const progressAccessEnded = ref(false)
const rollbackPrecheck = ref<ImportRollbackCheck | null>(null)
const rollbackResult = ref<string | null>(null)
const rollbackSubmitted = ref(false)
const lineage = ref<ImportLineage | null>(null)
const showCompensation = ref(false)
const message = ref('')
const loading = ref(false)
const idempotencyKeys = new Map<string, string>()
let progressController: AbortController | null = null

const columns = computed(() => inspectResult.value?.columns ?? [])
const datasetDefinition = computed(() => datasets.value.find((item) => item.dataset_type === datasetType.value))
const selectedTemplate = computed(() => templates.value.find((item) => item.latest_version_id === selectedTemplateVersionId.value))
const canRollback = computed(() => auth.me?.user.permissions.includes('import.rollback') ?? false)
const canCompensate = computed(() => auth.me?.user.permissions.includes('import.compensate') ?? false)
const progressPercentage = computed(() => {
  const progress = jobProgress.value?.progress
  return progress && progress.total_count > 0
    ? Math.min(100, Math.round(progress.processed_count / progress.total_count * 100))
    : 0
})
const previewStatistics = computed(() => ({
  rows: inspectResult.value?.total_rows ?? 0,
  preview: inspectResult.value?.preview_row_count ?? 0,
  rowErrors: inspectResult.value?.preview_rows.filter((row) => row.errors.length > 0).length ?? 0,
  rowWarnings: inspectResult.value?.preview_rows.filter((row) => row.warnings.length > 0).length ?? 0
}))
const frozenParameters = computed(() => [
  { label: '编码', value: inspectResult.value?.encoding.value ?? '自动识别' },
  { label: '分隔符', value: inspectResult.value?.delimiter.value ?? '自动识别' },
  { label: '工作表', value: inspectResult.value?.selected_sheet ?? '默认工作表' },
  { label: '表头行', value: String(inspectResult.value?.header_row ?? headerRow.value) },
  { label: '字段映射', value: mappingFields.value.map((field) =>
    `${field.source_column} → ${field.target_field}${field.transform ? ` (${field.transform})` : ''}`
  ).join('；') || '-' },
  { label: '校验版本', value: validationSummary.value?.validation_version ?? '-' }
])

async function ensureCsrf() {
  if (!auth.csrfToken) await auth.loadCsrf()
  return auth.csrfToken ?? ''
}

function errorMessage(error: unknown, fallback: string) {
  if (!(error instanceof ApiError)) return error instanceof Error ? error.message : fallback
  const labels: Record<string, string> = {
    rollback_precondition_stale: '回滚预检已过期，请重新预检后再确认。',
    rollback_conflict: '状态已变化并产生冲突，整批回滚未执行。',
    rollback_already_completed: '该批次已经回滚，未重复创建任务。',
    rollback_in_progress: '该批次已有回滚任务正在执行。',
    permission_denied: '当前会话没有执行此操作的权限。',
    auth_required: '会话已失效，请重新登录。',
    validation_stale: '预览或映射已变化，请重新校验。',
    idempotency_key_reused: '请求内容已变化，请重新发起操作。'
  }
  return labels[error.code] ?? `${fallback}（${error.code}）`
}

function operationKey(kind: string, importId: string) {
  const mapKey = `${kind}:${importId}`
  const existing = idempotencyKeys.get(mapKey)
  if (existing) return existing
  const value = `${kind}:${crypto.randomUUID()}`
  idempotencyKeys.set(mapKey, value)
  return value
}

function resetWorkflow() {
  inspectResult.value = null
  mappingFields.value = []
  errors.value = []
  validationSummary.value = null
  selectedConflictPolicy.value = null
  errorNextCursor.value = null
  jobProgress.value = null
  lastEventSequence.value = null
  rollbackPrecheck.value = null
  rollbackResult.value = null
  rollbackSubmitted.value = false
  lineage.value = null
  showCompensation.value = false
  stopProgress()
}

async function uploadSelected() {
  const raw = files.value[0]?.raw
  if (!raw) return void (message.value = '请选择一个导入文件。')
  loading.value = true
  try {
    const envelope = await uploadImport(raw, await ensureCsrf())
    currentImport.value = envelope.data
    resetWorkflow()
    await loadMetadata()
    message.value = '文件已上传并登记，可开始识别。'
  } catch (error) {
    message.value = errorMessage(error, '上传失败')
  } finally { loading.value = false }
}

async function inspectImport() {
  if (!currentImport.value) return
  loading.value = true
  try {
    const envelope = await sendJson<ApiEnvelope<ImportInspectResponse>>(
      `/api/v1/imports/${encodeURIComponent(currentImport.value.id)}/inspect`,
      { encoding: encoding.value || null, delimiter: delimiter.value || null,
        selected_sheet: selectedSheet.value || null, header_row: headerRow.value },
      await ensureCsrf()
    )
    inspectResult.value = clearPreviewRows(envelope.data)
    currentImport.value = { ...currentImport.value, status: envelope.data.status }
    selectedSheet.value = envelope.data.selected_sheet ?? ''
    if (envelope.data.preview_invalidated) invalidateDerivedState()
    if (!mappingFields.value.length) {
      const target = datasetDefinition.value?.fields[0]?.code ?? ''
      mappingFields.value = envelope.data.columns.map((column) => ({
        source_column: column.name, target_field: target, transform: null
      }))
    }
  } catch (error) { message.value = errorMessage(error, '识别失败') }
  finally { loading.value = false }
}

function invalidateDerivedState() {
  inspectResult.value = clearPreviewRows(inspectResult.value)
  errors.value = []
  errorNextCursor.value = null
  validationSummary.value = null
  selectedConflictPolicy.value = null
}

async function saveMapping() {
  if (!currentImport.value) return
  loading.value = true
  try {
    const envelope = await sendJson<ApiEnvelope<ImportMappingResponse>>(
      `/api/v1/imports/${encodeURIComponent(currentImport.value.id)}/mapping`,
      { dataset_type: datasetType.value, template_version_id: selectedTemplateVersionId.value,
        fields: mappingFields.value },
      await ensureCsrf(), 'PUT'
    )
    currentImport.value = { ...currentImport.value, status: envelope.data.status }
    if (envelope.data.preview_invalidated) invalidateDerivedState()
    message.value = envelope.data.preview_invalidated
      ? '映射已保存；旧预览和校验结果已失效，请重新生成。' : '映射已保存。'
  } catch (error) { message.value = errorMessage(error, '映射保存失败') }
  finally { loading.value = false }
}

async function previewImport() {
  if (!currentImport.value) return
  loading.value = true
  try {
    const envelope = await sendJson<ApiEnvelope<ImportInspectResponse>>(
      `/api/v1/imports/${encodeURIComponent(currentImport.value.id)}/preview`,
      { encoding: encoding.value || null, delimiter: delimiter.value || null,
        selected_sheet: selectedSheet.value || null, header_row: headerRow.value },
      await ensureCsrf()
    )
    inspectResult.value = envelope.data
    validationSummary.value = null
    selectedConflictPolicy.value = null
    await loadErrors(true)
  } catch (error) { message.value = errorMessage(error, '预览失败') }
  finally { loading.value = false }
}

async function validateImport() {
  if (!currentImport.value) return
  loading.value = true
  try {
    const envelope = await sendJson<ApiEnvelope<ImportValidationSummary>>(
      `/api/v1/imports/${encodeURIComponent(currentImport.value.id)}/validate`, {},
      await ensureCsrf()
    )
    validationSummary.value = envelope.data
    selectedConflictPolicy.value = envelope.data.allowed_conflict_policies[0] ?? null
    await loadErrors(true)
    message.value = envelope.data.blocking_error_count
      ? '校验完成，但存在阻断错误。' : '校验完成，请复核统计并二次确认。'
  } catch (error) { message.value = errorMessage(error, '校验失败') }
  finally { loading.value = false }
}

async function confirmCurrentImport() {
  if (!currentImport.value || !selectedConflictPolicy.value) return
  loading.value = true
  try {
    const envelope = await confirmImport(currentImport.value.id, selectedConflictPolicy.value,
      await ensureCsrf(), operationKey('confirm', currentImport.value.id))
    currentImport.value = { ...currentImport.value, status: envelope.data.status }
    jobProgress.value = {
      job_id: envelope.data.job_id, status: envelope.data.status, attempt_count: 0, max_attempts: 5,
      progress: { processed_count: 0, total_count: inspectResult.value?.total_rows ?? 0,
        imported_count: 0, overwritten_count: 0, skipped_count: 0, conflict_count: 0 }
    }
    message.value = envelope.data.replayed ? '已恢复同一确认任务。' : '导入任务已进入队列。'
    startProgress(envelope.data.import_id)
  } catch (error) { message.value = errorMessage(error, '确认导入失败') }
  finally { loading.value = false }
}

async function loadErrors(reset = false) {
  if (!currentImport.value) return
  const envelope = await getImportErrors(currentImport.value.id, reset ? null : errorNextCursor.value)
  errors.value = reset ? envelope.data.items : [...errors.value, ...envelope.data.items]
  errorNextCursor.value = envelope.data.next_cursor ?? null
}

function applyProgressEvent(event: ImportJobEvent) {
  lastEventSequence.value = event.event_seq
  jobProgress.value = {
    job_id: jobProgress.value?.job_id ?? '', status: event.status,
    attempt_count: jobProgress.value?.attempt_count ?? 0,
    max_attempts: jobProgress.value?.max_attempts ?? 5,
    progress: { processed_count: event.processed_rows, total_count: event.total_rows,
      imported_count: event.inserted_count, overwritten_count: event.updated_count,
      skipped_count: event.skipped_count, conflict_count: event.conflict_count },
    error_code: event.error_code
  }
  if (currentImport.value) currentImport.value = { ...currentImport.value, status: event.status }
  if (terminalImportStatuses.has(event.status)) void loadLineage()
}

function applyBatch(summary: ImportSummary) {
  currentImport.value = summary
  validationSummary.value = summary.validation ?? validationSummary.value
  jobProgress.value = normalizeImportJob(summary.job, inspectResult.value?.total_rows)
}

async function refreshSummary(importId: string) {
  const envelope = await getJson<ApiEnvelope<ImportSummary>>(`/api/v1/imports/${encodeURIComponent(importId)}`)
  return envelope.data
}

function startProgress(importId: string) {
  stopProgress()
  lastEventSequence.value = null
  sseDisconnected.value = false
  progressAccessEnded.value = false
  progressController = new AbortController()
  void followImportProgress(importId, null, jobProgress.value?.status ?? null, progressController.signal,
    { stream: streamImportEvents, refresh: refreshSummary },
    {
      onEvent: applyProgressEvent,
      onRefresh: applyBatch,
      onDisconnected: (value) => { sseDisconnected.value = value },
      onAccessEnded: () => {
        progressAccessEnded.value = true
        sseDisconnected.value = false
        message.value = '会话或导入读取权限已失效，实时跟踪已停止。'
      },
      onRetryError: () => { message.value = '实时连接中断，批次查询暂不可用，正在重试。' }
    })
}
function stopProgress() { progressController?.abort(); progressController = null }

async function runRollbackCheck() {
  if (!currentImport.value) return
  loading.value = true
  rollbackResult.value = null
  rollbackSubmitted.value = false
  try {
    const envelope = await rollbackCheck(currentImport.value.id, await ensureCsrf())
    rollbackPrecheck.value = envelope.data
  } catch (error) { message.value = errorMessage(error, '回滚预检失败') }
  finally { loading.value = false }
}

async function loadMoreRollbackConflicts() {
  if (!currentImport.value || !rollbackPrecheck.value?.next_cursor) return
  loading.value = true
  try {
    const envelope = await getRollbackConflicts(currentImport.value.id,
      rollbackPrecheck.value.precheck_request_id, rollbackPrecheck.value.next_cursor)
    rollbackPrecheck.value = { ...rollbackPrecheck.value,
      conflicts: [...rollbackPrecheck.value.conflicts, ...envelope.data.items],
      next_cursor: envelope.data.next_cursor }
  } catch (error) { message.value = errorMessage(error, '冲突分页加载失败') }
  finally { loading.value = false }
}

async function confirmRollback() {
  if (!currentImport.value || !rollbackPrecheck.value) return
  loading.value = true
  try {
    const envelope = await requestRollback(currentImport.value.id, rollbackPrecheck.value,
      await ensureCsrf(), operationKey('rollback', currentImport.value.id))
    rollbackResult.value = envelope.data.replayed ? '已恢复同一回滚任务。' : '整批回滚任务已进入队列。'
    rollbackSubmitted.value = true
    jobProgress.value = { job_id: envelope.data.job_id, status: envelope.data.status,
      attempt_count: 0, max_attempts: 5,
      progress: { processed_count: 0, total_count: rollbackPrecheck.value.affected_count,
        imported_count: 0, overwritten_count: 0, skipped_count: 0, conflict_count: 0 } }
    startProgress(currentImport.value.id)
  } catch (error) {
    if (error instanceof ApiError && error.code === 'rollback_conflict' && error.data) {
      rollbackPrecheck.value = error.data as ImportRollbackCheck
    }
    rollbackResult.value = errorMessage(error, '回滚请求失败')
  } finally { loading.value = false }
}

async function submitCompensation(file: File, reason: string) {
  if (!currentImport.value) return
  loading.value = true
  try {
    const envelope = await createCompensation(currentImport.value.id, file, reason,
      await ensureCsrf(), operationKey('compensate', currentImport.value.id))
    const newBatch = await refreshSummary(envelope.data.compensation_import_id)
    resetWorkflow()
    currentImport.value = newBatch
    await loadLineage()
    message.value = envelope.data.replayed ? '已恢复同一补偿批次。' : '补偿批次已创建，已切换到新批次继续识别、映射和校验。'
  } catch (error) { message.value = errorMessage(error, '补偿批次创建失败') }
  finally { loading.value = false }
}

async function loadLineage() {
  if (!currentImport.value) return
  try { lineage.value = (await getImportLineage(currentImport.value.id)).data }
  catch (error) { message.value = errorMessage(error, '批次追溯加载失败') }
}

async function loadMetadata() {
  const [datasetEnvelope, templateEnvelope] = await Promise.all([
    getJson<ApiEnvelope<{ items: ImportDatasetDefinition[] }>>('/api/v1/import-datasets'),
    getJson<ApiEnvelope<{ items: ImportTemplateSummary[] }>>('/api/v1/import-templates')
  ])
  datasets.value = datasetEnvelope.data.items
  templates.value = templateEnvelope.data.items
  if (!datasets.value.some((item) => item.dataset_type === datasetType.value)) {
    datasetType.value = datasets.value[0]?.dataset_type ?? ''
  }
}

function handleDatasetChange() {
  const fallback = datasetDefinition.value?.fields[0]?.code ?? ''
  mappingFields.value = mappingFields.value.map((field) => ({
    ...field,
    target_field: datasetDefinition.value?.fields.some((target) => target.code === field.target_field)
      ? field.target_field : fallback,
    transform: null
  }))
  invalidateDerivedState()
}
function handleTemplateChange(versionId: string | null) {
  if (!versionId || !selectedTemplate.value) return
  datasetType.value = selectedTemplate.value.dataset_type
  mappingFields.value = selectedTemplate.value.fields.map((field) => ({ ...field }))
  invalidateDerivedState()
}
function transformsFor(target: string) {
  return datasetDefinition.value?.fields.find((field) => field.code === target)?.transforms ?? []
}
async function createTemplate() {
  if (!templateName.value.trim() || !mappingFields.value.length) return
  loading.value = true
  try {
    await sendJson('/api/v1/import-templates', { dataset_type: datasetType.value,
      name: templateName.value.trim(), description: null, fields: mappingFields.value },
    await ensureCsrf())
    await loadMetadata()
    message.value = '映射模板已保存。'
  } catch (error) { message.value = errorMessage(error, '模板保存失败') }
  finally { loading.value = false }
}
function previewCellValue(row: { cells: Array<{ column: string; raw_value: string }> }, column: string) {
  return row.cells.find((cell) => cell.column === column)?.raw_value ?? ''
}
function previewCellNormalized(
  row: { cells: Array<{ column: string; normalized_value: string | null }> },
  column: string
) {
  return row.cells.find((cell) => cell.column === column)?.normalized_value ?? ''
}

onMounted(() => void loadMetadata().catch((error) => { message.value = errorMessage(error, '导入元数据加载失败') }))
onUnmounted(stopProgress)
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <p class="eyebrow">Phase 3D</p>
      <h1>导入中心</h1>
      <p>完成文件识别、映射、校验、异步导入，以及可审计的整批回滚与补偿。</p>
    </div>
    <el-alert v-if="message" :title="message" type="info" show-icon class="status-alert" />

    <div class="two-column">
      <el-card shadow="never">
        <template #header>1. 文件与批次</template>
        <el-upload v-model:file-list="files" :auto-upload="false" :limit="1">
          <el-button>选择 TXT / CSV / XLS / XLSX</el-button>
        </el-upload>
        <el-button type="primary" :loading="loading" class="details" @click="uploadSelected">上传</el-button>
        <el-descriptions v-if="currentImport" :column="1" border class="details">
          <el-descriptions-item label="状态">{{ currentImport.status }}</el-descriptions-item>
          <el-descriptions-item label="文件">{{ currentImport.file.original_filename }}</el-descriptions-item>
          <el-descriptions-item label="格式">{{ currentImport.file.detected_format }}</el-descriptions-item>
          <el-descriptions-item label="大小">{{ currentImport.file.size_bytes }} bytes</el-descriptions-item>
          <el-descriptions-item label="SHA-256">{{ currentImport.file.sha256 }}</el-descriptions-item>
        </el-descriptions>
      </el-card>
      <el-card shadow="never">
        <template #header>2. 识别参数</template>
        <el-form label-width="88px">
          <el-form-item label="编码"><el-select v-model="encoding" clearable placeholder="自动识别">
            <el-option label="UTF-8" value="utf-8" /><el-option label="GBK" value="gbk" />
          </el-select></el-form-item>
          <el-form-item label="分隔符"><el-select v-model="delimiter" clearable placeholder="自动识别">
            <el-option label="逗号" value="," /><el-option label="Tab" value="\t" />
            <el-option label="分号" value=";" /><el-option label="竖线" value="|" />
          </el-select></el-form-item>
          <el-form-item label="工作表"><el-select v-model="selectedSheet" clearable>
            <el-option v-for="sheet in inspectResult?.sheets ?? []" :key="sheet.name" :label="sheet.name" :value="sheet.name" />
          </el-select></el-form-item>
          <el-form-item label="表头行"><el-input-number v-model="headerRow" :min="1" :max="100" /></el-form-item>
        </el-form>
        <el-button :disabled="!currentImport" :loading="loading" @click="inspectImport">识别文件</el-button>
      </el-card>
    </div>

    <el-card v-if="columns.length" shadow="never" class="section-card">
      <template #header>3. 字段映射与模板</template>
      <el-table :data="mappingFields" border>
        <el-table-column prop="source_column" label="源字段" min-width="160" />
        <el-table-column label="目标字段" min-width="200"><template #default="{ row }">
          <el-select v-model="row.target_field" :disabled="Boolean(selectedTemplateVersionId)">
            <el-option v-for="target in datasetDefinition?.fields ?? []" :key="target.code"
              :label="`${target.label} (${target.code})`" :value="target.code" />
          </el-select>
        </template></el-table-column>
        <el-table-column label="转换" min-width="160"><template #default="{ row }">
          <el-select v-model="row.transform" clearable :disabled="Boolean(selectedTemplateVersionId)">
            <el-option v-for="transform in transformsFor(row.target_field)" :key="transform" :label="transform" :value="transform" />
          </el-select>
        </template></el-table-column>
      </el-table>
      <div class="toolbar">
        <el-select v-model="datasetType" class="inline-input" @change="handleDatasetChange">
          <el-option v-for="dataset in datasets" :key="dataset.dataset_type" :label="dataset.dataset_type" :value="dataset.dataset_type" />
        </el-select>
        <el-select v-model="selectedTemplateVersionId" clearable class="inline-input" placeholder="应用模板" @change="handleTemplateChange">
          <el-option v-for="template in templates.filter((item) => item.dataset_type === datasetType)"
            :key="template.latest_version_id" :label="`${template.name} (v${template.latest_version_number})`"
            :value="template.latest_version_id" />
        </el-select>
        <el-button :loading="loading" @click="saveMapping">保存映射</el-button>
        <el-input v-model="templateName" class="inline-input" placeholder="新模板名称" />
        <el-button :loading="loading" @click="createTemplate">另存模板</el-button>
        <el-button type="primary" :loading="loading" @click="previewImport">生成预览</el-button>
      </div>
    </el-card>

    <el-card v-if="inspectResult?.preview_rows.length" shadow="never" class="section-card">
      <template #header>4. 预览与统计</template>
      <el-descriptions :column="4" border>
        <el-descriptions-item label="总行数">{{ previewStatistics.rows }}</el-descriptions-item>
        <el-descriptions-item label="预览行">{{ previewStatistics.preview }}</el-descriptions-item>
        <el-descriptions-item label="含错误行">{{ previewStatistics.rowErrors }}</el-descriptions-item>
        <el-descriptions-item label="含警告行">{{ previewStatistics.rowWarnings }}</el-descriptions-item>
      </el-descriptions>
      <el-table :data="inspectResult.preview_rows" border height="360" class="details">
        <el-table-column prop="row_number" label="行号" width="80" />
        <el-table-column v-for="column in columns" :key="column.name" :label="column.name" min-width="150">
          <template #default="{ row }">
            <div>{{ previewCellValue(row, column.name) }}</div>
            <small v-if="previewCellNormalized(row, column.name)" class="normalized">
              标准化：{{ previewCellNormalized(row, column.name) }}
            </small>
          </template>
        </el-table-column>
        <el-table-column label="行问题" min-width="220">
          <template #default="{ row }">
            <div v-for="code in row.errors" :key="`error-${code}`" class="row-error">{{ code }}</div>
            <div v-for="code in row.warnings" :key="`warning-${code}`" class="row-warning">{{ code }}</div>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card v-if="inspectResult?.preview_rows.length || validationSummary" shadow="never" class="section-card">
      <template #header>5. 校验与冻结确认</template>
      <el-button :disabled="!inspectResult?.preview_rows.length" :loading="loading" @click="validateImport">执行校验</el-button>
      <el-descriptions v-if="validationSummary" :column="4" border class="details">
        <el-descriptions-item label="阻断错误">{{ validationSummary.blocking_error_count }}</el-descriptions-item>
        <el-descriptions-item label="警告">{{ validationSummary.warning_count }}</el-descriptions-item>
        <el-descriptions-item label="文件内重复">{{ validationSummary.duplicate_count }}</el-descriptions-item>
        <el-descriptions-item label="数据库冲突">{{ validationSummary.conflict_count }}</el-descriptions-item>
      </el-descriptions>
      <ImportConfirmPanel v-if="validationSummary" v-model="selectedConflictPolicy"
        :validation="validationSummary" :loading="loading" :frozen-parameters="frozenParameters"
        class="details" @confirm="confirmCurrentImport" />
    </el-card>

    <el-card v-if="jobProgress" shadow="never" class="section-card">
      <template #header>6. 异步任务进度</template>
      <el-alert v-if="progressAccessEnded" title="会话或权限已失效，跟踪已终止，不再自动重连。" type="error" :closable="false" />
      <el-alert v-else-if="sseDisconnected" title="实时连接中断，正在使用批次查询兜底并自动重连。" type="warning" :closable="false" />
      <el-progress :percentage="progressPercentage" :status="jobProgress.status === 'succeeded' || jobProgress.status === 'rolled_back' ? 'success' : undefined" />
      <el-descriptions :column="4" border class="details">
        <el-descriptions-item label="状态">{{ jobProgress.status }}</el-descriptions-item>
        <el-descriptions-item label="处理">{{ jobProgress.progress.processed_count }} / {{ jobProgress.progress.total_count }}</el-descriptions-item>
        <el-descriptions-item label="新增">{{ jobProgress.progress.imported_count }}</el-descriptions-item>
        <el-descriptions-item label="覆盖">{{ jobProgress.progress.overwritten_count }}</el-descriptions-item>
        <el-descriptions-item label="跳过">{{ jobProgress.progress.skipped_count }}</el-descriptions-item>
        <el-descriptions-item label="冲突">{{ jobProgress.progress.conflict_count }}</el-descriptions-item>
        <el-descriptions-item label="尝试">{{ jobProgress.attempt_count }} / {{ jobProgress.max_attempts }}</el-descriptions-item>
        <el-descriptions-item v-if="jobProgress.error_code" label="错误代码">{{ jobProgress.error_code }}</el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card v-if="currentImport" shadow="never" class="section-card">
      <template #header>7. 回滚、补偿与追溯</template>
      <div class="toolbar">
        <el-button v-if="canRollback" :loading="loading" @click="runRollbackCheck">执行回滚预检</el-button>
        <el-button v-if="canCompensate" @click="showCompensation = !showCompensation; loadLineage()">
          {{ showCompensation ? '收起补偿' : '创建补偿批次' }}
        </el-button>
        <el-button @click="loadLineage">刷新批次追溯</el-button>
      </div>
      <ImportRollbackPanel v-if="rollbackPrecheck" :check="rollbackPrecheck" :loading="loading"
        :submitted="rollbackSubmitted" :result="rollbackResult" class="details" @confirm="confirmRollback"
        @more="loadMoreRollbackConflicts" @compensate="showCompensation = true" />
      <ImportCompensationPanel v-if="showCompensation && canCompensate" :lineage="lineage"
        :loading="loading" class="details" @submit="submitCompensation" />
      <div v-if="lineage && !showCompensation" class="details">
        <h3>数据来源与审计链</h3>
        <el-table :data="lineage.nodes" border>
          <el-table-column prop="import_id" label="批次 ID" min-width="240" />
          <el-table-column prop="file.original_filename" label="来源文件" />
          <el-table-column prop="status" label="批次状态" />
          <el-table-column prop="rollback_capability" label="回滚能力" />
          <el-table-column prop="mapping_id" label="映射版本" min-width="220" />
          <el-table-column prop="confirmed_by" label="确认人" min-width="220" />
          <el-table-column prop="compensation_reason" label="补偿原因" />
        </el-table>
        <el-table :data="lineage.nodes.flatMap((node) => node.jobs.map((job) => ({ ...job, import_id: node.import_id })))"
          border class="details">
          <el-table-column prop="import_id" label="批次 ID" min-width="240" />
          <el-table-column prop="job_type" label="任务类型" />
          <el-table-column prop="status" label="任务状态" />
          <el-table-column prop="attempt_count" label="尝试次数" />
          <el-table-column prop="error_code" label="错误代码" />
        </el-table>
        <el-table :data="lineage.nodes.flatMap((node) => node.rollbacks.map((rollback) => ({ ...rollback, import_id: node.import_id })))"
          border class="details">
          <el-table-column prop="import_id" label="批次 ID" min-width="240" />
          <el-table-column prop="status" label="回滚状态" />
          <el-table-column prop="conflict_count" label="冲突数" />
          <el-table-column prop="requested_by" label="发起人" min-width="220" />
        </el-table>
        <el-table :data="lineage.audits" border class="details">
          <el-table-column prop="created_at" label="时间" />
          <el-table-column prop="event_type" label="审计事件" />
          <el-table-column prop="outcome" label="结果" />
        </el-table>
      </div>
    </el-card>

    <el-card v-if="errors.length || inspectResult?.warnings.length" shadow="never" class="section-card">
      <template #header>8. 错误与警告</template>
      <el-table :data="[...(inspectResult?.warnings ?? []), ...errors]" border>
        <el-table-column prop="severity" label="级别" width="100" />
        <el-table-column prop="row_number" label="行号" width="90" />
        <el-table-column prop="field_name" label="字段" width="150" />
        <el-table-column prop="error_code" label="稳定代码" width="190" />
        <el-table-column prop="message" label="说明" min-width="240" />
      </el-table>
      <el-button v-if="errorNextCursor" :loading="loading" class="details" @click="loadErrors(false)">加载更多</el-button>
    </el-card>
  </section>
</template>

<style scoped>
.two-column { display: grid; grid-template-columns: repeat(2, minmax(300px, 1fr)); gap: 16px; }
.section-card { margin-top: 16px; }
.details { margin-top: 16px; }
.toolbar { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 12px; }
.inline-input { width: 190px; }
.normalized { color: var(--el-text-color-secondary); }
.row-error { color: var(--el-color-danger); }
.row-warning { color: var(--el-color-warning); }
@media (max-width: 860px) { .two-column { grid-template-columns: 1fr; } }
</style>
