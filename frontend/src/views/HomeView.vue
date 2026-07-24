<script setup lang="ts">
import { onMounted } from 'vue'
import { useHealthStore } from '../stores/health'

const health = useHealthStore()
onMounted(() => void health.refresh())
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <p class="eyebrow">Phase 1 Foundation</p>
      <h1>工程基础已就绪</h1>
      <p>当前页面只展示系统基础状态，不包含正式业务功能。</p>
    </div>

    <div class="status-grid">
      <el-card shadow="never">
        <template #header>API Live</template>
        <el-tag :type="health.live?.status === 'ok' ? 'success' : 'warning'">
          {{ health.live?.status ?? 'unknown' }}
        </el-tag>
      </el-card>
      <el-card shadow="never">
        <template #header>API Ready</template>
        <el-tag :type="health.ready?.status === 'ready' ? 'success' : 'warning'">
          {{ health.ready?.status ?? 'unknown' }}
        </el-tag>
      </el-card>
      <el-card shadow="never">
        <template #header>Version</template>
        <span>{{ health.version?.version ?? 'local' }}</span>
      </el-card>
    </div>

    <el-alert
      v-if="health.error"
      :title="health.error"
      type="warning"
      show-icon
      :closable="false"
      class="status-alert"
    />
  </section>
</template>
