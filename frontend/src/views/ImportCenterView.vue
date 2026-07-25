<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import type { UploadUserFile } from 'element-plus'
import { getJson, sendJson, uploadImport } from '../api'
import type {
  ApiEnvelope,
  ImportErrorItem,
  ImportDatasetDefinition,
  ImportInspectResponse,
  ImportMappingField,
  ImportMappingResponse,
  ImportPreviewRow,
  ImportSummary,
  ImportTemplateSummary
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
const message = ref('')
const loading = ref(false)

const columns = computed(() => inspectResult.value?.columns ?? [])
const mappingFields = ref<ImportMappingField[]>([])
const datasetDefinition = computed(() => datasets.value.find((dataset) => dataset.dataset_type === datasetType.value))
const selectedTemplate = computed(() => templates.value.find((template) => template.latest_version_id === selectedTemplateVersionId.value))

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
    errors.value = []
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
    await loadErrors()
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

async function loadErrors() {
  if (!currentImport.value) return
  const envelope = await getJson<ApiEnvelope<{ items: ImportErrorItem[] }>>(
    `/api/v1/imports/${currentImport.value.id}/errors`
  )
  errors.value = envelope.data.items
}

function previewCellValue(row: ImportPreviewRow, column: string) {
  return row.cells.find((cell) => cell.column === column)?.raw_value ?? ''
}

onMounted(() => {
  void loadMetadata().catch((error) => {
    message.value = error instanceof Error ? error.message : '无法加载导入元数据'
  })
})
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <p class="eyebrow">Phase 3B</p>
      <h1>导入中心</h1>
      <p>上传文件后完成格式识别、字段映射和前 50 行预览。</p>
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

@media (max-width: 860px) {
  .import-layout {
    grid-template-columns: 1fr;
  }
}
</style>
