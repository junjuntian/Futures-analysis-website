<script setup lang="ts">
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import { useAuthStore } from './stores/auth'
import { themeMode, toggleTheme } from './theme'

const auth = useAuthStore()

// —— 账号功能 ——
//
// 原来这三样各占一个侧栏菜单项：身份认证（登录后进去会被守卫弹回首页，所以看着
// 和首页一模一样）、Session、系统状态。侧栏是每天要扫的东西，账号自服务一年碰
// 不了两次，不该占同等位置。改密码与登录设备收进这里，系统状态并进总览页。
const devicesOpen = ref(false)
const passwordOpen = ref(false)
const currentPassword = ref('')
const newPassword = ref('')
const confirmPassword = ref('')
const submitting = ref(false)

async function openDevices() {
  devicesOpen.value = true
  await auth.loadCsrf()
  await auth.loadSessions()
}

function openPassword() {
  currentPassword.value = ''
  newPassword.value = ''
  confirmPassword.value = ''
  passwordOpen.value = true
}

async function submitPassword() {
  if (newPassword.value !== confirmPassword.value) {
    ElMessage.warning('两次输入的新密码不一致')
    return
  }
  submitting.value = true
  try {
    const revoked = await auth.changePassword(currentPassword.value, newPassword.value)
    passwordOpen.value = false
    ElMessage.success(
      revoked > 0 ? `密码已改。其他 ${revoked} 台设备已被登出` : '密码已改'
    )
  } catch (error) {
    // 后端把「旧密码不对」和「新密码不合规」分开报，原样透出去：
    // 一句笼统的「修改失败」会让人反复试同一个错。
    const code = error instanceof Error ? error.message : ''
    if (code.includes('403')) ElMessage.error('当前密码不正确')
    else if (code.includes('400')) ElMessage.error('新密码不合要求：至少 15 个字符，且不能与当前密码相同')
    else ElMessage.error('修改失败，请重试')
  } finally {
    submitting.value = false
  }
}

function handleCommand(command: string) {
  if (command === 'password') openPassword()
  else if (command === 'devices') void openDevices()
  else if (command === 'logout') void auth.logout()
}
</script>

