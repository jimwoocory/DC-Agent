<template>
  <div class="chat-analytics-page">
    <v-container fluid class="analytics-shell pa-4 pa-md-6">
      <div class="page-head">
        <div>
          <h1>问题消息 / 未回复看板</h1>
          <p>只看未正式回复、处理中未闭环、等材料或被通道拦截的消息；正常已接收/已完成的聊天不计入。</p>
        </div>
        <v-btn
          color="primary"
          variant="tonal"
          size="small"
          prepend-icon="mdi-refresh"
          :loading="loading"
          @click="refreshAll"
        >
          刷新
        </v-btn>
      </div>

      <section class="toolbar">
        <v-btn-toggle
          v-model="rangePreset"
          density="compact"
          divided
          mandatory
          variant="outlined"
          @update:model-value="handleRangeChange"
        >
          <v-btn v-for="preset in rangePresets" :key="preset" :value="preset" size="small">
            {{ presetLabels[preset] }}
          </v-btn>
        </v-btn-toggle>
        <div v-if="rangePreset === 'custom'" class="custom-range">
          <v-text-field
            v-model="customStartDate"
            type="date"
            density="compact"
            variant="outlined"
            hide-details
            label="开始日期"
            @update:model-value="handleRangeChange"
          />
          <v-text-field
            v-model="customEndDate"
            type="date"
            density="compact"
            variant="outlined"
            hide-details
            label="结束日期"
            @update:model-value="handleRangeChange"
          />
        </div>
        <span class="range-summary">{{ rangeSummary }}</span>
      </section>

      <v-alert
        v-if="latestMessageNotice"
        type="info"
        variant="tonal"
        class="mb-4"
      >
        {{ latestMessageNotice }}
      </v-alert>

      <v-alert
        v-if="errorMessage"
        type="warning"
        variant="tonal"
        class="mb-4"
      >
        {{ errorMessage }}
      </v-alert>

      <div class="metric-grid">
        <section v-for="card in metricCards" :key="card.label" class="metric-card">
          <v-icon size="18">{{ card.icon }}</v-icon>
          <span>{{ card.label }}</span>
          <strong>{{ card.value }}</strong>
          <small>{{ card.note }}</small>
        </section>
      </div>

      <div class="content-grid">
        <section class="panel message-panel">
          <div class="panel-head">
            <div>
              <h2>问题消息内容</h2>
              <p>按时间倒序展示需要处理的原文；已确认接收或已完成回复的正常聊天默认排除。</p>
            </div>
            <v-chip size="small" variant="tonal" color="primary">
              {{ messages.length }} / {{ summary?.messagesTotal || 0 }}
            </v-chip>
          </div>

          <div v-if="messages.length" class="message-list">
            <article v-for="message in messages" :key="message.id" class="message-row">
              <div class="message-meta">
                <v-chip
                  size="x-small"
                  :color="statusColor(message.status)"
                  variant="tonal"
                >
                  {{ statusLabel(message.status) }}
                </v-chip>
                <v-chip size="x-small" color="primary" variant="tonal">
                  {{ categoryLabel(message.category) }}
                </v-chip>
                <span>{{ message.sender_display_name || message.sender_name || message.platform_id }}</span>
                <span v-if="message.sender_department" class="meta-muted">
                  {{ message.sender_department }}{{ message.sender_role ? ` · ${message.sender_role}` : '' }}
                </span>
                <time>{{ formatDateTime(message.created_at) }}</time>
              </div>
              <p>{{ message.text || '（空内容）' }}</p>
              <div class="message-foot">
                <span v-if="message.case_id">Case {{ shortId(message.case_id) }}</span>
                <span>{{ message.is_group ? '群聊' : '私聊' }}</span>
                <span v-if="message.message_id">消息 {{ shortId(message.message_id) }}</span>
              </div>
            </article>
          </div>
          <div v-else class="empty-state">这个时间范围内没有问题消息。</div>
        </section>

        <aside class="side-stack">
          <section class="panel">
            <h2>问题来源同事</h2>
            <div v-if="summary?.topSenders.length" class="rank-list">
              <div v-for="item in summary.topSenders" :key="item.name" class="rank-row">
                <span>{{ item.name }}</span>
                <strong>{{ item.count }}</strong>
              </div>
            </div>
            <div v-else class="empty-state compact">暂无同事统计。</div>
          </section>

          <section class="panel">
            <h2>部门分布</h2>
            <div v-if="summary?.departments.length" class="rank-list">
              <div v-for="item in summary.departments" :key="item.name" class="rank-row">
                <span>{{ item.name }}</span>
                <strong>{{ item.count }}</strong>
              </div>
            </div>
            <div v-else class="empty-state compact">暂无部门统计。</div>
          </section>

          <section class="panel">
            <h2>处理状态</h2>
            <div v-if="summary?.statuses.length" class="rank-list">
              <div v-for="item in summary.statuses" :key="item.name" class="rank-row">
                <span>{{ statusLabel(item.name) }}</span>
                <strong>{{ item.count }}</strong>
              </div>
            </div>
            <div v-else class="empty-state compact">暂无状态统计。</div>
          </section>

          <section class="panel">
            <h2>请求类型</h2>
            <div v-if="summary?.categories.length" class="rank-list">
              <div v-for="item in summary.categories" :key="item.name" class="rank-row">
                <span>{{ categoryLabel(item.name) }}</span>
                <strong>{{ item.count }}</strong>
              </div>
            </div>
            <div v-else class="empty-state compact">暂无类型统计。</div>
          </section>

          <section class="panel">
            <h2>关键词</h2>
            <div v-if="summary?.keywords.length" class="keyword-list">
              <v-chip
                v-for="item in summary.keywords"
                :key="item.name"
                size="small"
                variant="tonal"
              >
                {{ item.name }} · {{ item.count }}
              </v-chip>
            </div>
            <div v-else class="empty-state compact">暂无关键词。</div>
          </section>

          <section class="panel">
            <h2>平台分布</h2>
            <div v-if="summary?.platforms.length" class="rank-list">
              <div v-for="item in summary.platforms" :key="item.name" class="rank-row">
                <span>{{ item.name }}</span>
                <strong>{{ item.count }}</strong>
              </div>
            </div>
            <div v-else class="empty-state compact">暂无平台统计。</div>
          </section>
        </aside>
      </div>
    </v-container>
  </div>
