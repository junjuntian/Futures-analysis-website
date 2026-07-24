<script setup lang="ts">
import { useHealthStore } from '../stores/health'

const health = useHealthStore()
</script>

<template>
  <section class="page">
    <div class="page-heading">
      <p class="eyebrow">System</p>
      <h1>系统状态</h1>
    </div>
    <el-button :loading="health.loading" type="primary" @click="health.refresh">刷新状态</el-button>
    <el-descriptions :column="1" border class="descriptions">
      <el-descriptions-item label="Live">{{ health.live?.status ?? 'unknown' }}</el-descriptions-item>
      <el-descriptions-item label="Ready">{{ health.ready?.status ?? 'unknown' }}</el-descriptions-item>
      <el-descriptions-item label="Version">{{ health.version?.version ?? 'unknown' }}</el-descriptions-item>
      <el-descriptions-item label="Git SHA">{{ health.version?.git_sha ?? 'unknown' }}</el-descriptions-item>
    </el-descriptions>
  </section>
</template>
