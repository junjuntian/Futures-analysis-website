<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import type { UploadUserFile } from 'element-plus'
import {
  confirmImport,
  getImportErrors,
  getJson,
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
  ImportJobStatus,
  ImportJobSummary,
  ImportMappingField,
  ImportMappingResponse,
  ImportPreviewRow,
  ImportSummary,
  ImportTemplateSummary,
  ImportValidationSummary
} from '../api'
import { useAuthStore } from '../stores/auth'
import { clearPreviewRows } from './importPreviewState'

const auth = useAuthStore()
const files = ref<UploadUserFile[]>([])
const currentImport = ref<ImportSummary | null>(null)
const inspectResult = ref<ImportInspectResponse | null>(null)
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
const jobProgress = ref<ImportJobSummary | null>(null)
const lastEventSequence = ref<number | null>(null)
const sseDisconnected = ref(false)
const message = ref('')
const loading = ref(false)
const idempotencyKeys = new Map<string, string>()
let progressController: AbortController | null = null

const columns = computed(() => inspectResult.value?.columns ?? [])
const mappingFields = ref<ImportMappingField[]>([])
const datasetDefinition = computed(() => datasets.value.find((dataset) => dataset.dataset_type === datasetType.value))
const selectedTemplate = computed(() => templates.value.find((template) => template.latest_version_id === selectedTemplateVersionId.value))
const progressPercentage = computed(() => {
  const total = jobProgress.value?.total_rows ?? 0
  if (total <= 0) return 0
  return Math.min(100, Math.round(((jobProgress.value?.processed_rows ?? 0) / total) * 100))
})
const terminalJobStatuses = new Set<ImportJobStatus>(['succeeded', 'failed', 'dead_letter'])
const jobStatuses = new Set<ImportJobStatus>([
  'queued',
  'running',
  'progress',
  'succeeded',
  'failed',
  'dead_letter'
])
const conflictPolicyLabels: Record<ImportConflictPolicy, string> = {
  skip: '跳过冲突记录',
  overwrite: '覆盖已有记录',
  keep_conflict: '保留候选、正式表不新增同键记录',
  abort: '发现冲突即终止'
}

async function ensureCsrf() {
  if (!auth.csrfToken) {
    await auth.loadCsrf()
  }
  return auth.csrfToken ?? ''
}

async function uploadSelected() {
  const raw = files.value[0]?.raw
  if (!raw) {
    message.value = '请选择文件'
    return
  }
  loading.value = true
  message.value = ''
  try {
    const csrf = await ensureCsrf()
    const envelope = await uploadImport(raw, csrf)
    currentImport.value = envelope.data
    inspectResult.value = null
    mappingFields.value = []
    errors.value = []
    validationSummary.value = null
    selectedConflictPolicy.value = null
    errorNextCursor.value = null
    stopProgress()
    jobProgress.value = null
    lastEventSequence.value = null
    selectedTemplateVersionId.value = null
    await loadMetadata()
  } catch (error) {
    message.value = error instanceof Error ? error.message : '上传失败'
  } finally {
    loading.value = false
  }
}

async function inspectImport() {
  if (!currentImport.value) return
  loading.value = true
  message.value = ''
  try {
    const csrf = await ensureCsrf()
    const envelope = await sendJson<ApiEnvelope<ImportInspectResponse>>(
      `/api/v1/imports/${currentImport.value.id}/inspect`,
      {
        encoding: encoding.value || null,
        delimiter: delimiter.value || null,
        selected_sheet: selectedSheet.value || null,
        header_row: headerRow.value
      },
      csrf
    )
    inspectResult.value = clearPreviewRows(envelope.data)
    if (envelope.data.preview_invalidated) {
      errors.value = []
      errorNextCursor.value = null
      validationSummary.value = null
      selectedConflictPolicy.value = null
    }
    currentImport.value = { ...currentImport.value, status: envelope.data.status }
    selectedSheet.value = envelope.data.selected_sheet ?? ''
    if (mappingFields.value.length === 0) {
      mappingFields.value = envelope.data.columns.map((column) => ({
        source_column: column.name,
        target_field: datasetDefinition.value?.fields[0]?.code ?? '',
        transform: null
      }))
    }
  } catch (error) {
    message.value = error instanceof Error ? error.message : '识别失败'
  } finally {
    loading.value = false
  }
}

