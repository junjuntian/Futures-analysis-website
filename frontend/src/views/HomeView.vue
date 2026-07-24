<script setup lang="ts">
import { onMounted } from 'vue'
import { useAuthStore } from '../stores/auth'
import { useHealthStore } from '../stores/health'

const health = useHealthStore()
const auth = useAuthStore()
onMounted(() => {
  void health.refresh()
  void auth.refresh()
})
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <p class="eyebrow">Phase 2 Foundation</p>
      <h1>身份与 Workspace 隔离基础</h1>
      <p>当前阶段只提供初始化、登录、个人 Workspace 和 Session 管理。</p>
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
      <el-card shadow="never">
        <template #header>当前用户</template>
        <span>{{ auth.me?.user.username ?? '未登录' }}</span>
      </el-card>
      <el-card shadow="never">
        <template #header>当前 Workspace</template>
        <span>{{ auth.workspace?.name ?? '未解析' }}</span>
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