<template>
  <el-container class="app-shell">
    <el-aside width="232px" class="sidebar">
      <!-- 品牌标「趋势折线」(2026-08-16 运营者七选一拍板):深色方章 + 红色
           上行折线 + 金色终点。红=站内红涨语义,金点=机构那一步,全站唯一
           点缀金 #FFC94D。favicon 是它的 16px 加粗版(public/favicon.svg)。 -->
      <div class="brand">
        <svg class="brand-logo" viewBox="0 0 36 36" aria-hidden="true">
          <rect x="0.75" y="0.75" width="34.5" height="34.5" rx="7.5" fill="#131722" stroke="#363a45" stroke-width="1.5" />
          <path d="M7,26 L15,17 L19.5,21 L28.5,9" fill="none" stroke="#F23645" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round" />
          <circle cx="28.5" cy="9" r="3.4" fill="#FFC94D" />
        </svg>
        <span>期货机构资金</span>
      </div>
      <el-menu router :default-active="$route.path">
        <el-menu-item index="/">
          <span class="nav-emoji">🏠</span>
          <span>总览</span>
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
          <el-menu-item index="/seats/net-position">净持仓</el-menu-item>
        </el-sub-menu>
        <el-menu-item index="/smart-money-view">
          <span class="nav-emoji">💰</span>
          <span>机构资金</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="topbar">
        <span>期货与套利数据分析平台</span>
        <div class="topbar-actions">
          <button
            class="theme-toggle"
            :title="themeMode === 'dark' ? '切到浅色' : '切到深色'"
            @click="toggleTheme"
          >
            <!-- 太阳/月亮沿用 SSPanel 暗色开关的图标语言 -->
            <svg v-if="themeMode === 'dark'" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <circle cx="12" cy="12" r="4" />
              <path d="M12 2v2m0 16v2M4.9 4.9l1.4 1.4m11.4 11.4 1.4 1.4M2 12h2m16 0h2M4.9 19.1l1.4-1.4m11.4-11.4 1.4-1.4" />
            </svg>
            <svg v-else width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />
            </svg>
          </button>
          <el-dropdown v-if="auth.me" trigger="click" @command="handleCommand">
            <span class="account-trigger">
              <span class="account-avatar">{{ auth.me.user.username.slice(0, 1).toUpperCase() }}</span>
              <span class="account-name">{{ auth.me.user.username }}</span>
              <span class="account-caret">▾</span>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <div class="account-head">
                  <div class="account-head-name">{{ auth.me.user.username }}</div>
                  <div class="account-head-meta">
                    {{ auth.me.user.roles.join(' · ') || '无角色' }}
                    <template v-if="auth.workspace"> · {{ auth.workspace.name }}</template>
                  </div>
                </div>
                <el-dropdown-item command="password">修改密码</el-dropdown-item>
                <el-dropdown-item command="devices">登录设备</el-dropdown-item>
                <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>
      </el-header>
      <el-main>
        <!-- :key 绑主题:切主题强制重挂载视图,让 ECharts 带新配色重建(理由见 theme.ts) -->
        <router-view :key="themeMode" />
      </el-main>
    </el-container>
  </el-container>

  <el-dialog v-model="devicesOpen" title="登录设备" width="720px">
    <p class="dialog-note">
      每一行是一次登录留下的会话。撤销之后那台设备下次请求就会被要求重新登录；
      当前这台不能撤销——把自己踢下线没有意义。
    </p>
    <el-table :data="auth.sessions" border size="small">
      <el-table-column prop="created_at" label="登录时间" min-width="170" />
      <el-table-column prop="last_seen_at" label="最近活动" min-width="170" />
      <el-table-column prop="absolute_expires_at" label="绝对过期" min-width="170" />
      <el-table-column label="当前" width="80">
        <template #default="{ row }">
          <el-tag :type="row.current ? 'success' : 'info'" size="small">
            {{ row.current ? '是' : '否' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="90">
        <template #default="{ row }">
          <el-button size="small" :disabled="row.current" @click="auth.revokeSession(row.id)">
            撤销
          </el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-dialog>

  <el-dialog v-model="passwordOpen" title="修改密码" width="420px">
    <el-form label-position="top" @submit.prevent>
      <el-form-item label="当前密码">
        <el-input v-model="currentPassword" type="password" show-password autocomplete="current-password" />
      </el-form-item>
      <el-form-item label="新密码">
        <el-input v-model="newPassword" type="password" show-password autocomplete="new-password" />
      </el-form-item>
      <el-form-item label="再输一次新密码">
        <el-input v-model="confirmPassword" type="password" show-password autocomplete="new-password" />
      </el-form-item>
    </el-form>
    <p class="dialog-note">
      至少 15 个字符。改完之后，其他设备上的登录会全部失效，这一台不受影响。
    </p>
    <template #footer>
      <el-button @click="passwordOpen = false">取消</el-button>
      <el-button
        type="primary"
        :loading="submitting"
        :disabled="!currentPassword || !newPassword || !confirmPassword"
        @click="submitPassword"
      >
        确认修改
      </el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.account-trigger {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  outline: none;
  color: var(--el-text-color-regular);
}
.account-trigger:focus-visible {
  box-shadow: 0 0 0 2px var(--el-color-primary-light-5);
  border-radius: 4px;
}
.account-avatar {
  width: 24px;
  height: 24px;
  border-radius: 50%;
  background: var(--el-color-primary);
  color: #fff;
  display: grid;
  place-items: center;
  font-size: 12px;
  font-weight: 600;
  flex: none;
}
.account-name {
  font-size: 13px;
}
.account-caret {
  font-size: 10px;
  color: var(--el-text-color-placeholder);
}
.account-head {
  padding: 8px 16px 10px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  margin-bottom: 4px;
}
.account-head-name {
  font-weight: 600;
  font-size: 13px;
  color: var(--el-text-color-primary);
}
.account-head-meta {
  font-size: 11.5px;
  color: var(--el-text-color-secondary);
  margin-top: 2px;
}
.dialog-note {
  color: var(--el-text-color-secondary);
  font-size: 12.5px;
  line-height: 1.6;
  margin: 0 0 12px;
}
</style>
