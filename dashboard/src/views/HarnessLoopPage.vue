<script setup>
import { computed, onMounted, ref } from 'vue';
import { useTheme } from 'vuetify';
import axios from 'axios';

const theme = useTheme();
const isDark = computed(() => theme.global.current.value.dark);

const statuses = [
  { title: '全部', value: '' },
  { title: 'Pending', value: 'pending' },
  { title: 'In Progress', value: 'in_progress' },
  { title: 'Blocked', value: 'blocked' },
  { title: 'Review', value: 'review_required' },
  { title: 'Completed', value: 'completed' },
  { title: 'Failed', value: 'failed' }
];

const loadingTasks = ref(false);
const loadingTimeline = ref(false);
const selectedStatus = ref('');
const tasks = ref([]);
const selectedTask = ref(null);
const timeline = ref([]);
const error = ref('');

const filteredLoopEvents = computed(() =>
  timeline.value.filter((item) => String(item.event_type || '').startsWith('loop_'))
);

const fetchTasks = async () => {
  loadingTasks.value = true;
  error.value = '';
  try {
    const res = await axios.get('/api/harness-loop/tasks', {
      params: {
        status: selectedStatus.value || undefined,
        limit: 80
      }
    });
    tasks.value = res.data?.data?.tasks || [];
    if (tasks.value.length > 0) {
      await selectTask(tasks.value[0]);
    } else {
      selectedTask.value = null;
      timeline.value = [];
    }
  } catch (err) {
    error.value = err?.response?.data?.message || '加载 Loop 任务失败';
  } finally {
    loadingTasks.value = false;
  }
};

const selectTask = async (task) => {
  selectedTask.value = task;
  loadingTimeline.value = true;
  error.value = '';
  try {
    const res = await axios.get(`/api/harness-loop/tasks/${task.task_id}/timeline`);
    timeline.value = res.data?.data?.timeline || [];
  } catch (err) {
    error.value = err?.response?.data?.message || '加载任务时间线失败';
  } finally {
    loadingTimeline.value = false;
  }
};

const shortId = (value) => String(value || '').slice(0, 8);

onMounted(fetchTasks);
</script>

<template>
  <div class="dashboard-page harness-loop-page" :class="{ 'is-dark': isDark }">
    <v-container fluid class="dashboard-shell pa-4 pa-md-6">
      <div class="dashboard-header">
        <div class="dashboard-header-main">
          <h1 class="dashboard-title">Loop 闭环</h1>
          <p class="dashboard-subtitle">Harness 任务计划、执行观察和完成裁决时间线。</p>
        </div>
        <div class="dashboard-header-actions">
          <v-select
            v-model="selectedStatus"
            :items="statuses"
            density="compact"
            hide-details
            variant="outlined"
            class="status-select"
            @update:model-value="fetchTasks"
          />
          <v-btn
            color="primary"
            variant="flat"
            :loading="loadingTasks"
            prepend-icon="mdi-refresh"
            @click="fetchTasks"
          >
            刷新
          </v-btn>
        </div>
      </div>

      <v-alert v-if="error" type="error" variant="tonal" density="compact" class="mb-4">
        {{ error }}
      </v-alert>

      <div class="loop-layout">
        <section class="task-list">
          <v-progress-linear v-if="loadingTasks" indeterminate color="primary" />
          <button
            v-for="task in tasks"
            :key="task.task_id"
            class="task-row"
            :class="{ active: selectedTask?.task_id === task.task_id }"
            type="button"
            @click="selectTask(task)"
          >
            <span class="task-title">{{ task.title }}</span>
            <span class="task-meta">
              <v-chip size="x-small" variant="tonal">{{ task.status }}</v-chip>
              <span>{{ shortId(task.task_id) }}</span>
            </span>
          </button>
          <div v-if="!loadingTasks && tasks.length === 0" class="empty-state">
            暂无 Harness 任务
          </div>
        </section>

        <section class="timeline-panel">
          <div class="timeline-head">
            <div>
              <h2>{{ selectedTask?.title || '选择一个任务' }}</h2>
              <p v-if="selectedTask">{{ selectedTask.domain }} · {{ shortId(selectedTask.task_id) }}</p>
            </div>
            <v-chip v-if="selectedTask" size="small" variant="tonal">
              {{ selectedTask.status }}
            </v-chip>
          </div>

          <v-progress-linear v-if="loadingTimeline" indeterminate color="primary" />

          <div class="timeline">
            <article
              v-for="event in filteredLoopEvents"
              :key="event.event_id"
              class="timeline-item"
            >
              <div class="timeline-marker" />
              <div class="timeline-content">
                <div class="event-title">
                  <span>{{ event.event_type }}</span>
                  <time>{{ event.created_at }}</time>
                </div>
                <p>{{ event.payload?.summary || event.payload?.metadata?.settlement_status || 'Loop event' }}</p>
                <pre>{{ JSON.stringify(event.payload, null, 2) }}</pre>
              </div>
            </article>
            <div
              v-if="selectedTask && !loadingTimeline && filteredLoopEvents.length === 0"
              class="empty-state"
            >
              暂无 Loop 时间线事件
            </div>
          </div>
        </section>
      </div>
    </v-container>
  </div>
