<script setup lang="ts">
import axios from "axios";
import { computed, onMounted, ref } from "vue";

type Candidate = {
  candidate_id: string;
  candidate_type: string;
  department_id: string;
  scenario_id: string;
  title: string;
  summary: string;
  confidence: number;
  review_status: string;
  sensitivity: string;
  recommended_action: string;
  is_runtime_eligible: boolean;
};

type OutreachItem = {
  employee_id: string;
  employee_hash: string;
  display_name: string;
  department_id: string;
  role: string;
  reason: string;
  unanswered_outreach_count: number;
};

type DashboardPayload = {
  metrics: Record<string, number>;
  top_scenarios: Array<{ scenario_id: string; count: number }>;
  friction_points: Array<{ friction_point: string; count: number }>;
  pending_candidates: Candidate[];
  hermes_candidates: Candidate[];
};

const emptyDashboard: DashboardPayload = {
  metrics: {
    total_sessions: 0,
    outreach_sent: 0,
    effective_conversations: 0,
    completed_sessions: 0,
    blocked_sessions: 0,
    pending_candidates: 0,
    released_improvements: 0,
  },
  top_scenarios: [],
  friction_points: [],
  pending_candidates: [],
  hermes_candidates: [],
};

const dashboard = ref<DashboardPayload>(emptyDashboard);
const outreachPlan = ref<{ eligible: OutreachItem[]; skipped: OutreachItem[] }>({
  eligible: [],
  skipped: [],
});
const doctor = ref<Record<string, any>>({});
const loading = ref(false);
const dispatchLoading = ref(false);
const errorText = ref("");
const dispatchResult = ref<Record<string, any> | null>(null);

const metricRows = computed(() => [
  {
    label: "试点员工",
    value: dashboard.value.metrics.active_pilots || 0,
    icon: "mdi-account-multiple-check-outline",
    color: "primary",
  },
  {
    label: "有效对话",
    value: dashboard.value.metrics.effective_conversations || 0,
    icon: "mdi-account-voice",
    color: "success",
  },
  {
    label: "完成任务",
    value: dashboard.value.metrics.completed_sessions || 0,
    icon: "mdi-check-circle-outline",
    color: "info",
  },
  {
    label: "待审批洞察",
    value: dashboard.value.metrics.pending_candidates || 0,
    icon: "mdi-clipboard-clock-outline",
    color: "warning",
  },
]);

async function refresh() {
  loading.value = true;
  errorText.value = "";
  try {
    const [dashboardResponse, doctorResponse, planResponse] = await Promise.all([
      axios.get("/api/employee-insight/dashboard"),
      axios.get("/api/employee-insight/doctor"),
      axios.get("/api/employee-insight/outreach-plan"),
    ]);
    if (dashboardResponse.data.status === "ok") {
      dashboard.value = dashboardResponse.data.data || emptyDashboard;
    }
    if (doctorResponse.data.status === "ok") {
      doctor.value = doctorResponse.data.data || {};
    }
    if (planResponse.data.status === "ok") {
      outreachPlan.value = planResponse.data.data || { eligible: [], skipped: [] };
    }
  } catch (error: any) {
    errorText.value = error?.response?.data?.message || error.message || "刷新失败";
  } finally {
    loading.value = false;
  }
}

async function runDryDispatch() {
  dispatchLoading.value = true;
  errorText.value = "";
  try {
    const response = await axios.post("/api/employee-insight/outreach-dispatch", {
      dry_run: true,
    });
    if (response.data.status !== "ok") {
      throw new Error(response.data.message || "触达预演失败");
    }
    dispatchResult.value = response.data.data;
    outreachPlan.value = {
      eligible: response.data.data.planned || [],
      skipped: response.data.data.skipped || [],
    };
  } catch (error: any) {
    errorText.value = error?.response?.data?.message || error.message || "触达预演失败";
  } finally {
    dispatchLoading.value = false;
  }
}

function percent(value: number) {
  return `${Math.round(value * 100)}%`;
}

onMounted(refresh);
</script>

