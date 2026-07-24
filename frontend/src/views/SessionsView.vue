<script setup lang="ts">
import { onMounted } from 'vue'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()

onMounted(async () => {
  await auth.refresh()
  if (auth.me) {
    await auth.loadCsrf()
    await auth.loadSessions()
  }
})
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <p class="eyebrow">Identity</p>
      <h1>Session 管理</h1>
    </div>

    <el-table :data="auth.sessions" border>
      <el-table-column prop="created_at" label="创建时间" min-width="180" />
      <el-table-column prop="last_seen_at" label="最近活动" min-width="180" />
      <el-table-column prop="absolute_expires_at" label="绝对过期" min-width="180" />
      <el-table-column label="当前" width="90">
        <template #default="{ row }">
          <el-tag :type="row.current ? 'success' : 'info'">{{ row.current ? '是' : '否' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="120">
        <template #default="{ row }">
          <el-button size="small" :disabled="row.current" @click="auth.revokeSession(row.id)">撤销</el-button>
        </template>
      </el-table-column>
    </el-table>
  </section>
</template>