async function saveMapping() {
  if (!currentImport.value) return
  loading.value = true
  message.value = ''
  try {
    const csrf = await ensureCsrf()
    const envelope = await sendJson<ApiEnvelope<ImportMappingResponse>>(
      `/api/v1/imports/${currentImport.value.id}/mapping`,
      {
        dataset_type: datasetType.value,
        template_version_id: selectedTemplateVersionId.value,
        fields: mappingFields.value
      },
      csrf,
      'PUT'
    )
    currentImport.value = { ...currentImport.value, status: envelope.data.status }
    if (envelope.data.preview_invalidated) {
      inspectResult.value = clearPreviewRows(inspectResult.value)
      errors.value = []
      errorNextCursor.value = null
      validationSummary.value = null
      selectedConflictPolicy.value = null
      message.value = '映射已保存，旧预览已失效，请重新生成预览'
    } else {
      message.value = '映射已保存'
    }
  } catch (error) {
    message.value = error instanceof Error ? error.message : '映射保存失败'
  } finally {
    loading.value = false
  }
}

async function previewImport() {
  if (!currentImport.value) return
  loading.value = true
  message.value = ''
  try {
    const csrf = await ensureCsrf()
    const envelope = await sendJson<ApiEnvelope<ImportInspectResponse>>(
      `/api/v1/imports/${currentImport.value.id}/preview`,
      {
        encoding: encoding.value || null,
        delimiter: delimiter.value || null,
        selected_sheet: selectedSheet.value || null,
        header_row: headerRow.value
      },
      csrf
    )
    inspectResult.value = envelope.data
    validationSummary.value = null
    selectedConflictPolicy.value = null
    await loadErrors(true)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '预览失败'
  } finally {
    loading.value = false
  }
}

async function createTemplate() {
  if (mappingFields.value.length === 0 || !templateName.value.trim()) {
    message.value = '请填写模板名称并保存字段映射'
    return
  }
  loading.value = true
  message.value = ''
  try {
    const csrf = await ensureCsrf()
    const envelope = await sendJson<ApiEnvelope<{ id: string }>>(
      '/api/v1/import-templates',
      {
        dataset_type: datasetType.value,
        name: templateName.value.trim(),
        description: null,
        fields: mappingFields.value
      },
      csrf
    )
    await loadTemplates()
    selectedTemplateVersionId.value = envelope.data.id
    message.value = '模板已创建'
  } catch (error) {
    message.value = error instanceof Error ? error.message : '模板创建失败'
  } finally {
    loading.value = false
  }
}

async function loadTemplates() {
  const envelope = await getJson<ApiEnvelope<{ items: ImportTemplateSummary[] }>>('/api/v1/import-templates')
  templates.value = envelope.data.items
}

async function loadDatasets() {
  const envelope = await getJson<ApiEnvelope<{ items: ImportDatasetDefinition[] }>>('/api/v1/import-datasets')
  datasets.value = envelope.data.items
  if (!datasets.value.some((dataset) => dataset.dataset_type === datasetType.value)) {
    datasetType.value = datasets.value[0]?.dataset_type ?? ''
  }
}

async function loadMetadata() {
  await Promise.all([loadDatasets(), loadTemplates()])
}

function handleDatasetChange() {
  const fallbackTarget = datasetDefinition.value?.fields[0]?.code ?? ''
  mappingFields.value = mappingFields.value.map((field) => ({
    ...field,
    target_field: datasetDefinition.value?.fields.some((target) => target.code === field.target_field)
      ? field.target_field
      : fallbackTarget,
    transform: null
  }))
}

