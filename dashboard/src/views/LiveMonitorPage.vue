<template>
  <div class="live-monitor-page" :class="{ 'is-dark': isDark }">
    <v-container fluid class="live-monitor-shell pa-4 pa-md-6">
      <div class="monitor-header">
        <div>
          <h1 class="monitor-title">{{ t('header.title') }}</h1>
          <p class="monitor-subtitle">{{ t('header.subtitle') }}</p>
        </div>
        <div class="header-actions">
          <div class="status-pill" :class="connectionClass">
            <span class="status-dot" />
            <span>{{ connectionLabel }}</span>
          </div>
          <v-btn
            color="primary"
            variant="tonal"
            size="small"
            prepend-icon="mdi-refresh"
            :loading="loading"
            @click="refreshAll"
          >
            {{ t('actions.refresh') }}
          </v-btn>
        </div>
      </div>

      <v-alert
        v-if="errorMessage"
        type="warning"
        variant="tonal"
        class="mb-4"
      >
        {{ errorMessage }}
      </v-alert>

      <div v-if="loading && !pet" class="loading-wrap">
        <v-progress-circular indeterminate color="primary" />
      </div>

      <template v-else>
        <section class="hero-panel">
          <div class="pet-identity">
            <div class="pet-avatar">
              <v-icon size="42">mdi-cat</v-icon>
            </div>
            <div>
              <div class="pet-name">{{ pet?.name || t('pet.unknownName') }}</div>
              <div class="pet-meta">
                <span>{{ identityLabel }}</span>
                <span>{{ t('pet.level', { level: pet?.level ?? 0 }) }}</span>
              </div>
            </div>
          </div>
          <div class="pet-state">
            <div class="state-label">{{ stateLabel }}</div>
            <div class="state-meta">{{ emotionLabel }} · {{ sceneLabel }}</div>
          </div>
        </section>

        <div class="metric-grid">
          <section
            v-for="card in metricCards"
            :key="card.label"
            class="monitor-card metric-card"
          >
            <div class="metric-icon">
              <v-icon size="18">{{ card.icon }}</v-icon>
            </div>
            <div class="metric-label">{{ card.label }}</div>
            <div class="metric-value">{{ card.value }}</div>
            <div class="metric-note">{{ card.note }}</div>
          </section>
        </div>

        <div class="content-grid">
          <section class="monitor-card event-card">
            <div class="card-head">
              <div>
                <div class="section-title">{{ t('events.title') }}</div>
                <div class="section-subtitle">{{ t('events.subtitle') }}</div>
              </div>
              <v-chip size="small" variant="tonal" color="primary">
                {{ t('events.count', { count: events.length }) }}
              </v-chip>
            </div>
            <div class="event-filters">
              <v-btn-toggle
                v-model="rangePreset"
                density="compact"
                divided
                mandatory
                variant="outlined"
                @update:model-value="handleRangePresetChange"
              >
                <v-btn
                  v-for="preset in rangePresets"
                  :key="preset"
                  :value="preset"
                  size="small"
                >
                  {{ t(`filters.presets.${preset}`) }}
                </v-btn>
              </v-btn-toggle>
              <div v-if="rangePreset === 'custom'" class="custom-range">
                <v-text-field
                  v-model="customStartDate"
                  type="date"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :label="t('filters.startDate')"
                  @update:model-value="handleCustomRangeChange"
                />
                <v-text-field
                  v-model="customEndDate"
                  type="date"
                  density="compact"
                  variant="outlined"
                  hide-details
                  :label="t('filters.endDate')"
                  @update:model-value="handleCustomRangeChange"
                />
              </div>
              <span class="range-summary">{{ rangeSummary }}</span>
            </div>
            <div v-if="events.length" class="event-list">
              <article
                v-for="event in recentEvents"
                :key="event.id"
                class="event-row"
              >
                <div class="event-icon" :class="`source-${event.source || 'unknown'}`">
                  <v-icon size="16">{{ eventIcon(event) }}</v-icon>
                </div>
                <div class="event-body">
                  <div class="event-title-row">
                    <strong>{{ eventTypeLabel(event.event_type) }}</strong>
                    <span>{{ formatDateTime(event.created_at) }}</span>
                  </div>
                  <div class="event-detail">{{ eventDetail(event) }}</div>
                </div>
              </article>
            </div>
            <div v-else class="empty-state">{{ t('events.empty') }}</div>
          </section>

          <section class="monitor-card health-card">
            <div class="card-head compact">
              <div>
                <div class="section-title">{{ t('health.title') }}</div>
                <div class="section-subtitle">{{ t('health.subtitle') }}</div>
              </div>
            </div>

            <div class="health-list">
              <div
                v-for="item in healthItems"
                :key="item.label"
                class="health-row"
              >
                <span>{{ item.label }}</span>
                <strong>{{ item.value }}</strong>
              </div>
            </div>

            <div class="source-breakdown">
              <div class="section-title small">{{ t('sources.title') }}</div>
              <div v-if="sourceBreakdown.length" class="source-list">
                <div
                  v-for="source in sourceBreakdown"
                  :key="source.name"
                  class="source-row"
                >
                  <span>{{ sourceLabel(source.name) }}</span>
                  <div class="source-meter">
                    <span :style="{ width: `${source.percent}%` }" />
                  </div>
                  <strong>{{ source.count }}</strong>
                </div>
              </div>
              <div v-else class="empty-state compact">{{ t('sources.empty') }}</div>
            </div>
          </section>
        </div>
      </template>
    </v-container>
  </div>
