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

type VerificationStatus = {
  config: {
    real_sender_enabled: boolean;
    approval_token_configured: boolean;
  };
  profiles: {
    verification_count: number;
    items: OutreachItem[];
  };
  go_no_go: {
    ready_for_one_person_send: boolean;
    reasons: string[];
  };
  report?: VerificationReport;
};

type VerificationCheck = {
  id: string;
  passed: boolean;
  message: string;
};

type VerificationReport = {
  sample_size: number;
  counts: Record<string, number>;
  rates: Record<string, number>;
  thresholds: Record<string, number>;
  checks: VerificationCheck[];
  go_no_go: {
    passed: boolean;
    next_scope: string;
  };
  rollback: {
    required: boolean;
    reasons: string[];
    actions: string[];
  };
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
const verificationStatus = ref<VerificationStatus | null>(null);
const verificationResult = ref<Record<string, any> | null>(null);
const verificationReport = ref<VerificationReport | null>(null);
const doctor = ref<Record<string, any>>({});
const loading = ref(false);
const dispatchLoading = ref(false);
const verificationLoading = ref(false);
const errorText = ref("");
const dispatchResult = ref<Record<string, any> | null>(null);
const approvalToken = ref("");

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

const verificationChecks = computed<VerificationCheck[]>(() => {
  return (verificationResult.value?.checks || []) as VerificationCheck[];
});

const verificationMetricRows = computed(() => {
  const report = verificationReport.value || verificationResult.value?.report;
  const rates = report?.rates || {};
  const thresholds = report?.thresholds || {};
  return [
    { label: "发送率", key: "sent_rate", value: rates.sent_rate || 0, target: thresholds.sent_rate || 0.95 },
    { label: "打开率", key: "open_rate", value: rates.open_rate || 0, target: thresholds.open_rate || 0.8 },
    { label: "表单打开率", key: "form_open_rate", value: rates.form_open_rate || 0, target: thresholds.form_open_rate || 0.95 },
    { label: "提交率", key: "submit_rate", value: rates.submit_rate || 0, target: thresholds.submit_rate || 0.9 },
    { label: "异常率", key: "anomaly_rate", value: rates.anomaly_rate || 0, target: thresholds.anomaly_rate || 0.05 },
  ];
});

const activeVerificationReport = computed<VerificationReport | null>(() => {
  return (verificationResult.value?.report as VerificationReport) || verificationReport.value;
});

async function refresh() {
  loading.value = true;
  errorText.value = "";
  try {
    const [
      dashboardResponse,
      doctorResponse,
      planResponse,
      verificationResponse,
      reportResponse,
    ] = await Promise.all([
      axios.get("/api/employee-insight/dashboard"),
      axios.get("/api/employee-insight/doctor"),
      axios.get("/api/employee-insight/outreach-plan"),
      axios.get("/api/employee-insight/verification/status"),
      axios.get("/api/employee-insight/verification/report"),
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
    if (verificationResponse.data.status === "ok") {
      verificationStatus.value = verificationResponse.data.data;
      verificationReport.value = verificationResponse.data.data.report || null;
    }
    if (reportResponse.data.status === "ok") {
      verificationReport.value = reportResponse.data.data;
    }
  } catch (error: any) {
    errorText.value = error?.response?.data?.message || error.message || "刷新失败";
  } finally {
    loading.value = false;
  }
}

async function refreshVerificationStatus() {
  const response = await axios.get("/api/employee-insight/verification/status");
  if (response.data.status === "ok") {
    verificationStatus.value = response.data.data;
  }
}

async function runVerification(sendOne = false) {
  verificationLoading.value = true;
  errorText.value = "";
  try {
    const response = await axios.post("/api/employee-insight/verification/run", {
      approval_token: approvalToken.value,
      send_one: sendOne,
    });
    if (response.data.status !== "ok") {
      throw new Error(response.data.message || "灰度验证失败");
    }
    verificationResult.value = response.data.data;
    verificationStatus.value = response.data.data.status;
    verificationReport.value = response.data.data.report;
  } catch (error: any) {
    errorText.value = error?.response?.data?.message || error.message || "灰度验证失败";
  } finally {
    verificationLoading.value = false;
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

    <v-card class="verification-panel mb-4">
      <v-card-text>
        <div class="verification-head">
          <div>
            <div class="section-kicker">灰度验证</div>
            <h2>一键灰度验证</h2>
          </div>
          <div class="verification-actions">
            <v-btn
              color="info"
              :loading="verificationLoading"
              prepend-icon="mdi-shield-check-outline"
              variant="tonal"
              @click="runVerification(false)"
            >
              自动检查
            </v-btn>
            <v-btn
              color="primary"
              :loading="verificationLoading"
              prepend-icon="mdi-send-check-outline"
              variant="flat"
              @click="runVerification(true)"
            >
              授权发送 1 人
            </v-btn>
          </div>
        </div>

        <div class="verification-grid">
          <div class="verification-status">
            <div class="status-row">
              <span>真实发送开关</span>
              <v-chip
                :color="verificationStatus?.config?.real_sender_enabled ? 'success' : 'warning'"
                size="small"
                variant="tonal"
              >
                {{ verificationStatus?.config?.real_sender_enabled ? "已开启" : "未开启" }}
              </v-chip>
            </div>
            <div class="status-row">
              <span>审批 token</span>
              <v-chip
                :color="verificationStatus?.config?.approval_token_configured ? 'success' : 'warning'"
                size="small"
                variant="tonal"
              >
                {{ verificationStatus?.config?.approval_token_configured ? "已配置" : "未配置" }}
              </v-chip>
            </div>
            <div class="status-row">
              <span>测试白名单</span>
              <v-chip color="info" size="small" variant="tonal">
                {{ verificationStatus?.profiles?.verification_count || 0 }} 人
              </v-chip>
            </div>
            <v-text-field
              v-model="approvalToken"
              class="mt-3"
              density="compact"
              hide-details
              label="审批 token"
              prepend-inner-icon="mdi-key-outline"
              type="password"
              variant="outlined"
            />
          </div>

          <div class="verification-checks">
            <div v-if="!verificationChecks.length" class="empty">暂无验证结果</div>
            <div
              v-for="item in verificationChecks"
              :key="item.id"
              class="check-row"
            >
              <v-icon :color="item.passed ? 'success' : 'error'" size="20">
                {{ item.passed ? "mdi-check-circle-outline" : "mdi-alert-circle-outline" }}
              </v-icon>
              <span>{{ item.message }}</span>
            </div>
          </div>

          <div class="verification-result">
            <div class="result-ring" :class="{ pass: verificationResult?.go_no_go?.passed }">
              <span>{{ verificationResult?.go_no_go?.passed ? "Go" : "No-Go" }}</span>
            </div>
            <div class="muted-line">
              {{ verificationResult?.go_no_go?.next_scope || "等待自动检查" }}
            </div>
            <div v-if="verificationResult?.send_result" class="muted-line">
              已发送 {{ verificationResult.send_result.sent?.length || 0 }} 人，
              失败 {{ verificationResult.send_result.failed?.length || 0 }} 人
            </div>
          </div>
        </div>

        <div class="verification-metrics">
          <div class="metrics-head">
            <span>灰度指标验收</span>
            <v-chip
              :color="activeVerificationReport?.go_no_go?.passed ? 'success' : 'warning'"
              size="small"
              variant="tonal"
            >
              样本 {{ activeVerificationReport?.sample_size || 0 }}
            </v-chip>
          </div>
          <div class="metric-strip">
            <div
              v-for="item in verificationMetricRows"
              :key="item.key"
              class="metric-pill"
            >
              <span>{{ item.label }}</span>
              <strong>{{ percent(item.value) }}</strong>
              <small>目标 {{ percent(item.target) }}</small>
            </div>
          </div>
          <v-alert
            v-if="activeVerificationReport?.rollback?.required"
            class="mt-3"
            density="compact"
            type="warning"
            variant="tonal"
          >
            {{ activeVerificationReport.rollback.reasons.join("；") }}
          </v-alert>
        </div>
      </v-card-text>
    </v-card>

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

.verification-panel {
  border: 1px solid rgba(var(--v-theme-primary), 0.18);
}

.verification-head {
  align-items: center;
  display: flex;
  gap: 16px;
  justify-content: space-between;
  margin-bottom: 16px;
}

.verification-head h2 {
  font-size: 20px;
  line-height: 1.25;
  margin: 2px 0 0;
}

.section-kicker {
  color: rgb(var(--v-theme-primary));
  font-size: 12px;
  font-weight: 700;
}

.verification-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: flex-end;
}

.verification-grid {
  display: grid;
  gap: 16px;
  grid-template-columns: minmax(220px, 0.95fr) minmax(260px, 1.35fr) minmax(150px, 0.7fr);
}

.verification-status,
.verification-checks,
.verification-result {
  border: 1px solid rgba(var(--v-theme-on-surface), 0.08);
  border-radius: 8px;
  padding: 14px;
}

.check-row {
  align-items: center;
  display: flex;
  gap: 8px;
  min-height: 30px;
}

.verification-result {
  align-items: center;
  display: flex;
  flex-direction: column;
  gap: 10px;
  justify-content: center;
  text-align: center;
}

.result-ring {
  align-items: center;
  border: 2px solid rgb(var(--v-theme-warning));
  border-radius: 50%;
  color: rgb(var(--v-theme-warning));
  display: flex;
  font-size: 22px;
  font-weight: 800;
  height: 86px;
  justify-content: center;
  width: 86px;
}

.result-ring.pass {
  border-color: rgb(var(--v-theme-success));
  color: rgb(var(--v-theme-success));
}

.verification-metrics {
  border-top: 1px solid rgba(var(--v-theme-on-surface), 0.08);
  margin-top: 16px;
  padding-top: 16px;
}

.metrics-head {
  align-items: center;
  display: flex;
  font-weight: 700;
  justify-content: space-between;
  margin-bottom: 10px;
}

.metric-strip {
  display: grid;
  gap: 10px;
  grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
}

.metric-pill {
  border: 1px solid rgba(var(--v-theme-on-surface), 0.08);
  border-radius: 8px;
  display: grid;
  gap: 4px;
  padding: 10px 12px;
}

.metric-pill strong {
  font-size: 22px;
  line-height: 1;
}

.metric-pill small {
  color: rgba(var(--v-theme-on-surface), 0.58);
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

@media (max-width: 960px) {
  .verification-grid {
    grid-template-columns: 1fr;
  }

  .verification-head {
    align-items: flex-start;
    flex-direction: column;
  }

  .verification-actions {
    justify-content: flex-start;
  }
}
</style>
