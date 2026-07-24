<script setup lang="ts">
import { computed, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const auth = useAuthStore()
const router = useRouter()

const form = reactive({
  mode: 'login' as 'login' | 'bootstrap',
  username: '',
  password: '',
  bootstrapToken: ''
})

const title = computed(() => (form.mode === 'bootstrap' ? '首次初始化' : '登录'))

async function submit() {
  if (form.mode === 'bootstrap') {
    await auth.bootstrap(form.username, form.password, form.bootstrapToken)
  } else {
    await auth.login(form.username, form.password)
  }
  form.bootstrapToken = ''
  form.password = ''
  await router.push('/')
}
</script>

<template>
  <section class="auth-page">
    <el-card shadow="never" class="auth-panel">
      <template #header>
        <div class="auth-header">
          <span>{{ title }}</span>
          <el-segmented v-model="form.mode" :options="['login', 'bootstrap']" />
        </div>
      </template>
      <el-form label-position="top" @submit.prevent="submit">
        <el-form-item label="用户名">
          <el-input v-model="form.username" autocomplete="username" />
        </el-form-item>
        <el-form-item label="密码">
          <el-input v-model="form.password" type="password" autocomplete="current-password" show-password />
        </el-form-item>
        <el-form-item v-if="form.mode === 'bootstrap'" label="Bootstrap Token">
          <el-input v-model="form.bootstrapToken" type="password" autocomplete="off" show-password />
        </el-form-item>
        <el-alert
          v-if="auth.error"
          :title="auth.error"
          type="warning"
          show-icon
          :closable="false"
          class="status-alert"
        />
        <el-button type="primary" native-type="submit" :loading="auth.loading">提交</el-button>
      </el-form>
    </el-card>
  </section>
</template>