</template>

<script setup lang="ts">
import axios from 'axios'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useTheme } from 'vuetify'
import { useI18n, useModuleI18n } from '@/i18n/composables'

interface PetState {
  pet_id: string
  user_id: string
  name: string
  species: string
  asset_id: string
  state: string
  emotion: string
  scene: string
  level: number
  xp: number
  coins: number
  energy: number
  streak_days: number
  last_event_id: number
  last_active_at: string
  updated_at: string
}

interface PetIdentity {
  pet_id: string
  feishu_open_id: string
  employee_id: string
}

interface PetEvent {
  id: number
  pet_id: string
  user_id: string
  source: string
  event_type: string
  source_ref: Record<string, string>
  payload: Record<string, unknown>
  state_after: Partial<PetState>
  created_at: string
}

type ConnectionState = 'connected' | 'system' | 'degraded' | 'disconnected'
type RangePreset = 'today' | 'yesterday' | 'week' | 'month' | 'custom'

const { locale } = useI18n()
const { tm: t } = useModuleI18n('features/live-monitor')
const theme = useTheme()

const loading = ref(true)
const errorMessage = ref('')
const pet = ref<PetState | null>(null)
const identity = ref<PetIdentity | null>(null)
const events = ref<PetEvent[]>([])
const lastEventId = ref(0)
const lastUpdatedAt = ref<Date | null>(null)
const connectionState = ref<ConnectionState>('disconnected')
const rangePresets: RangePreset[] = ['today', 'yesterday', 'week', 'month', 'custom']
const eventPageSize = 300
const maxCachedEvents = 5000
const rangePreset = ref<RangePreset>('today')
const customStartDate = ref(formatDateInput(startOfLocalDay(new Date())))
const customEndDate = ref(formatDateInput(startOfLocalDay(new Date())))

let pollTimer: number | null = null
let heartbeatTimer: number | null = null

const isDark = computed(() => theme.global.current.value.dark)

function responseData<T>(response: { data: { status?: string; message?: string; data?: T } }): T {
  if (response.data.status === 'error') {
    throw new Error(response.data.message || t('errors.loadFailed'))
  }
  return response.data.data as T
}

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
  if (rangePreset.value === 'week') {
    return { from: startOfLocalWeek(now) }
  }
  if (rangePreset.value === 'month') {
    return { from: new Date(now.getFullYear(), now.getMonth(), 1) }
  }
  if (rangePreset.value === 'custom') {
    const start = parseDateInput(customStartDate.value) || today
    const end = parseDateInput(customEndDate.value) || start
    return { from: start, to: addDays(end < start ? start : end, 1) }
  }
  return { from: today }
}

