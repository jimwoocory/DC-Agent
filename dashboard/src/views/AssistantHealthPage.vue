<template>
  <div class="assistant-health-page">
    <v-container fluid class="health-shell pa-4 pa-md-6">
      <div class="page-head">
        <div>
          <h1>小助手聊天健康</h1>
          <p>后台线程、队列监听和会话存储的实时状态。</p>
        </div>
        <div class="head-actions">
          <v-select
            v-model="staleAfterSec"
            :items="staleOptions"
            item-title="label"
            item-value="value"
            density="compact"
            variant="outlined"
            hide-details
            class="threshold-select"
            @update:model-value="refreshHealth"
          />
          <v-btn
            color="primary"
            variant="tonal"
            size="small"
            prepend-icon="mdi-refresh"
            :loading="loading"
            @click="refreshHealth"
          >
            刷新
          </v-btn>
        </div>
      </div>

      <section class="status-band" :class="overallClass">
        <div class="status-main">
          <span class="status-dot" />
          <div>
            <strong>{{ overallLabel }}</strong>
            <span>{{ generatedAtLabel }}</span>
          </div>
        </div>
        <div class="status-meta">
          <span>HTTP {{ httpStatus || '-' }}</span>
          <span>{{ staleAfterSec / 60 }} 分钟阈值</span>
        </div>
      </section>

      <v-alert
        v-if="errorMessage"
        type="warning"
        variant="tonal"
        class="mb-4"
      >
        {{ errorMessage }}
      </v-alert>

      <div class="metric-grid">
        <section
          v-for="card in metricCards"
          :key="card.label"
          class="metric-panel"
        >
          <v-icon size="18">{{ card.icon }}</v-icon>
          <span>{{ card.label }}</span>
          <strong>{{ card.value }}</strong>
          <small>{{ card.note }}</small>
        </section>
      </div>

      <div class="content-grid">
        <section class="panel checks-panel">
          <div class="panel-head">
            <div>
              <h2>检查项</h2>
              <p>{{ checkSummary }}</p>
            </div>
            <v-chip
              size="small"
              :color="failedChecks.length ? 'warning' : 'success'"
              variant="tonal"
            >
              {{ failedChecks.length ? `${failedChecks.length} 异常` : '全部正常' }}
            </v-chip>
          </div>

          <div v-if="health?.checks?.length" class="checks-table">
            <div class="table-head">名称</div>
            <div class="table-head">状态</div>
            <div class="table-head">原因</div>
            <template v-for="check in health.checks" :key="check.name">
              <div class="table-cell">{{ checkName(check.name) }}</div>
              <div class="table-cell">
                <v-chip
                  size="x-small"
                  :color="check.status === 'ok' ? 'success' : 'warning'"
                  variant="tonal"
                >
                  {{ check.status === 'ok' ? '正常' : '异常' }}
                </v-chip>
              </div>
              <div class="table-cell muted">{{ reasonLabel(check.reason) }}</div>
            </template>
          </div>
          <div v-else class="empty-state">暂无检查数据。</div>
        </section>

        <aside class="side-stack">
          <section class="panel">
            <h2>今日采集</h2>
            <div class="kv-list">
              <div v-for="item in collectionRows" :key="item.label" class="kv-row">
                <span>{{ item.label }}</span>
                <strong>{{ item.value }}</strong>
              </div>
            </div>
          </section>

          <section class="panel">
            <h2>最近活动</h2>
            <div class="kv-list">
              <div v-for="item in activityRows" :key="item.label" class="kv-row">
                <span>{{ item.label }}</span>
                <strong>{{ item.value }}</strong>
              </div>
            </div>
          </section>

          <section class="panel">
            <h2>运行状态</h2>
            <div class="kv-list">
              <div v-for="item in runtimeRows" :key="item.label" class="kv-row">
                <span>{{ item.label }}</span>
                <strong>{{ item.value }}</strong>
              </div>
            </div>
          </section>

          <section class="panel">
            <h2>队列状态</h2>
            <div class="kv-list">
              <div v-for="item in queueRows" :key="item.label" class="kv-row">
                <span>{{ item.label }}</span>
                <strong>{{ item.value }}</strong>
              </div>
            </div>
          </section>
        </aside>
      </div>
    </v-container>
  </div>
</template>

<script setup lang="ts">
import axios from 'axios'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

interface HealthCheck {
  name: string
  status: string
  reason?: string
}