function handleTemplateChange(versionId: string | null) {
  if (!versionId) return
  const template = selectedTemplate.value
  if (!template) return
  datasetType.value = template.dataset_type
  mappingFields.value = template.fields.map((field) => ({ ...field }))
}

function transformsFor(targetField: string) {
  return datasetDefinition.value?.fields.find((target) => target.code === targetField)?.transforms ?? []
}

async function validateImport() {
  if (!currentImport.value) return
  loading.value = true
  message.value = ''
  try {
    const csrf = await ensureCsrf()
    const envelope = await sendJson<ApiEnvelope<ImportValidationSummary>>(
      `/api/v1/imports/${currentImport.value.id}/validate`,
      {},
      csrf
    )
    validationSummary.value = envelope.data
    selectedConflictPolicy.value = envelope.data.allowed_conflict_policies.includes(
      selectedConflictPolicy.value as ImportConflictPolicy
    )
      ? selectedConflictPolicy.value
      : (envelope.data.allowed_conflict_policies[0] ?? null)
    await loadErrors(true)
    message.value = envelope.data.blocking_error_count
      ? '校验完成：存在阻断错误，暂不能确认导入'
      : '校验完成，可以选择冲突策略并确认导入'
  } catch (error) {
    message.value = error instanceof Error ? error.message : '校验失败'
  } finally {
    loading.value = false
  }
}

function idempotencyKeyFor(importId: string) {
  const existing = idempotencyKeys.get(importId)
  if (existing) return existing
  const key = `import-confirm:${importId}:${crypto.randomUUID()}`
  idempotencyKeys.set(importId, key)
  return key
}

async function confirmCurrentImport() {
  if (!currentImport.value || !selectedConflictPolicy.value) return
  loading.value = true
  message.value = ''
  try {
    const csrf = await ensureCsrf()
    const envelope = await confirmImport(
      currentImport.value.id,
      selectedConflictPolicy.value,
      csrf,
      idempotencyKeyFor(currentImport.value.id)
    )
    currentImport.value = { ...currentImport.value, status: envelope.data.status }
    jobProgress.value = {
      job_id: envelope.data.job_id,
      status: envelope.data.status,
      processed_rows: 0,
      total_rows: inspectResult.value?.total_rows ?? 0,
      inserted_count: 0,
      updated_count: 0,
      skipped_count: 0,
      conflict_count: 0
    }
    message.value = envelope.data.replayed ? '已恢复同一确认任务' : '导入任务已进入队列'
    startProgress(envelope.data.import_id)
  } catch (error) {
    message.value = error instanceof Error ? error.message : '确认导入失败'
  } finally {
    loading.value = false
  }
}

async function loadErrors(reset = false) {
  if (!currentImport.value) return
  const envelope = await getImportErrors(
    currentImport.value.id,
    reset ? null : errorNextCursor.value
  )
  errors.value = reset ? envelope.data.items : [...errors.value, ...envelope.data.items]
  errorNextCursor.value = envelope.data.next_cursor ?? null
}

function applyProgressEvent(event: ImportJobEvent) {
  if (lastEventSequence.value !== null && event.event_seq <= lastEventSequence.value) return
  lastEventSequence.value = event.event_seq
  sseDisconnected.value = false
  jobProgress.value = {
    job_id: jobProgress.value?.job_id ?? '',
    status: event.status,
    processed_rows: event.processed_rows,
    total_rows: event.total_rows,
    inserted_count: event.inserted_count,
    updated_count: event.updated_count,
    skipped_count: event.skipped_count,
    conflict_count: event.conflict_count,
    error_code: event.error_code
  }
  if (currentImport.value) {
    currentImport.value = { ...currentImport.value, status: event.status }
  }
}

