<script setup lang="ts">
import axios from "axios";
import { computed, onMounted, ref } from "vue";

type DashboardPayload = {
  proposals: {
    total: number;
    by_status: Record<string, number>;
    pending: Array<Record<string, unknown>>;
    approved_not_applied: Array<Record<string, unknown>>;
  };
  runtime_overrides: {
    config_ok: boolean;
    status: string;
    source: string;
    error: string;
    version: number | null;
    enabled_count: number;
    disabled_count: number;
  };
  memory_governance: {
    store_exists: boolean;
    total: number;
    by_status: Record<string, number>;
  };
  quality: {
    sample_count: number;
    average_score: number | null;
    blocked_count: number;
    review_required_count: number;
    passed_count: number;
    minimum_score: number;
  };
  ops_sop: {
    daily: string[];
    weekly: string[];
    escalation: string[];
  };
};

type Reminder = {
  reminder_id: string;
  severity: "info" | "warning" | "critical";
  title: string;
  message: string;
  action: string;
};

const emptyDashboard: DashboardPayload = {
  proposals: {
    total: 0,
    by_status: {},
    pending: [],
    approved_not_applied: [],
  },
  runtime_overrides: {
    config_ok: false,
    status: "unknown",
    source: "",
    error: "",
    version: null,
    enabled_count: 0,
    disabled_count: 0,
  },
  memory_governance: {
    store_exists: false,
    total: 0,
    by_status: {},
  },
  quality: {
    sample_count: 0,
    average_score: null,
    blocked_count: 0,
    review_required_count: 0,
    passed_count: 0,
    minimum_score: 80,
  },
  ops_sop: {
    daily: [],
    weekly: [],
    escalation: [],
  },
};

const dashboard = ref<DashboardPayload>(emptyDashboard);
const reminders = ref<Reminder[]>([]);
const doctor = ref<Record<string, unknown>>({});
const loading = ref(false);
const actionLoading = ref("");
const doctorLoaded = ref(false);
const doctorError = ref("");
const snackbar = ref({
  show: false,
  color: "success",
  text: "",
});

const metricRows = computed(() => [
  {
    label: "待审批规则",
    value: dashboard.value.proposals.by_status.pending || 0,
    icon: "mdi-clipboard-clock-outline",
    color: "warning",
  },
  {
    label: "已批准未应用",
    value: dashboard.value.proposals.by_status.approved_for_runtime || 0,
    icon: "mdi-shield-check-outline",
    color: "error",
  },
  {
    label: "生效规则",
    value: dashboard.value.runtime_overrides.enabled_count,
    icon: "mdi-toggle-switch-outline",
    color: "success",
  },
  {
    label: "待审核记忆",
    value: dashboard.value.memory_governance.by_status.need_review || 0,
    icon: "mdi-brain",
    color: "primary",
  },
]);

const doctorMissing = computed(() => {
  const missing = doctor.value.missing;
  return Array.isArray(missing) ? missing : [];
});

const doctorReady = computed(() => doctor.value.ready === true);

const runtimeStatusText = computed(() => {
  const status = dashboard.value.runtime_overrides.status || "unknown";
  if (status === "active") {
    return `active v${dashboard.value.runtime_overrides.version ?? "-"}`;
  }
  if (status === "configured_empty") return "configured / empty";
  if (status === "missing") return "missing";
  if (status === "invalid") return "invalid";
  return "unknown";
});

const runtimeStatusColor = computed(() => {
  const status = dashboard.value.runtime_overrides.status || "unknown";
  if (status === "active") return "success";
  if (status === "configured_empty") return "info";
  if (status === "invalid") return "error";
  return "warning";
});

function showMessage(text: string, color = "success") {
  snackbar.value = { show: true, color, text };
}

async function refresh() {
  loading.value = true;
  doctorError.value = "";
  try {
    const [dashboardResponse, remindersResponse, doctorResponse] = await Promise.all([
      axios.get("/api/content-sop-ops/dashboard"),
      axios.get("/api/content-sop-ops/reminders"),
      axios.get("/api/content-sop-ops/doctor"),
    ]);
    if (dashboardResponse.data.status === "ok") {
      dashboard.value = dashboardResponse.data.data;
    }
    if (remindersResponse.data.status === "ok") {
      reminders.value = remindersResponse.data.data.items || [];
    }
    if (doctorResponse.data.status === "ok") {
      doctor.value = doctorResponse.data.data || {};
      doctorLoaded.value = true;
    }
  } catch (error: any) {
    doctorLoaded.value = false;
    doctorError.value = error?.response?.data?.message || error.message || "刷新失败";
    showMessage(error?.response?.data?.message || error.message || "刷新失败", "error");
  } finally {
    loading.value = false;
  }
}