</template>

<script setup lang="ts">
import axios from 'axios'
import { computed, onMounted, ref } from 'vue'

type RangePreset = 'today' | 'yesterday' | 'week' | 'month' | 'custom'

interface CountRow {
  name: string
  count: number
}

interface Summary {
  latestMessageAt: string
  messagesTotal: number
  userMessages: number
  assistantMessages: number
  activeColleagues: number
  conversations: number
  avgUserLength: number
  openItems: number
  actionableItems: number
  inboxEvents: number
  chatHistoryMessages: number
  platforms: CountRow[]
  topSenders: CountRow[]
  departments: CountRow[]
  keywords: CountRow[]
  statuses: CountRow[]
  categories: CountRow[]
}

interface ChatMessage {
  id: string
  item_id: string
  created_at: string
  updated_at: string
  session_id: string
  conversation_id: string
  platform_id: string
  sender_id: string
  sender_name: string
  sender_display_name: string
  sender_department: string
  sender_role: string
  sender_relation_type: string
  sender_directory_matched: boolean
  role: string
  text: string
  category: string
  status: string
  case_id: string
  task_id: string
  source: string
  message_id: string
  is_group: boolean
  is_at_or_wake_command: boolean
}

const rangePresets: RangePreset[] = ['today', 'yesterday', 'week', 'month', 'custom']
const presetLabels: Record<RangePreset, string> = {
  today: '今天',
  yesterday: '昨天',
  week: '本周',
  month: '本月',
  custom: '自定义'
}

const loading = ref(false)
const errorMessage = ref('')
const rangePreset = ref<RangePreset>('today')
const customStartDate = ref(formatDateInput(startOfLocalDay(new Date())))
const customEndDate = ref(formatDateInput(startOfLocalDay(new Date())))
const summary = ref<Summary | null>(null)
const messages = ref<ChatMessage[]>([])

function startOfLocalDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate())
}

function startOfLocalWeek(date: Date): Date {
  const dayStart = startOfLocalDay(date)
  const mondayOffset = (dayStart.getDay() + 6) % 7
  dayStart.setDate(dayStart.getDate() - mondayOffset)
  return dayStart
}

function addDays(date: Date, days: number): Date {
  const next = new Date(date)
  next.setDate(next.getDate() + days)
  return next
}

function formatDateInput(date: Date): string {
  const year = date.getFullYear()
  const month = `${date.getMonth() + 1}`.padStart(2, '0')
  const day = `${date.getDate()}`.padStart(2, '0')
  return `${year}-${month}-${day}`
}

function parseDateInput(value: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null
  const [year, month, day] = value.split('-').map(Number)
  return new Date(year, month - 1, day)
}

function selectedRange(): { from: Date; to?: Date } {
  const now = new Date()
  const today = startOfLocalDay(now)
  if (rangePreset.value === 'yesterday') {
    const yesterday = addDays(today, -1)
    return { from: yesterday, to: today }
  }
  if (rangePreset.value === 'week') return { from: startOfLocalWeek(now) }
  if (rangePreset.value === 'month') return { from: new Date(now.getFullYear(), now.getMonth(), 1) }
  if (rangePreset.value === 'custom') {
    const start = parseDateInput(customStartDate.value) || today
    const end = parseDateInput(customEndDate.value) || start
    return { from: start, to: addDays(end < start ? start : end, 1) }
  }
  return { from: today }
}