function eventQueryParams(): Record<string, string | number> {
  const range = selectedRange()
  return {
    after_id: lastEventId.value,
    limit: eventPageSize,
    from: range.from.toISOString(),
    ...(range.to ? { to: range.to.toISOString() } : {})
  }
}

function resetEventCursor(): void {
  events.value = []
  lastEventId.value = 0
}

async function handleRangePresetChange(): Promise<void> {
  resetEventCursor()
  await refreshAll()
}

async function handleCustomRangeChange(): Promise<void> {
  if (rangePreset.value !== 'custom') return
  resetEventCursor()
  await refreshAll()
}

async function fetchPet(): Promise<void> {
  const data = responseData<{
    pet: PetState
    identity: PetIdentity | null
    last_event_id: number
  }>(await axios.get('/api/pet/me'))
  pet.value = data.pet
  identity.value = data.identity
  if (events.value.length > 0 || lastEventId.value > 0) {
    lastEventId.value = Math.max(lastEventId.value, data.last_event_id || data.pet.last_event_id || 0)
  }
}

async function fetchEvents(): Promise<void> {
  for (let page = 0; page < 20; page += 1) {
    const data = responseData<{
      events: PetEvent[]
      last_event_id: number
    }>(await axios.get('/api/pet/events', {
      params: eventQueryParams()
    }))

    if (data.events.length) {
      events.value = [...events.value, ...data.events]
        .sort((left, right) => left.id - right.id)
        .slice(-maxCachedEvents)
      const newest = data.events[data.events.length - 1]
      lastEventId.value = Math.max(data.last_event_id, newest.id)
      if (newest.state_after && Object.keys(newest.state_after).length) {
        pet.value = { ...(pet.value || newest.state_after as PetState), ...newest.state_after } as PetState
      }
    } else {
      lastEventId.value = Math.max(lastEventId.value, data.last_event_id)
    }

    if (data.events.length < eventPageSize) break
  }
}

async function sendHeartbeat(): Promise<void> {
  if (!identity.value) return
  await axios.post('/api/pet/heartbeat', {
    last_event_id: lastEventId.value,
    app_version: 'dashboard-live-monitor'
  })
}

async function refreshAll(): Promise<void> {
  try {
    errorMessage.value = ''
    await fetchPet()
    if (identity.value) {
      await fetchEvents()
    } else {
      await fetchEvents()
    }
    lastUpdatedAt.value = new Date()
    connectionState.value = identity.value ? 'connected' : 'system'
  } catch (error) {
    console.error('Failed to refresh live monitor:', error)
    connectionState.value = pet.value ? 'degraded' : 'disconnected'
    errorMessage.value = error instanceof Error ? error.message : t('errors.loadFailed')
  } finally {
    loading.value = false
  }
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat(locale.value).format(value || 0)
}

function formatDateTime(value: string): string {
  if (!value) return t('common.unknown')
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return t('common.unknown')
  return date.toLocaleString(locale.value, {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  })
}

function minutesSince(value: string): number | null {
  if (!value) return null
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return null
  return Math.max(0, Math.round((Date.now() - timestamp) / 60_000))
}

function labelFromMap(prefix: string, value?: string): string {
  const key = value || 'unknown'
  return t(`${prefix}.${key}`)
}

function eventTypeLabel(eventType: string): string {
  const known = t(`eventTypes.${eventType}`)
  return known === `eventTypes.${eventType}` ? eventType : known
}

function sourceLabel(source: string): string {
  const known = t(`sources.names.${source || 'unknown'}`)
  return known === `sources.names.${source || 'unknown'}` ? source || t('common.unknown') : known
}

function eventIcon(event: PetEvent): string {
  if (event.event_type.includes('failed')) return 'mdi-alert-circle-outline'
  if (event.event_type.includes('completed') || event.event_type.includes('success')) return 'mdi-check-circle-outline'
  if (event.source === 'desktop') return 'mdi-monitor'
  if (event.source === 'router') return 'mdi-routes'
  if (event.source === 'harness') return 'mdi-clipboard-pulse-outline'
  if (event.source === 'hermes') return 'mdi-run-fast'
  if (event.source === 'obsidian') return 'mdi-notebook-outline'
  return 'mdi-pulse'
}

