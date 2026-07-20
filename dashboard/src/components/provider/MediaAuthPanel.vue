<template>
  <section class="media-auth-panel">
    <div class="media-auth-head">
      <div>
        <div class="media-auth-eyebrow">NAS MEDIA OAUTH</div>
        <h2>生图账号登录器</h2>
        <p>Image2 与即梦在 NAS 内登录，凭证写入持久化卷，所有请求继承 AWS 代理隧道。</p>
      </div>
      <v-btn
        prepend-icon="mdi-refresh"
        variant="text"
        :loading="loading"
        @click="loadStatus"
      >
        刷新状态
      </v-btn>
    </div>

    <div class="media-auth-grid">
      <article v-for="provider in providers" :key="provider.provider" class="media-auth-card">
        <div class="media-auth-card__top">
          <div class="media-auth-icon">
            <v-icon>{{ provider.provider === 'codex' ? 'mdi-image-multiple-outline' : 'mdi-creation-outline' }}</v-icon>
          </div>
          <div class="media-auth-card__title">
            <strong>{{ provider.label }}</strong>
            <span>{{ provider.provider === 'codex' ? 'Image2 使用 Codex OAuth 订阅额度' : '即梦 Device Flow 与积分账户' }}</span>
          </div>
          <v-chip
            size="small"
            :color="provider.authenticated ? 'success' : 'warning'"
            variant="tonal"
          >
            {{ provider.authenticated ? '已登录' : '未登录' }}
          </v-chip>
        </div>

        <div class="media-auth-facts">
          <div><span>运行位置</span><strong>NAS</strong></div>
          <div><span>代理出口</span><strong>{{ provider.proxy?.route || '未配置' }}</strong></div>
          <div v-if="provider.expires_at"><span>Token 到期</span><strong>{{ formatTime(provider.expires_at) }}</strong></div>
          <div v-if="provider.credits"><span>剩余积分</span><strong>{{ provider.credits }}</strong></div>
        </div>

        <p class="media-auth-detail">{{ provider.detail }}</p>

        <div class="media-auth-actions">
          <v-btn
            color="primary"
            variant="tonal"
            :disabled="!provider.executable_available"
            :loading="busyProvider === provider.provider && busyAction === 'login'"
            @click="startLogin(provider.provider)"
          >
            {{ provider.authenticated ? '重新登录' : '登录' }}
          </v-btn>
          <v-btn
            variant="text"
            :loading="busyProvider === provider.provider && busyAction === 'test'"
            @click="testProvider(provider.provider)"
          >
            检测
          </v-btn>
          <v-btn
            v-if="provider.authenticated"
            color="error"
            variant="text"
            :loading="busyProvider === provider.provider && busyAction === 'logout'"
            @click="logoutProvider(provider.provider)"
          >
            退出
          </v-btn>
        </div>
      </article>
    </div>

    <v-dialog v-model="loginDialog" max-width="520" persistent>
      <v-card>
        <v-card-title class="text-h3 pa-4 pb-0 pl-6">完成设备授权</v-card-title>
        <v-card-text class="py-4">
          <p class="text-body-2 text-medium-emphasis mb-4">
            在浏览器打开授权页，输入一次性授权码。授权完成后，本页会自动更新 NAS 登录状态。
          </p>
          <v-text-field
            :model-value="loginFlow.verification_url"
            label="授权地址"
            readonly
            variant="solo-filled"
          />
          <v-text-field
            :model-value="loginFlow.user_code"
            label="一次性授权码"
            readonly
            variant="solo-filled"
            class="media-auth-code"
          />
          <v-alert :type="flowState === 'failed' ? 'error' : 'info'" variant="tonal">
            {{ flowMessage }}
          </v-alert>
        </v-card-text>
        <v-card-actions class="pa-4">
          <v-btn variant="text" @click="cancelFlow">关闭</v-btn>
          <v-spacer />
          <v-btn
            color="primary"
            variant="tonal"
            prepend-icon="mdi-open-in-new"
            @click="openAuthorizationPage"
          >
            打开授权页
          </v-btn>
          <v-btn
            color="primary"
            variant="tonal"
            :loading="checkingFlow"
            @click="checkLogin"
          >
            我已完成授权
          </v-btn>
        </v-card-actions>
      </v-card>
    </v-dialog>
  </section>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { mediaAuthApi } from '@/api/v1'

const emit = defineEmits(['message'])

const loading = ref(false)
const providers = ref([])
const busyProvider = ref('')
const busyAction = ref('')
const loginDialog = ref(false)
const loginFlow = ref({})
const flowState = ref('idle')
const flowDetail = ref('')
const checkingFlow = ref(false)
let pollTimer = null

const flowMessage = computed(() => {
  if (flowState.value === 'authenticated') return '授权成功，NAS 已保存登录状态。'
  if (flowState.value === 'failed') return flowDetail.value || '授权失败，请重新发起登录。'
  return flowDetail.value || '等待在浏览器完成授权。'
})

function notify(message, color = 'success') {
  emit('message', { message, color })
}

function formatTime(value) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

async function loadStatus() {
  loading.value = true
  try {
    const response = await mediaAuthApi.status()
    providers.value = response.data.data?.providers || []
  } catch (error) {
    notify(error.response?.data?.message || error.message || '读取媒体登录状态失败', 'error')
  } finally {
    loading.value = false
  }
}