async function runScheduledAudit() {
  actionLoading.value = "audit";
  try {
    const response = await axios.post("/api/content-sop-ops/scheduled-run", {
      content_sop_ops: {
        audit_report_enabled: true,
      },
    });
    if (response.data.status !== "ok") {
      throw new Error(response.data.message || "定时审计失败");
    }
    showMessage(response.data.data.report_path ? "审计报表已导出" : "审计检查已完成");
    await refresh();
  } catch (error: any) {
    showMessage(error?.response?.data?.message || error.message || "定时审计失败", "error");
  } finally {
    actionLoading.value = "";
  }
}

function severityColor(severity: Reminder["severity"]) {
  if (severity === "critical") return "error";
  if (severity === "warning") return "warning";
  return "info";
}

onMounted(refresh);
</script>

<template>
  <div class="content-sop-ops-page">
    <div class="page-toolbar">
      <div>
        <h1>内容 SOP 运营</h1>
        <p>规则候选、记忆治理、运行时覆盖、质量 gate 和审计任务的控制台。</p>
      </div>
      <div class="toolbar-actions">
        <v-btn
          color="primary"
          variant="tonal"
          prepend-icon="mdi-refresh"
          :loading="loading"
          @click="refresh"
        >
          刷新
        </v-btn>
        <v-btn
          color="primary"
          prepend-icon="mdi-file-export-outline"
          :loading="actionLoading === 'audit'"
          @click="runScheduledAudit"
        >
          导出审计
        </v-btn>
      </div>
    </div>

    <div class="metric-grid">
      <v-sheet
        v-for="row in metricRows"
        :key="row.label"
        class="metric-panel"
        border
        rounded="sm"
      >
        <v-icon :icon="row.icon" :color="row.color" size="24" />
        <div>
          <div class="metric-label">{{ row.label }}</div>
          <div class="metric-value">{{ row.value }}</div>
        </div>
      </v-sheet>
    </div>

    <div class="ops-grid">
      <v-sheet class="panel" border rounded="sm">
        <div class="panel-heading">
          <h2>主动提醒</h2>
          <v-chip size="small" color="primary" variant="tonal">{{ reminders.length }}</v-chip>
        </div>
        <div v-if="reminders.length" class="reminder-list">
          <div v-for="item in reminders" :key="item.reminder_id" class="reminder-row">
            <v-chip size="small" :color="severityColor(item.severity)" variant="tonal">
              {{ item.severity }}
            </v-chip>
            <div>
              <div class="row-title">{{ item.title }}</div>
              <div class="row-subtitle">{{ item.message }}</div>
            </div>
          </div>
        </div>
        <div v-else class="empty-state">当前没有需要处理的运营提醒。</div>
      </v-sheet>

      <v-sheet class="panel" border rounded="sm">
        <div class="panel-heading">
          <h2>生产配置</h2>
          <v-chip
            size="small"
            :color="doctorReady ? 'success' : 'warning'"
            variant="tonal"
          >
            {{ doctorReady ? "ready" : "needs review" }}
          </v-chip>
        </div>
        <div v-if="!doctorLoaded" class="check-list">
          <div class="check-row">
            <v-icon icon="mdi-alert-circle-outline" color="warning" size="18" />
            <span>{{ doctorError || "生产配置状态未确认。" }}</span>
          </div>
        </div>
        <div v-else-if="doctorMissing.length" class="check-list">
          <div v-for="item in doctorMissing" :key="String(item)" class="check-row">
            <v-icon icon="mdi-alert-circle-outline" color="warning" size="18" />
            <span>{{ item }}</span>
          </div>
        </div>
        <div v-else-if="doctorReady" class="empty-state">
          生产开关、路径和管理员配置已确认。
        </div>
        <div v-else class="check-list">
          <div class="check-row">
            <v-icon icon="mdi-alert-circle-outline" color="warning" size="18" />
            <span>生产配置状态未通过确认。</span>
          </div>
        </div>
      </v-sheet>

      <v-sheet class="panel" border rounded="sm">
        <div class="panel-heading">
          <h2>质量评分</h2>
          <v-chip size="small" color="info" variant="tonal">
            min {{ dashboard.quality.minimum_score }}
          </v-chip>
        </div>
        <div class="quality-row">
          <div>
            <div class="metric-label">平均分</div>
            <div class="metric-value">
              {{ dashboard.quality.average_score ?? "N/A" }}
            </div>
          </div>
          <div>
            <div class="metric-label">阻塞</div>
            <div class="metric-value compact">{{ dashboard.quality.blocked_count }}</div>
          </div>
          <div>
            <div class="metric-label">待复核</div>
            <div class="metric-value compact">
              {{ dashboard.quality.review_required_count }}
            </div>
          </div>
        </div>
      </v-sheet>

      <v-sheet class="panel" border rounded="sm">
        <div class="panel-heading">
          <h2>Runtime</h2>
          <v-chip
            size="small"
            :color="runtimeStatusColor"
            variant="tonal"
          >
            {{ runtimeStatusText }}
          </v-chip>
        </div>
        <div class="runtime-row">
          <span>启用 {{ dashboard.runtime_overrides.enabled_count }}</span>
          <span>停用 {{ dashboard.runtime_overrides.disabled_count }}</span>
          <span>记忆 {{ dashboard.memory_governance.total }}</span>
        </div>
        <div
          v-if="!dashboard.runtime_overrides.config_ok"
          class="runtime-warning"
        >
          {{ dashboard.runtime_overrides.error || dashboard.runtime_overrides.source || "runtime config not confirmed" }}
        </div>
      </v-sheet>
    </div>

    <v-sheet class="panel sop-panel" border rounded="sm">
      <div class="panel-heading">
        <h2>运营 SOP</h2>
        <v-chip size="small" color="primary" variant="outlined">daily / weekly</v-chip>
      </div>
      <div class="sop-columns">
        <div>
          <h3>每日</h3>
          <ul>
            <li v-for="item in dashboard.ops_sop.daily" :key="item">{{ item }}</li>
          </ul>
        </div>
        <div>
          <h3>每周</h3>
          <ul>
            <li v-for="item in dashboard.ops_sop.weekly" :key="item">{{ item }}</li>
          </ul>
        </div>
        <div>
          <h3>升级</h3>
          <ul>
            <li v-for="item in dashboard.ops_sop.escalation" :key="item">{{ item }}</li>
          </ul>
          <div v-if="!dashboard.ops_sop.escalation.length" class="empty-state">
            当前没有升级项。
          </div>
        </div>
      </div>
    </v-sheet>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="2600">
      {{ snackbar.text }}
    </v-snackbar>
  </div>