interface RuntimeHealth {
  active_runs: number
  active_conversations: number
  active_sessions: number
  active_threads: number
  active_platforms: number
  stale_runs: number
  stale_after_sec: number
  oldest_run_age_sec: number
  oldest_run_started_at: string | null
}

interface QueueHealth {
  queues: number
  back_queues: number
  pending_requests: number
  listener_registered: boolean
  listener_tasks: number
}

interface ActivityHealth {
  started_5m: number
  started_15m: number
  started_60m: number
  completed_15m: number
  failed_15m: number
  disconnected_15m: number
  last_event: string | null
  last_kind: string | null
  last_event_at: string | null
  last_started_at: string | null
  last_finished_at: string | null
  last_duration_sec: number
}

interface CollectionHealth {
  source: string
  timezone: string
  since_local: string
  since_at: string
  total_calls: number
  completed_calls: number
  failed_calls: number
  aborted_calls: number
  last_call_at: string | null
  last_status: string | null
}

interface AssistantHealth {
  status: string
  generated_at: string
  checks: HealthCheck[]
  runtime: RuntimeHealth
  activity: ActivityHealth
  collection: CollectionHealth
  queues: QueueHealth
}

const loading = ref(false)
const errorMessage = ref('')
const health = ref<AssistantHealth | null>(null)
const httpStatus = ref<number | null>(null)
const staleAfterSec = ref(900)
let refreshTimer: number | undefined

const staleOptions = [
  { label: '5 分钟', value: 300 },
  { label: '15 分钟', value: 900 },
  { label: '30 分钟', value: 1800 },
  { label: '60 分钟', value: 3600 }
]

const failedChecks = computed(() =>
  (health.value?.checks || []).filter((check) => check.status !== 'ok')
)

const overallClass = computed(() => {
  if (!health.value) return 'is-unknown'
  if (failedChecks.value.length) return 'is-warning'
  return 'is-ok'
})

const overallLabel = computed(() => {
  if (!health.value) return '等待检测'
  if (failedChecks.value.length) return '需要处理'
  return '后台健康'
})

const generatedAtLabel = computed(() => {
  if (!health.value?.generated_at) return '尚未刷新'
  return formatDateTime(health.value.generated_at)
})

const runtime = computed(() => health.value?.runtime)
const activity = computed(() => health.value?.activity)
const collection = computed(() => health.value?.collection)
const queues = computed(() => health.value?.queues)

const metricCards = computed(() => [
  {
    label: '今日命中',
    value: collection.value?.total_calls ?? 0,
    note: `完成 ${collection.value?.completed_calls ?? 0} / 异常 ${(collection.value?.failed_calls ?? 0) + (collection.value?.aborted_calls ?? 0)}`,
    icon: 'mdi-history'
  },
  {
    label: '运行请求',
    value: runtime.value?.active_runs ?? 0,
    note: `${runtime.value?.active_sessions ?? 0} 会话 / ${runtime.value?.active_threads ?? 0} 线程 / ${runtime.value?.active_platforms ?? 0} 平台`,
    icon: 'mdi-message-processing-outline'
  },
  {
    label: '卡住请求',
    value: runtime.value?.stale_runs ?? 0,
    note: `最久 ${formatDuration(runtime.value?.oldest_run_age_sec ?? 0)}`,
    icon: 'mdi-timer-alert-outline'
  },
  {
    label: '等待响应',
    value: queues.value?.pending_requests ?? 0,
    note: `${queues.value?.back_queues ?? 0} 回传队列`,
    icon: 'mdi-tray-arrow-down'
  },
  {
    label: '队列监听',
    value: queues.value?.listener_tasks ?? 0,
    note: queues.value?.listener_registered ? '监听已注册' : '未注册',
    icon: 'mdi-access-point'
  }
])

const collectionRows = computed(() => [
  {
    label: '采集起点',
    value: collection.value?.since_local
      ? formatDateTime(collection.value.since_local)
      : '-'
  },
  { label: '今日调用', value: collection.value?.total_calls ?? 0 },
  {
    label: '完成 / 失败 / 中止',
    value: `${collection.value?.completed_calls ?? 0} / ${collection.value?.failed_calls ?? 0} / ${collection.value?.aborted_calls ?? 0}`
  },
  {
    label: '最后调用',
    value: collection.value?.last_call_at
      ? formatDateTime(collection.value.last_call_at)
      : '-'
  },
  {
    label: '最后状态',
    value: statusLabel(collection.value?.last_status)
  }
])

