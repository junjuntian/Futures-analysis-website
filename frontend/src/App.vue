<script setup lang="ts">
import { Cpu, HomeFilled, Key, SwitchButton, User } from '@element-plus/icons-vue'
import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
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
        <el-menu-item index="/auth">
          <el-icon><Key /></el-icon>
          <span>身份认证</span>
        </el-menu-item>
        <el-menu-item index="/sessions">
          <el-icon><User /></el-icon>
          <span>Session</span>
        </el-menu-item>
        <el-menu-item index="/system">
          <el-icon><Cpu /></el-icon>
          <span>系统状态</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="topbar">
        <span>期货与套利数据分析平台</span>
        <div class="topbar-actions">
          <span v-if="auth.me" class="identity-chip">{{ auth.me.user.username }}</span>
          <el-button v-if="auth.me" :icon="SwitchButton" text @click="auth.logout">退出</el-button>
        </div>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>
