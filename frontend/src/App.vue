<script setup lang="ts">
import { Cpu, HomeFilled } from '@element-plus/icons-vue'
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="232px" class="sidebar">
      <div class="brand">Futures Analysis</div>
      <el-menu router default-active="/">
        <el-menu-item index="/">
          <el-icon><HomeFilled /></el-icon>
          <span>首页</span>
        </el-menu-item>
        <el-menu-item index="/system">
          <el-icon><Cpu /></el-icon>
          <span>系统状态</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="topbar">期货与套利数据分析平台</el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>