function applyBatchFallback(summary: ImportSummary) {
  currentImport.value = summary
  validationSummary.value =
    summary.validation_summary ?? summary.validation ?? validationSummary.value
  const nestedJob = summary.job
  const status = nestedJob?.status ?? summary.status
  if (nestedJob) {
    jobProgress.value = nestedJob
  } else if (summary.job_id && jobStatuses.has(status as ImportJobStatus)) {
    jobProgress.value = {
      job_id: summary.job_id,
      status: status as ImportJobStatus,
      processed_rows: summary.processed_rows ?? 0,
      total_rows: summary.total_rows ?? 0,
      inserted_count: summary.inserted_count ?? 0,
      updated_count: summary.updated_count ?? 0,
      skipped_count: summary.skipped_count ?? 0,
      conflict_count: summary.conflict_count ?? 0,
      error_code: summary.error_code
    }
  }
}

async function refreshImportStatus(importId: string) {
  const envelope = await getJson<ApiEnvelope<ImportSummary>>(
    `/api/v1/imports/${encodeURIComponent(importId)}`
  )
  applyBatchFallback(envelope.data)
}

function waitForReconnect(signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const timer = window.setTimeout(resolve, 3000)
    signal.addEventListener(
      'abort',
      () => {
        window.clearTimeout(timer)
        resolve()
      },
      { once: true }
    )
  })
}

async function followImportProgress(importId: string, signal: AbortSignal) {
  while (!signal.aborted) {
    try {
      await streamImportEvents(importId, lastEventSequence.value, applyProgressEvent, signal)
      if (jobProgress.value && terminalJobStatuses.has(jobProgress.value.status)) return
      sseDisconnected.value = true
    } catch (error) {
      if (signal.aborted) return
      sseDisconnected.value = true
    }

    try {
      await refreshImportStatus(importId)
      if (jobProgress.value && terminalJobStatuses.has(jobProgress.value.status)) return
    } catch {
      message.value = '进度连接中断，批次状态查询暂时不可用，正在重试'
    }
    await waitForReconnect(signal)
  }
}

function startProgress(importId: string) {
  stopProgress()
  lastEventSequence.value = null
  sseDisconnected.value = false
  progressController = new AbortController()
  void followImportProgress(importId, progressController.signal)
}

function stopProgress() {
  progressController?.abort()
  progressController = null
}

function previewCellValue(row: ImportPreviewRow, column: string) {
  return row.cells.find((cell) => cell.column === column)?.raw_value ?? ''
}

onMounted(() => {
  void loadMetadata().catch((error) => {
    message.value = error instanceof Error ? error.message : '无法加载导入元数据'
  })
})