function eventDetail(event: PetEvent): string {
  const refs = [
    event.source_ref?.conversation_id ? t('events.refs.conversation', { id: event.source_ref.conversation_id }) : '',
    event.source_ref?.router_trace_id ? t('events.refs.trace', { id: event.source_ref.router_trace_id }) : '',
    event.source_ref?.harness_task_id ? t('events.refs.task', { id: event.source_ref.harness_task_id }) : '',
    event.source_ref?.hermes_job_id ? t('events.refs.job', { id: event.source_ref.hermes_job_id }) : '',
    event.source_ref?.desktop_session_id ? t('events.refs.desktop', { id: event.source_ref.desktop_session_id }) : ''
  ].filter(Boolean)

  if (refs.length) return refs.join(' · ')
  if (typeof event.payload?.action === 'string') {
    return t('events.action', { action: event.payload.action })
  }
  return sourceLabel(event.source)
}

const recentEvents = computed(() => [...events.value].reverse().slice(0, 24))

const rangeSummary = computed(() => {
  const range = selectedRange()
  const from = formatDateTime(range.from.toISOString())
  const to = range.to ? formatDateTime(range.to.toISOString()) : t('filters.now')
  return t('filters.summary', { from, to })
})

const identityLabel = computed(() => {
  if (!identity.value) return t('pet.unbound')
  return identity.value.employee_id || identity.value.feishu_open_id || identity.value.pet_id
})

const stateLabel = computed(() => labelFromMap('states', pet.value?.state))
const emotionLabel = computed(() => labelFromMap('emotions', pet.value?.emotion))
const sceneLabel = computed(() => labelFromMap('scenes', pet.value?.scene))

const connectionClass = computed(() => `is-${connectionState.value}`)
const connectionLabel = computed(() => t(`connection.${connectionState.value}`))

const lastUpdatedLabel = computed(() => {
  if (!lastUpdatedAt.value) return t('common.notUpdated')
  return lastUpdatedAt.value.toLocaleTimeString(locale.value, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  })
})

const lastActiveLabel = computed(() => {
  const minutes = minutesSince(pet.value?.last_active_at || pet.value?.updated_at || '')
  if (minutes === null) return t('common.unknown')
  if (minutes < 1) return t('time.justNow')
  if (minutes < 60) return t('time.minutesAgo', { minutes })
  return t('time.hoursAgo', { hours: Math.round(minutes / 60) })
})

const metricCards = computed(() => [
  {
    label: t('metrics.energy.label'),
    value: `${pet.value?.energy ?? 0}%`,
    note: t('metrics.energy.note'),
    icon: 'mdi-lightning-bolt-outline'
  },
  {
    label: t('metrics.xp.label'),
    value: formatNumber(pet.value?.xp ?? 0),
    note: t('metrics.xp.note', { coins: formatNumber(pet.value?.coins ?? 0) }),
    icon: 'mdi-star-four-points-outline'
  },
  {
    label: t('metrics.streak.label'),
    value: t('metrics.streak.value', { days: pet.value?.streak_days ?? 0 }),
    note: t('metrics.streak.note'),
    icon: 'mdi-calendar-check-outline'
  },
  {
    label: t('metrics.lastActive.label'),
    value: lastActiveLabel.value,
    note: t('metrics.lastActive.note', { time: lastUpdatedLabel.value }),
    icon: 'mdi-clock-outline'
  }
])

const healthItems = computed(() => [
  {
    label: t('health.petId'),
    value: pet.value?.pet_id || t('common.unknown')
  },
  {
    label: t('health.lastEvent'),
    value: formatNumber(lastEventId.value)
  },
  {
    label: t('health.cachedEvents'),
    value: formatNumber(events.value.length)
  },
  {
    label: t('health.updatedAt'),
    value: formatDateTime(pet.value?.updated_at || '')
  }
])