</template>

<style scoped>
@import '@/styles/dashboard-shell.css';

.loop-layout {
  display: grid;
  grid-template-columns: minmax(260px, 360px) minmax(0, 1fr);
  gap: 16px;
  min-height: 0;
}

.status-select {
  width: 180px;
}

.task-list,
.timeline-panel {
  border: 1px solid rgba(128, 128, 128, 0.18);
  border-radius: 8px;
  min-height: 520px;
  overflow: hidden;
  background: rgb(var(--v-theme-surface));
}

.task-row {
  display: flex;
  width: 100%;
  min-height: 72px;
  flex-direction: column;
  gap: 8px;
  align-items: flex-start;
  justify-content: center;
  border: 0;
  border-bottom: 1px solid rgba(128, 128, 128, 0.14);
  background: transparent;
  padding: 12px 14px;
  color: inherit;
  text-align: left;
  cursor: pointer;
}

.task-row.active {
  background: rgba(var(--v-theme-primary), 0.1);
}

.task-title {
  font-size: 14px;
  font-weight: 600;
  line-height: 1.35;
}

.task-meta {
  display: flex;
  gap: 8px;
  align-items: center;
  color: var(--dashboard-muted);
  font-size: 12px;
}

.timeline-head {
  display: flex;
  min-height: 76px;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid rgba(128, 128, 128, 0.14);
  padding: 14px 18px;
}

.timeline-head h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
}

.timeline-head p {
  margin: 4px 0 0;
  color: var(--dashboard-muted);
  font-size: 12px;
}

.timeline {
  padding: 18px;
}

.timeline-item {
  display: grid;
  grid-template-columns: 16px minmax(0, 1fr);
  gap: 12px;
  margin-bottom: 16px;
}

.timeline-marker {
  width: 10px;
  height: 10px;
  margin-top: 7px;
  border-radius: 50%;
  background: rgb(var(--v-theme-primary));
}

.timeline-content {
  min-width: 0;
}

.event-title {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  justify-content: space-between;
  font-size: 13px;
  font-weight: 700;
}

.event-title time {
  color: var(--dashboard-muted);
  font-size: 12px;
  font-weight: 400;
}

.timeline-content p {
  margin: 6px 0;
  color: var(--dashboard-muted);
  font-size: 13px;
}

.timeline-content pre {
  max-height: 220px;
  overflow: auto;
  border-radius: 8px;
  background: rgba(128, 128, 128, 0.1);
  padding: 10px;
  font-size: 12px;
  line-height: 1.45;
  white-space: pre-wrap;
}

.empty-state {
  padding: 24px;
  color: var(--dashboard-muted);
  font-size: 14px;
}

@media (max-width: 960px) {
  .loop-layout {
    grid-template-columns: 1fr;
  }

  .status-select {
    width: 100%;
  }
}
</style>