function queryParams(): Record<string, string | number> {
  const range = selectedRange()
  return {
    from: range.from.toISOString(),
    ...(range.to ? { to: range.to.toISOString() } : {})
  }
}

function responseData<T>(response: { data: { status?: string; message?: string; data?: T } }): T {
  if (response.data.status === 'error') throw new Error(response.data.message || '加载失败')
  return response.data.data as T
}

async function refreshAll(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try {
    const params = queryParams()
    const [summaryResponse, messagesResponse] = await Promise.all([
      axios.get('/api/chat-analytics/summary', { params }),
      axios.get('/api/chat-analytics/messages', { params: { ...params, limit: 120 } })
    ])
    const summaryData = responseData<{
      latest_message_at: string
      metrics: {
        messages: number
        user_messages: number
        assistant_messages: number
        active_colleagues: number
        conversations: number
        avg_user_length: number
        open_items: number
        actionable_items: number
        inbox_events: number
        chat_history_messages: number
      }
      platforms: CountRow[]
      top_senders: CountRow[]
      departments: CountRow[]
      keywords: CountRow[]
      statuses: CountRow[]
      categories: CountRow[]
    }>(summaryResponse)
    const messageData = responseData<{ messages: ChatMessage[] }>(messagesResponse)
    summary.value = {
      latestMessageAt: summaryData.latest_message_at,
      messagesTotal: summaryData.metrics.messages,
      userMessages: summaryData.metrics.user_messages,
      assistantMessages: summaryData.metrics.assistant_messages,
      activeColleagues: summaryData.metrics.active_colleagues,
      conversations: summaryData.metrics.conversations,
      avgUserLength: summaryData.metrics.avg_user_length,
      openItems: summaryData.metrics.open_items,
      actionableItems: summaryData.metrics.actionable_items,
      inboxEvents: summaryData.metrics.inbox_events,
      chatHistoryMessages: summaryData.metrics.chat_history_messages,
      platforms: summaryData.platforms,
      topSenders: summaryData.top_senders,
      departments: summaryData.departments,
      keywords: summaryData.keywords,
      statuses: summaryData.statuses,
      categories: summaryData.categories
    }
    messages.value = messageData.messages
  } catch (error) {
    console.error('Failed to load chat analytics:', error)
    errorMessage.value = error instanceof Error ? error.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function handleRangeChange(): Promise<void> {
  await refreshAll()
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat('zh-CN').format(value || 0)
}

function formatDateTime(value: string): string {
  if (!value) return '未知'
  const normalized = value.includes('T') ? value : value.replace(' ', 'T')
  const date = new Date(normalized)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit'
  })
}

const rangeSummary = computed(() => {
  const range = selectedRange()
  return `${formatDateTime(range.from.toISOString())} 至 ${range.to ? formatDateTime(range.to.toISOString()) : '现在'}`
})

const latestMessageNotice = computed(() => {
  if (!summary.value?.latestMessageAt) return '请求库还没有问题消息记录。'
  if (summary.value.messagesTotal > 0) return ''
  return `当前范围没有问题消息。请求库最新一条问题记录是 ${formatDateTime(summary.value.latestMessageAt)}。`
})

const metricCards = computed(() => [
  {
    label: '问题消息',
    value: formatNumber(summary.value?.messagesTotal || 0),
    note: '未回复/卡住/拦截',
    icon: 'mdi-message-text-outline'
  },
  {
    label: '待处理问题',
    value: formatNumber(summary.value?.openItems || 0),
    note: '新请求/处理中/等材料',
    icon: 'mdi-account-voice'
  },
  {
    label: '正式聊天落库',
    value: formatNumber(summary.value?.chatHistoryMessages || 0),
    note: '参考项，不参与问题计数',
    icon: 'mdi-robot-outline'
  },
  {
    label: '活跃同事',
    value: formatNumber(summary.value?.activeColleagues || 0),
    note: `${formatNumber(summary.value?.conversations || 0)} 个会话`,
    icon: 'mdi-account-group-outline'
  },
  {
    label: '处理事件',
    value: formatNumber(summary.value?.inboxEvents || 0),
    note: `平均 ${summary.value?.avgUserLength || 0} 字`,
    icon: 'mdi-ruler-square'
  }
])