const sourceBreakdown = computed(() => {
  const counts = events.value.reduce<Record<string, number>>((acc, event) => {
    const source = event.source || 'unknown'
    acc[source] = (acc[source] || 0) + 1
    return acc
  }, {})
  const max = Math.max(1, ...Object.values(counts))
  return Object.entries(counts)
    .map(([name, count]) => ({
      name,
      count,
      percent: Math.round((count / max) * 100)
    }))
    .sort((left, right) => right.count - left.count)
})

onMounted(async () => {
  await refreshAll()
  void sendHeartbeat()
  pollTimer = window.setInterval(() => {
    void refreshAll()
  }, 5_000)
  heartbeatTimer = window.setInterval(() => {
    void sendHeartbeat()
  }, 30_000)
})

onBeforeUnmount(() => {
  if (pollTimer !== null) window.clearInterval(pollTimer)
  if (heartbeatTimer !== null) window.clearInterval(heartbeatTimer)
})
</script>

<style scoped>
.live-monitor-page {
  --monitor-bg: rgb(var(--v-theme-background));
  --monitor-surface: rgb(var(--v-theme-surface));
  --monitor-text: rgb(var(--v-theme-on-surface));
  --monitor-muted: rgba(var(--v-theme-on-surface), 0.68);
  --monitor-subtle: rgba(var(--v-theme-on-surface), 0.54);
  --monitor-border: rgba(var(--v-theme-on-surface), 0.12);
  --monitor-soft: rgba(var(--v-theme-primary), 0.08);
  min-height: 100%;
  background: var(--monitor-bg);
}

.live-monitor-page.is-dark {
  --monitor-border: rgba(var(--v-theme-on-surface), 0.18);
  --monitor-soft: rgba(var(--v-theme-primary), 0.14);
}

.live-monitor-shell {
  max-width: 1480px;
  margin: 0 auto;
  color: var(--monitor-text);
  font-family: "SF Pro Display", "SF Pro Text", -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}

.monitor-header,
.header-actions,
.hero-panel,
.pet-identity,
.pet-meta,
.card-head,
.event-filters,
.custom-range,
.event-title-row,
.health-row,
.source-row {
  display: flex;
}

.monitor-header {
  justify-content: space-between;
  align-items: flex-end;
  gap: 20px;
  margin-bottom: 24px;
}

.monitor-title {
  margin: 0;
  font-size: 1.5rem;
  line-height: 1.2;
  font-weight: 700;
  letter-spacing: 0;
}

.monitor-subtitle {
  margin: 6px 0 0;
  color: var(--monitor-muted);
  font-size: 0.875rem;
}

.header-actions {
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  justify-content: flex-end;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 32px;
  padding: 6px 10px;
  border: 1px solid var(--monitor-border);
  border-radius: 8px;
  background: var(--monitor-surface);
  color: var(--monitor-muted);
  font-size: 13px;
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: rgb(var(--v-theme-error));
}

.status-pill.is-connected .status-dot {
  background: rgb(var(--v-theme-success));
}

.status-pill.is-degraded .status-dot {
  background: rgb(var(--v-theme-warning));
}

.loading-wrap {
  display: flex;
  justify-content: center;
  padding: 80px 0;
}

.hero-panel {
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  margin-bottom: 18px;
  padding: 22px;
  border: 1px solid var(--monitor-border);
  border-radius: 8px;
  background: var(--monitor-surface);
}

.pet-identity {
  align-items: center;
  gap: 16px;
  min-width: 0;
}

.pet-avatar {
  display: grid;
  place-items: center;
  width: 72px;
  height: 72px;
  border-radius: 8px;
  background: var(--monitor-soft);
  color: rgb(var(--v-theme-primary));
}

.pet-name {
  font-size: 2rem;
  line-height: 1.1;
  font-weight: 750;
  letter-spacing: 0;
}

.pet-meta {
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 8px;
  color: var(--monitor-muted);
  font-size: 13px;
}

.pet-state {
  text-align: right;
}

.state-label {
  font-size: 1.6rem;
  line-height: 1.15;
  font-weight: 700;
}

.state-meta {
  margin-top: 8px;
  color: var(--monitor-muted);
  font-size: 13px;
}

.metric-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 14px;
  margin-bottom: 18px;
}