<template>
  <div class="employee-insight-page">
    <div class="page-toolbar">
      <div>
        <h1>员工需求洞察</h1>
        <p>飞书私聊陪跑、需求卡点、候选洞察、Obsidian 审批和 Hermes 深挖的闭环看板。</p>
      </div>
      <v-btn
        color="primary"
        :loading="loading"
        prepend-icon="mdi-refresh"
        variant="flat"
        @click="refresh"
      >
        刷新
      </v-btn>
      <v-btn
        color="secondary"
        :loading="dispatchLoading"
        prepend-icon="mdi-send-clock-outline"
        variant="tonal"
        @click="runDryDispatch"
      >
        触达预演
      </v-btn>
    </div>

    <v-alert
      v-if="errorText"
      class="mb-4"
      type="error"
      variant="tonal"
      density="comfortable"
    >
      {{ errorText }}
    </v-alert>

    <div class="metric-grid">
      <v-card v-for="item in metricRows" :key="item.label" variant="tonal">
        <v-card-text>
          <div class="metric-card">
            <v-icon :color="item.color" size="30">{{ item.icon }}</v-icon>
            <div>
              <div class="metric-value">{{ item.value }}</div>
              <div class="metric-label">{{ item.label }}</div>
            </div>
          </div>
        </v-card-text>
      </v-card>
    </div>

    <div class="content-grid">
      <v-card>
        <v-card-title>高频场景</v-card-title>
        <v-card-text>
          <div v-if="!dashboard.top_scenarios.length" class="empty">暂无场景数据</div>
          <div
            v-for="item in dashboard.top_scenarios"
            :key="item.scenario_id"
            class="rank-row"
          >
            <span>{{ item.scenario_id }}</span>
            <v-chip color="primary" size="small" variant="tonal">{{ item.count }}</v-chip>
          </div>
        </v-card-text>
      </v-card>

      <v-card>
        <v-card-title>高频卡点</v-card-title>
        <v-card-text>
          <div v-if="!dashboard.friction_points.length" class="empty">暂无卡点数据</div>
          <div
            v-for="item in dashboard.friction_points"
            :key="item.friction_point"
            class="rank-row"
          >
            <span>{{ item.friction_point }}</span>
            <v-chip color="warning" size="small" variant="tonal">{{ item.count }}</v-chip>
          </div>
        </v-card-text>
      </v-card>

      <v-card>
        <v-card-title>运行状态</v-card-title>
        <v-card-text>
          <div class="status-row">
            <span>数据存储</span>
            <v-chip :color="doctor.store?.exists ? 'success' : 'warning'" size="small">
              {{ doctor.store?.exists ? "已创建" : "待创建" }}
            </v-chip>
          </div>
          <div class="status-row">
            <span>Obsidian 审批目录</span>
            <v-chip :color="doctor.obsidian?.exists ? 'success' : 'warning'" size="small">
              {{ doctor.obsidian?.exists ? "已就绪" : "待创建" }}
            </v-chip>
          </div>
          <div class="status-row">
            <span>发布策略</span>
            <v-chip color="info" size="small">{{ doctor.runtime_policy?.mutation_policy || "approved-only" }}</v-chip>
          </div>
        </v-card-text>
      </v-card>

      <v-card>
        <v-card-title>今日触达计划</v-card-title>
        <v-card-text>
          <div v-if="!outreachPlan.eligible.length" class="empty">暂无可触达员工</div>
          <div
            v-for="item in outreachPlan.eligible.slice(0, 8)"
            :key="item.employee_id"
            class="rank-row"
          >
            <span>{{ item.display_name || item.employee_hash || item.employee_id }}</span>
            <v-chip color="success" size="small" variant="tonal">可触达</v-chip>
          </div>
          <v-divider class="my-3" />
          <div class="muted-line">已跳过 {{ outreachPlan.skipped.length }} 人</div>
          <div v-if="dispatchResult" class="muted-line">
            最近预演：计划 {{ dispatchResult.planned?.length || 0 }} 人，实际发送 0 人
          </div>
        </v-card-text>
      </v-card>
    </div>

    <v-card class="mt-4">
      <v-card-title>待审批洞察</v-card-title>
      <v-table density="comfortable">
        <thead>
          <tr>
            <th>类型</th>
            <th>标题</th>
            <th>部门</th>
            <th>场景</th>
            <th>置信度</th>
            <th>建议动作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-if="!dashboard.pending_candidates.length">
            <td colspan="6" class="empty">暂无待审批洞察</td>
          </tr>
          <tr v-for="item in dashboard.pending_candidates" :key="item.candidate_id">
            <td>{{ item.candidate_type }}</td>
            <td>
              <div class="candidate-title">{{ item.title }}</div>
              <div class="candidate-summary">{{ item.summary }}</div>
            </td>
            <td>{{ item.department_id || "-" }}</td>
            <td>{{ item.scenario_id || "-" }}</td>
            <td>{{ percent(item.confidence || 0) }}</td>
            <td>{{ item.recommended_action || "-" }}</td>
          </tr>
        </tbody>
      </v-table>
    </v-card>
  </div>
</template>

<style scoped>
.employee-insight-page {
  padding: 20px;
}

.page-toolbar {
  align-items: center;
  display: flex;
  gap: 16px;
  justify-content: space-between;
  margin-bottom: 18px;
}

.page-toolbar h1 {
  font-size: 24px;
  font-weight: 700;
  margin: 0;
}

.page-toolbar p {
  color: rgba(var(--v-theme-on-surface), 0.68);
  margin: 6px 0 0;
}

.metric-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  margin-bottom: 16px;
}

.metric-card {
  align-items: center;
  display: flex;
  gap: 12px;
}

.metric-value {
  font-size: 26px;
  font-weight: 700;
  line-height: 1;
}

.metric-label,
.candidate-summary,
.empty {
  color: rgba(var(--v-theme-on-surface), 0.62);
}

.content-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
}

.rank-row,
.status-row {
  align-items: center;
  display: flex;
  justify-content: space-between;
  min-height: 34px;
}

.candidate-title {
  font-weight: 600;
}

.candidate-summary {
  font-size: 13px;
  margin-top: 2px;
}
</style>