function statusLabel(status: string): string {
  const labels: Record<string, string> = {
    received: '已接收',
    new: '新请求',
    acknowledged: '已确认',
    waiting_materials: '等材料',
    in_progress: '处理中',
    delivered: '已交付',
    confirmed: '已确认完成',
    closed: '已关闭',
    ignored: '已忽略',
    'blocked:group_not_allowlisted': '群未放行',
    'blocked:group_mention_required': '群未唤醒',
    'blocked:dm_pairing_required': '私聊未配对',
    'blocked:dm_not_allowlisted': '私聊未放行',
    'blocked:forged_card_action': '卡片校验失败',
    'blocked:disabled': '通道关闭'
  }
  if (status?.startsWith('blocked:')) return '被通道拦截'
  return labels[status] || status || '未知'
}

function statusColor(status: string): string {
  if (['delivered', 'confirmed', 'closed'].includes(status)) return 'success'
  if (['waiting_materials', 'in_progress', 'acknowledged'].includes(status)) return 'warning'
  if (status === 'ignored' || status?.startsWith('blocked:')) return 'error'
  return 'primary'
}

function categoryLabel(category: string): string {
  const labels: Record<string, string> = {
    request: '请求',
    task: '任务',
    feedback: '反馈',
    material: '材料',
    escalation: '升级',
    question: '问题',
    other: '其他'
  }
  return labels[category] || category || '未分类'
}

function shortId(value: string): string {
  if (!value) return ''
  return value.length > 10 ? `${value.slice(0, 8)}...` : value
}

onMounted(() => {
  void refreshAll()
})
</script>

<style scoped>
.chat-analytics-page {
  min-height: 100%;
  background: rgb(var(--v-theme-background));
}

.analytics-shell {
  max-width: 1480px;
  margin: 0 auto;
}

.page-head,
.toolbar,
.custom-range,
.panel-head,
.message-meta,
.message-foot,
.rank-row {
  display: flex;
}

.page-head {
  align-items: flex-end;
  justify-content: space-between;
  gap: 18px;
  margin-bottom: 18px;
}

.page-head h1 {
  margin: 0;
  font-size: 1.55rem;
  line-height: 1.2;
}

.page-head p,
.panel-head p {
  margin: 6px 0 0;
  color: rgba(var(--v-theme-on-surface), 0.68);
  font-size: 13px;
}

.toolbar {
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.custom-range {
  gap: 8px;
  flex-wrap: wrap;
}

.custom-range :deep(.v-input) {
  width: 150px;
}

.range-summary {
  color: rgba(var(--v-theme-on-surface), 0.58);
  font-size: 12px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}

.metric-card,
.panel {
  border: 1px solid rgba(var(--v-theme-on-surface), 0.12);
  border-radius: 8px;
  background: rgb(var(--v-theme-surface));
}

.metric-card {
  display: grid;
  gap: 8px;
  padding: 16px;
}

.metric-card .v-icon {
  color: rgb(var(--v-theme-primary));
}

.metric-card span,
.metric-card small {
  color: rgba(var(--v-theme-on-surface), 0.64);
}

.metric-card strong {
  font-size: 1.7rem;
  line-height: 1.1;
}

.content-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.55fr) minmax(320px, 0.75fr);
  gap: 16px;
  align-items: start;
}

.panel {
  padding: 18px;
}

.panel h2 {
  margin: 0;
  font-size: 18px;
  line-height: 1.3;
}

.panel-head {
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.message-list,
.side-stack,
.rank-list,
.keyword-list {
  display: grid;
  gap: 10px;
}

.message-list {
  max-height: 720px;
  overflow: auto;
  padding-right: 4px;
}

.message-row {
  padding: 12px;
  border: 1px solid rgba(var(--v-theme-on-surface), 0.1);
  border-radius: 8px;
}

.message-meta {
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  color: rgba(var(--v-theme-on-surface), 0.58);
  font-size: 12px;
}

.meta-muted {
  color: rgba(var(--v-theme-on-surface), 0.48);
}

.message-row p {
  margin: 8px 0 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  line-height: 1.58;
  font-size: 13px;
}

.message-foot {
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 10px;
  color: rgba(var(--v-theme-on-surface), 0.48);
  font-size: 12px;
}

.rank-row {
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 8px;
  border-bottom: 1px solid rgba(var(--v-theme-on-surface), 0.1);
  font-size: 13px;
}

.keyword-list {
  grid-template-columns: repeat(auto-fit, minmax(90px, max-content));
}

.empty-state {
  padding: 42px 12px;
  color: rgba(var(--v-theme-on-surface), 0.58);
  text-align: center;
  font-size: 13px;
}

.empty-state.compact {
  padding: 18px 0;
}

@media (max-width: 1180px) {
  .metric-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .content-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 720px) {
  .page-head {
    align-items: stretch;
    flex-direction: column;
  }

  .metric-grid {
    grid-template-columns: 1fr;
  }
}
</style>