.content-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.5fr) minmax(320px, 0.8fr);
  gap: 18px;
  align-items: start;
}

.monitor-card {
  border: 1px solid var(--monitor-border);
  border-radius: 8px;
  background: var(--monitor-surface);
}

.metric-card {
  padding: 18px;
}

.metric-icon,
.event-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  border-radius: 8px;
  background: var(--monitor-soft);
  color: rgb(var(--v-theme-primary));
}

.metric-label {
  margin-top: 10px;
  color: var(--monitor-muted);
  font-size: 13px;
  font-weight: 500;
}

.metric-value {
  margin-top: 7px;
  font-size: clamp(22px, 2vw, 30px);
  line-height: 1.1;
  font-weight: 720;
  letter-spacing: 0;
  overflow-wrap: anywhere;
}

.metric-note {
  margin-top: 8px;
  color: var(--monitor-subtle);
  font-size: 12px;
  line-height: 1.45;
}

.event-card,
.health-card {
  padding: 20px;
}

.card-head {
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 16px;
}

.card-head.compact {
  margin-bottom: 12px;
}

.event-filters {
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin: -2px 0 18px;
}

.custom-range {
  gap: 8px;
  flex-wrap: wrap;
}

.custom-range :deep(.v-input) {
  width: 150px;
}

.range-summary {
  color: var(--monitor-subtle);
  font-size: 0.78rem;
}

.section-title {
  font-size: 18px;
  font-weight: 700;
  line-height: 1.3;
  letter-spacing: 0;
}

.section-title.small {
  margin-bottom: 12px;
  font-size: 15px;
}

.section-subtitle {
  margin-top: 5px;
  color: var(--monitor-muted);
  font-size: 13px;
  line-height: 1.45;
}

.event-list {
  display: grid;
  gap: 10px;
  max-height: 680px;
  overflow: auto;
  padding-right: 4px;
}

.event-row {
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr);
  gap: 12px;
  align-items: flex-start;
  padding: 12px;
  border: 1px solid var(--monitor-border);
  border-radius: 8px;
}

.event-icon.source-harness {
  color: rgb(var(--v-theme-warning));
}

.event-icon.source-hermes {
  color: rgb(var(--v-theme-success));
}

.event-icon.source-router {
  color: rgb(var(--v-theme-info));
}

.event-body {
  min-width: 0;
}

.event-title-row {
  justify-content: space-between;
  gap: 12px;
  color: var(--monitor-text);
  font-size: 13px;
}

.event-title-row span {
  flex: 0 0 auto;
  color: var(--monitor-subtle);
  font-size: 12px;
}

.event-detail {
  margin-top: 5px;
  color: var(--monitor-muted);
  font-size: 12px;
  line-height: 1.45;
  overflow-wrap: anywhere;
}

.health-list {
  display: grid;
  gap: 8px;
}

.health-row {
  justify-content: space-between;
  gap: 14px;
  padding: 11px 0;
  border-bottom: 1px solid var(--monitor-border);
  color: var(--monitor-muted);
  font-size: 13px;
}

.health-row strong {
  color: var(--monitor-text);
  text-align: right;
  overflow-wrap: anywhere;
}

.source-breakdown {
  margin-top: 22px;
}

.source-list {
  display: grid;
  gap: 12px;
}

.source-row {
  align-items: center;
  gap: 10px;
  color: var(--monitor-muted);
  font-size: 13px;
}

.source-row > span {
  width: 86px;
}

.source-meter {
  flex: 1;
  height: 8px;
  overflow: hidden;
  border-radius: 8px;
  background: rgba(var(--v-theme-on-surface), 0.08);
}

.source-meter span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: rgb(var(--v-theme-primary));
}

.source-row strong {
  width: 34px;
  text-align: right;
}

.empty-state {
  padding: 42px 12px;
  color: var(--monitor-muted);
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
  .monitor-header,
  .hero-panel {
    align-items: stretch;
    flex-direction: column;
  }

  .header-actions {
    justify-content: flex-start;
  }

  .pet-state {
    text-align: left;
  }

  .metric-grid {
    grid-template-columns: 1fr;
  }

  .event-title-row {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }
}
</style>
