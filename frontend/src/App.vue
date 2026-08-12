<script setup lang="ts">
import { SwitchButton } from '@element-plus/icons-vue'
import { useAuthStore } from './stores/auth'

const auth = useAuthStore()
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="232px" class="sidebar">
      <div class="brand">Futures Analysis</div>
      <el-menu router :default-active="$route.path">
        <el-menu-item index="/">
          <span class="nav-emoji">🏠</span>
          <span>首页</span>
        </el-menu-item>
        <el-menu-item index="/auth">
          <span class="nav-emoji">🔑</span>
          <span>身份认证</span>
        </el-menu-item>
        <el-menu-item index="/imports">
          <span class="nav-emoji">☁️</span>
          <span>导入中心</span>
        </el-menu-item>
        <el-sub-menu index="spread-analytics">
          <template #title>
            <span class="nav-emoji">🧮</span>
            <span>套利分析</span>
          </template>
          <el-menu-item index="/spread-analytics/monitor">套利监控</el-menu-item>
          <el-menu-item index="/spread-analytics/free-spread">自由价差</el-menu-item>
        </el-sub-menu>
        <el-sub-menu index="seats">
          <template #title>
            <span class="nav-emoji">📊</span>
            <span>席位</span>
          </template>
          <el-menu-item index="/seats?tab=positions">席位持仓</el-menu-item>
          <el-menu-item index="/seats?tab=building">建仓过程</el-menu-item>
        </el-sub-menu>
        <el-menu-item index="/smart-money-view">
          <span class="nav-emoji">💰</span>
          <span>机构资金</span>
        </el-menu-item>
        <el-menu-item index="/sessions">
          <span class="nav-emoji">👤</span>
          <span>Session</span>
        </el-menu-item>
        <el-menu-item index="/system">
          <span class="nav-emoji">⚙️</span>
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