const activityRows = computed(() => [
  { label: '最近 5 分钟', value: activity.value?.started_5m ?? 0 },
  { label: '最近 15 分钟', value: activity.value?.started_15m ?? 0 },
  { label: '最近 60 分钟', value: activity.value?.started_60m ?? 0 },
  {
    label: '完成 / 断开 / 失败',
    value: `${activity.value?.completed_15m ?? 0} / ${activity.value?.disconnected_15m ?? 0} / ${activity.value?.failed_15m ?? 0}`
  },
  {
    label: '最后命中',
    value: activity.value?.last_started_at
      ? formatDateTime(activity.value.last_started_at)
      : '-'
  },
  {
    label: '最后结束',
    value: activity.value?.last_finished_at
      ? formatDateTime(activity.value.last_finished_at)
      : '-'
  },
  {
    label: '最后耗时',
    value: formatDuration(activity.value?.last_duration_sec ?? 0)
  }
])

const runtimeRows = computed(() => [
  { label: '运行会话', value: runtime.value?.active_sessions ?? 0 },
  { label: '运行线程', value: runtime.value?.active_threads ?? 0 },
  { label: '平台运行', value: runtime.value?.active_platforms ?? 0 },
  { label: '运行上下文', value: runtime.value?.active_conversations ?? 0 },
  { label: '最长耗时', value: formatDuration(runtime.value?.oldest_run_age_sec ?? 0) },
  { label: '最早开始', value: runtime.value?.oldest_run_started_at ? formatDateTime(runtime.value.oldest_run_started_at) : '-' }
])

const queueRows = computed(() => [
  { label: '输入队列', value: queues.value?.queues ?? 0 },
  { label: '回传队列', value: queues.value?.back_queues ?? 0 },
  { label: '待响应请求', value: queues.value?.pending_requests ?? 0 },
  { label: '监听注册', value: queues.value?.listener_registered ? '是' : '否' },
  { label: '监听任务', value: queues.value?.listener_tasks ?? 0 }
])

const checkSummary = computed(() => {
  const total = health.value?.checks?.length || 0
  if (!total) return '等待健康接口返回检查结果。'
  return `${total - failedChecks.value.length} 正常 / ${failedChecks.value.length} 异常`
})

async function refreshHealth() {
  loading.value = true
  errorMessage.value = ''
  try {
    const response = await axios.get('/api/chat/health', {
      params: { stale_after_sec: staleAfterSec.value },
      validateStatus: (status) => status < 600
    })
    httpStatus.value = response.status
    const payload = response.data || {}
    health.value = payload.data || null
    if (!health.value) {
      errorMessage.value = payload.message || '健康接口没有返回数据。'
    } else if (response.status >= 500) {
      errorMessage.value = payload.message || '检测到小助手后台异常。'
    }
  } catch (error) {
    httpStatus.value = null
    health.value = null
    errorMessage.value = error instanceof Error ? error.message : '健康接口请求失败。'
  } finally {
    loading.value = false
  }
}

function checkName(name: string) {
  const labels: Record<string, string> = {
    chat_route: '聊天路由',
    conversation_manager: '会话管理器',
    chat_storage: '会话存储',
    webchat_queue_listener: '队列监听器',
    running_chat_threads: '运行线程'
  }
  return labels[name] || name
}

function reasonLabel(reason?: string) {
  const labels: Record<string, string> = {
    storage_unavailable: '存储不可用',
    listener_not_registered: '监听未注册',
    stale_running_chat: '运行超时'
  }
  return reason ? labels[reason] || reason : '-'
}

function statusLabel(status?: string | null) {
  const labels: Record<string, string> = {
    completed: '完成',
    error: '失败',
    aborted: '中止'
  }
  return status ? labels[status] || status : '-'
}

function formatDateTime(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  })
}

function formatDuration(seconds: number) {
  if (!seconds) return '0 秒'
  if (seconds < 60) return `${seconds} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟`
  const hours = Math.floor(minutes / 60)
  return `${hours} 小时 ${minutes % 60} 分钟`
}

onMounted(() => {
  refreshHealth()
  refreshTimer = window.setInterval(refreshHealth, 10000)
})

onBeforeUnmount(() => {
  if (refreshTimer) window.clearInterval(refreshTimer)
})
</script>