onUnmounted(stopProgress)
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <p class="eyebrow">Phase 3C</p>
      <h1>导入中心</h1>
      <p>上传并预览文件，校验后选择冲突策略，确认导入并查看任务进度。</p>
    </div>

    <el-alert v-if="message" :title="message" type="info" show-icon class="status-alert" />

    <div class="import-layout">
      <el-card shadow="never">
        <template #header>文件上传</template>
        <el-upload v-model:file-list="files" :auto-upload="false" :limit="1">
          <el-button>选择 TXT / CSV / XLS / XLSX</el-button>
        </el-upload>
        <el-button type="primary" :loading="loading" class="action-button" @click="uploadSelected">
          上传
        </el-button>
        <el-descriptions v-if="currentImport" :column="1" border class="details">
          <el-descriptions-item label="批次">{{ currentImport.id }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ currentImport.status }}</el-descriptions-item>
          <el-descriptions-item label="文件">{{ currentImport.file.original_filename }}</el-descriptions-item>
          <el-descriptions-item label="格式">{{ currentImport.file.detected_format }}</el-descriptions-item>
        </el-descriptions>
      </el-card>

      <el-card shadow="never">
        <template #header>识别参数</template>
        <el-form label-width="96px">
          <el-form-item label="编码">
            <el-select v-model="encoding" clearable placeholder="自动识别">
              <el-option label="UTF-8" value="utf-8" />
              <el-option label="GBK" value="gbk" />
            </el-select>
          </el-form-item>
          <el-form-item label="分隔符">
            <el-select v-model="delimiter" clearable placeholder="自动识别">
              <el-option label="逗号" value="," />
              <el-option label="Tab" value="\t" />
              <el-option label="分号" value=";" />
              <el-option label="竖线" value="|" />
              <el-option label="空格" value=" " />
            </el-select>
          </el-form-item>
          <el-form-item label="工作表">
            <el-select v-model="selectedSheet" clearable placeholder="默认第一个工作表">
              <el-option v-for="sheet in inspectResult?.sheets ?? []" :key="sheet.name" :label="sheet.name" :value="sheet.name" />
            </el-select>
          </el-form-item>
          <el-form-item label="表头行">
            <el-input-number v-model="headerRow" :min="1" :max="100" />
          </el-form-item>
        </el-form>
        <el-button :disabled="!currentImport" :loading="loading" @click="inspectImport">识别</el-button>
      </el-card>
    </div>

    <el-card v-if="columns.length" shadow="never" class="section-card">
      <template #header>字段映射</template>
        <el-table :data="mappingFields" border>
        <el-table-column prop="source_column" label="源字段" min-width="160" />
          <el-table-column label="目标字段" min-width="180">
            <template #default="{ row }">
              <el-select v-model="row.target_field" :disabled="Boolean(selectedTemplateVersionId)">
                <el-option
                  v-for="target in datasetDefinition?.fields ?? []"
                  :key="target.code"
                  :label="`${target.label} (${target.code})`"
                  :value="target.code"
                />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column label="转换" min-width="160">
            <template #default="{ row }">
              <el-select v-model="row.transform" clearable :disabled="Boolean(selectedTemplateVersionId)">
                <el-option
                  v-for="transform in transformsFor(row.target_field)"
                  :key="transform"
                  :label="transform"
                  :value="transform"
                />
              </el-select>
            </template>
          </el-table-column>
        </el-table>
        <div class="toolbar">
          <el-select v-model="datasetType" class="inline-input" :disabled="Boolean(selectedTemplateVersionId)" @change="handleDatasetChange">
            <el-option
              v-for="dataset in datasets"
              :key="dataset.dataset_type"
              :label="dataset.dataset_type"
              :value="dataset.dataset_type"
            />
          </el-select>
          <el-select
            v-model="selectedTemplateVersionId"
            clearable
            class="inline-input"
            placeholder="应用映射模板"
            @change="handleTemplateChange"
          >
            <el-option
              v-for="template in templates.filter((item) => item.dataset_type === datasetType)"
              :key="template.latest_version_id"
              :label="`${template.name} (v${template.latest_version_number})`"
              :value="template.latest_version_id"
            />
          </el-select>
          <el-button :loading="loading" @click="saveMapping">保存映射</el-button>
        <el-input v-model="templateName" class="inline-input" placeholder="模板名称" />
        <el-button :loading="loading" @click="createTemplate">保存为模板</el-button>
        <el-button type="primary" :loading="loading" @click="previewImport">生成预览</el-button>
      </div>
    </el-card>

    <el-card v-if="inspectResult?.preview_rows.length" shadow="never" class="section-card">
      <template #header>前 50 行预览</template>
      <el-table :data="inspectResult.preview_rows" border height="420">
        <el-table-column prop="row_number" label="行号" width="80" />
        <el-table-column v-for="column in columns" :key="column.name" :label="column.name" min-width="160">
          <template #default="{ row }">
            <span>{{ previewCellValue(row, column.name) }}</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card v-if="inspectResult?.preview_rows.length || validationSummary" shadow="never" class="section-card">
      <template #header>校验与确认</template>
      <el-button :disabled="!inspectResult?.preview_rows.length" :loading="loading" @click="validateImport">
        校验
      </el-button>
      <el-descriptions v-if="validationSummary" :column="4" border class="details">
        <el-descriptions-item label="阻断错误">
          {{ validationSummary.blocking_error_count }}
        </el-descriptions-item>
        <el-descriptions-item label="警告">
          {{ validationSummary.warning_count }}
        </el-descriptions-item>
        <el-descriptions-item label="文件内重复">
          {{ validationSummary.duplicate_count }}
        </el-descriptions-item>
        <el-descriptions-item label="数据库冲突">
          {{ validationSummary.conflict_count }}
        </el-descriptions-item>
      </el-descriptions>
      <div v-if="validationSummary" class="confirm-panel">
        <p class="field-label">冲突策略（仅展示服务端允许的策略）</p>
        <el-radio-group v-model="selectedConflictPolicy">
          <el-radio
            v-for="policy in validationSummary.allowed_conflict_policies"
            :key="policy"
            :value="policy"
          >
            {{ conflictPolicyLabels[policy] }}
          </el-radio>
        </el-radio-group>
        <el-button
          type="primary"
          :disabled="validationSummary.blocking_error_count > 0 || !selectedConflictPolicy"
          :loading="loading"
          @click="confirmCurrentImport"
        >
          确认导入
        </el-button>
      </div>
    </el-card>

    <el-card v-if="jobProgress" shadow="never" class="section-card">
      <template #header>导入进度</template>
      <el-alert
        v-if="sseDisconnected"
        title="实时连接已中断，正在自动重连；当前状态由批次查询只读兜底。"
        type="warning"
        :closable="false"
        show-icon
      />
      <el-progress
        :percentage="progressPercentage"
        :status="jobProgress.status === 'succeeded' ? 'success' : undefined"
        class="details"
      />
      <el-descriptions :column="4" border class="details">
        <el-descriptions-item label="任务状态">{{ jobProgress.status }}</el-descriptions-item>
        <el-descriptions-item label="已处理">
          {{ jobProgress.processed_rows }} / {{ jobProgress.total_rows }}
        </el-descriptions-item>
        <el-descriptions-item label="新增">{{ jobProgress.inserted_count }}</el-descriptions-item>
        <el-descriptions-item label="覆盖">{{ jobProgress.updated_count }}</el-descriptions-item>
        <el-descriptions-item label="跳过">{{ jobProgress.skipped_count }}</el-descriptions-item>
        <el-descriptions-item label="冲突候选">{{ jobProgress.conflict_count }}</el-descriptions-item>
        <el-descriptions-item v-if="jobProgress.error_code" label="错误代码">
          {{ jobProgress.error_code }}
        </el-descriptions-item>
      </el-descriptions>
    </el-card>

    <el-card v-if="templates.length || errors.length || inspectResult?.warnings.length" shadow="never" class="section-card">
      <template #header>模板与错误</template>
      <el-table v-if="templates.length" :data="templates" border class="details">
        <el-table-column prop="name" label="模板" />
        <el-table-column prop="dataset_type" label="数据集" />
        <el-table-column prop="latest_version_number" label="版本" />
      </el-table>
      <el-table :data="[...(inspectResult?.warnings ?? []), ...errors]" border class="details">
        <el-table-column prop="severity" label="级别" width="100" />
        <el-table-column prop="row_number" label="行号" width="90" />
        <el-table-column prop="field_name" label="字段" width="160" />
        <el-table-column prop="error_code" label="代码" width="180" />
        <el-table-column prop="message" label="消息" min-width="240" />
      </el-table>
      <el-button
        v-if="errorNextCursor"
        :loading="loading"
        class="details"
        @click="loadErrors(false)"
      >
        加载更多错误
      </el-button>
    </el-card>
  </section>
</template>

<style scoped>
.import-layout {
  display: grid;
  grid-template-columns: minmax(280px, 1fr) minmax(320px, 1fr);
  gap: 16px;
}

.section-card {
  margin-top: 16px;
}

.action-button,
.details {
  margin-top: 16px;
}

.toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 16px;
}

.inline-input {
  width: 180px;
}

.confirm-panel {
  display: grid;
  gap: 12px;
  margin-top: 16px;
}

.field-label {
  margin: 0;
  color: var(--el-text-color-secondary);
}

@media (max-width: 860px) {
  .import-layout {
    grid-template-columns: 1fr;
  }
}
</style>