async function startLogin(provider) {
  busyProvider.value = provider
  busyAction.value = 'login'
  stopPolling()
  try {
    const response = await mediaAuthApi.startLogin(provider)
    loginFlow.value = response.data.data || {}
    flowState.value = 'pending'
    flowDetail.value = '等待在浏览器完成授权。'
    loginDialog.value = true
    pollTimer = window.setInterval(checkLogin, 3000)
  } catch (error) {
    notify(error.response?.data?.message || error.message || '无法发起登录', 'error')
  } finally {
    busyProvider.value = ''
    busyAction.value = ''
  }
}

async function checkLogin() {
  if (checkingFlow.value || flowState.value !== 'pending') return
  checkingFlow.value = true
  try {
    const response = await mediaAuthApi.checkLogin(loginFlow.value.provider, {
      session_id: loginFlow.value.session_id || '',
      device_code: loginFlow.value.device_code || '',
      poll_seconds: 0
    })
    const result = response.data.data || {}
    flowState.value = result.state || 'pending'
    flowDetail.value = result.detail || ''
    if (flowState.value === 'authenticated') {
      stopPolling()
      await loadStatus()
      notify(`${result.label || '媒体账号'}登录成功`)
      window.setTimeout(() => { loginDialog.value = false }, 900)
    } else if (flowState.value === 'failed') {
      stopPolling()
    }
  } catch (error) {
    flowState.value = 'failed'
    flowDetail.value = error.response?.data?.message || error.message || '登录检测失败'
    stopPolling()
  } finally {
    checkingFlow.value = false
  }
}

async function testProvider(provider) {
  busyProvider.value = provider
  busyAction.value = 'test'
  try {
    const response = await mediaAuthApi.test(provider)
    const result = response.data.data || {}
    notify(result.authenticated ? `${result.label}登录有效` : result.detail || '登录无效', result.authenticated ? 'success' : 'error')
    await loadStatus()
  } catch (error) {
    notify(error.response?.data?.message || error.message || '检测失败', 'error')
  } finally {
    busyProvider.value = ''
    busyAction.value = ''
  }
}

async function logoutProvider(provider) {
  busyProvider.value = provider
  busyAction.value = 'logout'
  try {
    await mediaAuthApi.logout(provider)
    notify('NAS 登录状态已清除')
    await loadStatus()
  } catch (error) {
    notify(error.response?.data?.message || error.message || '退出登录失败', 'error')
  } finally {
    busyProvider.value = ''
    busyAction.value = ''
  }
}

function openAuthorizationPage() {
  if (loginFlow.value.verification_url) {
    window.open(loginFlow.value.verification_url, '_blank', 'noopener,noreferrer')
  }
}

function stopPolling() {
  if (pollTimer !== null) {
    window.clearInterval(pollTimer)
    pollTimer = null
  }
}

function cancelFlow() {
  stopPolling()
  loginDialog.value = false
}

onMounted(loadStatus)
onBeforeUnmount(stopPolling)
</script>

<style scoped>
.media-auth-panel {
  margin: 0 16px 20px;
  padding: 20px;
  border: 1px solid rgba(var(--v-theme-on-surface), 0.08);
  border-radius: 22px;
  background: rgb(var(--v-theme-surface));
}

.media-auth-head,
.media-auth-card__top,
.media-auth-actions {
  display: flex;
  align-items: center;
}

.media-auth-head {
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
}

.media-auth-eyebrow {
  color: rgb(var(--v-theme-primary));
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.16em;
}

.media-auth-head h2 {
  margin: 4px 0;
  font-size: 22px;
}

.media-auth-head p,
.media-auth-detail {
  margin: 0;
  color: rgba(var(--v-theme-on-surface), 0.62);
  font-size: 13px;
}

.media-auth-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 14px;
}

.media-auth-card {
  padding: 16px;
  border: 1px solid rgba(var(--v-theme-on-surface), 0.08);
  border-radius: 16px;
  background: rgba(var(--v-theme-on-surface), 0.018);
}

.media-auth-card__top {
  gap: 12px;
}

.media-auth-icon {
  display: grid;
  width: 40px;
  height: 40px;
  flex: 0 0 auto;
  place-items: center;
  border-radius: 12px;
  color: rgb(var(--v-theme-primary));
  background: rgba(var(--v-theme-primary), 0.1);
}

.media-auth-card__title {
  min-width: 0;
  flex: 1;
}

.media-auth-card__title strong,
.media-auth-card__title span {
  display: block;
}

.media-auth-card__title span {
  margin-top: 2px;
  color: rgba(var(--v-theme-on-surface), 0.55);
  font-size: 11px;
}

.media-auth-facts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin: 14px 0 10px;
}

.media-auth-facts div {
  padding: 9px 10px;
  border-radius: 10px;
  background: rgba(var(--v-theme-on-surface), 0.035);
}

.media-auth-facts span,
.media-auth-facts strong {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.media-auth-facts span {
  color: rgba(var(--v-theme-on-surface), 0.5);
  font-size: 10px;
}

.media-auth-facts strong {
  margin-top: 3px;
  font-size: 12px;
}

.media-auth-detail {
  min-height: 38px;
  line-height: 1.45;
}

.media-auth-actions {
  gap: 6px;
  margin-top: 12px;
}

.media-auth-code :deep(input) {
  font-size: 22px;
  font-weight: 800;
  letter-spacing: 0.12em;
}

@media (max-width: 900px) {
  .media-auth-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 600px) {
  .media-auth-panel {
    margin-inline: 0;
    padding: 14px;
  }

  .media-auth-head {
    align-items: flex-start;
  }
}
</style>