<style scoped>
.assistant-health-page {
  min-height: 100%;
  background: linear-gradient(180deg, rgba(248, 250, 252, 0.98), rgba(241, 245, 249, 0.82));
}

.health-shell {
  max-width: 1440px;
}

.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 18px;
}

.page-head h1 {
  margin: 0;
  font-size: 24px;
  font-weight: 780;
  color: #0f172a;
}

.page-head p {
  margin: 6px 0 0;
  color: #64748b;
  font-size: 13px;
}

.head-actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.threshold-select {
  width: 128px;
}

.status-band {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  border: 1px solid rgba(148, 163, 184, 0.28);
  border-radius: 8px;
  padding: 14px 16px;
  margin-bottom: 16px;
  background: rgba(255, 255, 255, 0.84);
  box-shadow: 0 12px 32px rgba(15, 23, 42, 0.08);
}

.status-main {
  display: flex;
  align-items: center;
  gap: 12px;
}

.status-main strong {
  display: block;
  font-size: 16px;
  color: #0f172a;
}

.status-main span:last-child,
.status-meta {
  color: #64748b;
  font-size: 12px;
}

.status-dot {
  width: 12px;
  height: 12px;
  border-radius: 50%;
  background: #94a3b8;
}

.status-band.is-ok .status-dot {
  background: #10b981;
}

.status-band.is-warning .status-dot {
  background: #f59e0b;
}

.status-meta {
  display: flex;
  align-items: center;
  gap: 10px;
  white-space: nowrap;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}

.metric-panel,
.panel {
  border: 1px solid rgba(148, 163, 184, 0.24);
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.9);
  box-shadow: 0 10px 24px rgba(15, 23, 42, 0.06);
}

.metric-panel {
  min-height: 122px;
  padding: 14px;
  display: grid;
  grid-template-rows: auto auto 1fr auto;
  gap: 6px;
}

.metric-panel .v-icon {
  color: #2563eb;
}

.metric-panel span,
.metric-panel small,
.muted {
  color: #64748b;
}

.metric-panel strong {
  align-self: end;
  font-size: 30px;
  line-height: 1;
  color: #0f172a;
}

.content-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(300px, 0.8fr);
  gap: 14px;
}

.panel {
  padding: 16px;
}

.panel h2 {
  margin: 0;
  font-size: 15px;
  color: #0f172a;
}

.panel-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.panel-head p {
  margin: 4px 0 0;
  color: #64748b;
  font-size: 12px;
}

.checks-table {
  display: grid;
  grid-template-columns: minmax(150px, 0.7fr) minmax(92px, 0.35fr) minmax(180px, 1fr);
  border: 1px solid rgba(148, 163, 184, 0.22);
  border-radius: 8px;
  overflow: hidden;
}

.table-head,
.table-cell {
  min-height: 42px;
  display: flex;
  align-items: center;
  padding: 9px 11px;
  border-top: 1px solid rgba(148, 163, 184, 0.16);
  font-size: 13px;
}

.table-head {
  min-height: 36px;
  border-top: 0;
  background: rgba(241, 245, 249, 0.86);
  color: #475569;
  font-weight: 720;
}

.side-stack {
  display: grid;
  gap: 14px;
  align-content: start;
}

.kv-list {
  margin-top: 10px;
}

.kv-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 10px 0;
  border-top: 1px solid rgba(148, 163, 184, 0.16);
  font-size: 13px;
}

.kv-row:first-child {
  border-top: 0;
}

.kv-row span {
  color: #64748b;
}

.kv-row strong {
  color: #0f172a;
  text-align: right;
}

.empty-state {
  min-height: 120px;
  display: grid;
  place-items: center;
  color: #64748b;
  border: 1px dashed rgba(148, 163, 184, 0.42);
  border-radius: 8px;
}

@media (max-width: 1100px) {
  .metric-grid,
  .content-grid {
    grid-template-columns: 1fr 1fr;
  }

  .checks-panel {
    grid-column: 1 / -1;
  }
}

@media (max-width: 760px) {
  .page-head,
  .status-band,
  .head-actions {
    flex-direction: column;
    align-items: stretch;
  }

  .threshold-select {
    width: 100%;
  }

  .metric-grid,
  .content-grid {
    grid-template-columns: 1fr;
  }

  .checks-table {
    grid-template-columns: minmax(110px, 0.7fr) minmax(78px, 0.4fr) minmax(120px, 0.9fr);
    overflow-x: auto;
  }
}
</style>