</template>

<style scoped>
.content-sop-ops-page {
  padding: 24px;
}

.page-toolbar,
.panel-heading,
.toolbar-actions,
.metric-panel,
.reminder-row,
.check-row,
.runtime-row {
  display: flex;
  align-items: center;
}

.page-toolbar {
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 20px;
}

.page-toolbar h1,
.panel-heading h2,
.sop-columns h3 {
  margin: 0;
  letter-spacing: 0;
}

.page-toolbar h1 {
  font-size: 28px;
  line-height: 1.2;
}

.page-toolbar p,
.row-subtitle,
.empty-state,
.metric-label {
  color: rgba(var(--v-theme-on-surface), 0.68);
}

.page-toolbar p {
  margin: 6px 0 0;
}

.toolbar-actions {
  gap: 10px;
  flex-wrap: wrap;
}

.metric-grid,
.ops-grid,
.sop-columns {
  display: grid;
  gap: 14px;
}

.metric-grid {
  grid-template-columns: repeat(4, minmax(0, 1fr));
  margin-bottom: 14px;
}

.ops-grid {
  grid-template-columns: repeat(2, minmax(0, 1fr));
  margin-bottom: 14px;
}

.metric-panel,
.panel {
  padding: 16px;
}

.metric-panel {
  gap: 12px;
  min-height: 88px;
}

.metric-label {
  font-size: 13px;
}

.metric-value {
  font-size: 30px;
  font-weight: 700;
  line-height: 1.1;
}

.metric-value.compact {
  font-size: 22px;
}

.panel-heading {
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 14px;
}

.panel-heading h2 {
  font-size: 18px;
}

.reminder-list,
.check-list {
  display: grid;
  gap: 10px;
}

.reminder-row {
  gap: 12px;
  align-items: flex-start;
}

.row-title {
  font-weight: 600;
}

.row-subtitle {
  margin-top: 2px;
  font-size: 13px;
}

.check-row {
  gap: 8px;
}

.quality-row {
  display: grid;
  grid-template-columns: 1.2fr 1fr 1fr;
  gap: 12px;
}

.runtime-row {
  justify-content: space-between;
  gap: 10px;
  font-weight: 600;
}

.runtime-warning {
  margin-top: 10px;
  color: rgb(var(--v-theme-warning));
  font-size: 13px;
  line-height: 1.4;
  word-break: break-word;
}

.sop-columns {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}

.sop-columns h3 {
  font-size: 15px;
  margin-bottom: 8px;
}

.sop-columns ul {
  padding-left: 18px;
  margin: 0;
}

.sop-columns li {
  margin-bottom: 6px;
}

@media (max-width: 960px) {
  .metric-grid,
  .ops-grid,
  .sop-columns {
    grid-template-columns: 1fr;
  }

  .page-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
