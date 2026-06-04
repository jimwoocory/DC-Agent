<script setup lang="ts">
import axios from "axios";
import { computed, onMounted, ref } from "vue";

type StatusPayload = {
  total: number;
  by_status: Record<string, number>;
  by_sensitivity: Record<string, number>;
  recent: Array<Record<string, unknown>>;
  paths: Record<string, string>;
  store_exists: boolean;
};

type AuditItem = {
  audit_id: string;
  memory_id: string;
  action: string;
  actor: string;
  payload_json: string;
  created_at: string;
};

const status = ref<StatusPayload>({
  total: 0,
  by_status: {},
  by_sensitivity: {},
  recent: [],
  paths: {},
  store_exists: false,
});
const auditItems = ref<AuditItem[]>([]);
const loading = ref(false);
const actionLoading = ref("");
const exportLimit = ref(50);
const promoteDryRun = ref(true);
const snackbar = ref({
  show: false,
  color: "success",
  text: "",
});

const statusRows = computed(() => [
  {
    label: "待审核",
    value: status.value.by_status.need_review || 0,
    icon: "mdi-file-search-outline",
    color: "warning",
  },
  {
    label: "已批准",
    value: status.value.by_status.approved || 0,
    icon: "mdi-check-decagram-outline",
    color: "success",
  },
  {
    label: "已拒绝",
    value: status.value.by_status.rejected || 0,
    icon: "mdi-close-octagon-outline",
    color: "error",
  },
  {
    label: "已归档",
    value: status.value.by_status.archived || 0,
    icon: "mdi-archive-outline",
    color: "info",
  },
]);

const pathRows = computed(() =>
  Object.entries(status.value.paths || {}).map(([key, value]) => ({
    key,
    value,
  })),
);

function showMessage(text: string, color = "success") {
  snackbar.value = { show: true, color, text };
}

function resultCount(data: Record<string, unknown>) {
  const entries = [
    ["导出", data.exported_count],
    ["导入", data.imported_count],
    ["决策", data.decision_count],
    ["审计", data.audit_count],
    ["可发布", Array.isArray(data.promoted_memory_ids) ? data.promoted_memory_ids.length : undefined],
    ["跳过", Array.isArray(data.skipped_memory_ids) ? data.skipped_memory_ids.length : undefined],
  ].filter((entry) => entry[1] !== undefined);
  return entries.map(([label, value]) => `${label} ${value}`).join("，");
}

async function refresh() {
  loading.value = true;
  try {
    const [statusResponse, auditResponse] = await Promise.all([
      axios.get("/api/memory-governance/status"),
      axios.get("/api/memory-governance/audit", { params: { limit: 30 } }),
    ]);
    if (statusResponse.data.status === "ok") {
      status.value = statusResponse.data.data;
    }
    if (auditResponse.data.status === "ok") {
      auditItems.value = auditResponse.data.data.items || [];
    }
  } catch (error: any) {
    showMessage(error?.response?.data?.message || error.message || "刷新失败", "error");
  } finally {
    loading.value = false;
  }
}

async function runAction(name: string, url: string, body: Record<string, unknown>) {
  actionLoading.value = name;
  try {
    const response = await axios.post(url, body);
    if (response.data.status !== "ok") {
      throw new Error(response.data.message || "操作失败");
    }
    showMessage(resultCount(response.data.data) || "操作完成");
    await refresh();
  } catch (error: any) {
    showMessage(error?.response?.data?.message || error.message || "操作失败", "error");
  } finally {
    actionLoading.value = "";
  }
}

function exportCandidates() {
  runAction("export", "/api/memory-governance/export", {
    limit: exportLimit.value,
  });
}

function importNotes() {
  runAction("import", "/api/memory-governance/import", {
    actor: "dashboard-memory-governance",
  });
}

function promoteMemories() {
  runAction("promote", "/api/memory-governance/promote", {
    actor: "dashboard-memory-governance",
    dry_run: promoteDryRun.value,
  });
}

function formatAuditPayload(item: AuditItem) {
  try {
    const parsed = JSON.parse(item.payload_json || "{}");
    return Object.entries(parsed)
      .slice(0, 3)
      .map(([key, value]) => `${key}: ${value}`)
      .join(" / ");
  } catch {
    return item.payload_json || "";
  }
}

onMounted(refresh);
</script>

<template>
  <div class="memory-governance-page">
    <div class="page-toolbar">
      <div>
        <h1>记忆治理</h1>
        <p>NAS 候选记忆、Obsidian 人工审核、主召回发布的控制台。</p>
      </div>
      <v-btn
        color="primary"
        variant="tonal"
        prepend-icon="mdi-refresh"
        :loading="loading"
        @click="refresh"
      >
        刷新
      </v-btn>
    </div>

    <div class="status-grid">
      <v-sheet class="metric-panel total-panel" border rounded="sm">
        <div class="metric-label">治理库总量</div>
        <div class="metric-value">{{ status.total }}</div>
        <v-chip size="small" :color="status.store_exists ? 'success' : 'warning'" variant="tonal">
          {{ status.store_exists ? "已建立" : "未初始化" }}
        </v-chip>
      </v-sheet>
      <v-sheet
        v-for="row in statusRows"
        :key="row.label"
        class="metric-panel"
        border
        rounded="sm"
      >
        <v-icon :color="row.color" :icon="row.icon" size="22" />
        <div>
          <div class="metric-label">{{ row.label }}</div>
          <div class="metric-value compact">{{ row.value }}</div>
        </div>
      </v-sheet>
    </div>

    <div class="workbench-grid">
      <v-sheet class="action-panel" border rounded="sm">
        <div class="panel-heading">
          <h2>治理动作</h2>
          <v-chip size="small" color="primary" variant="outlined">Obsidian</v-chip>
        </div>
        <div class="action-row">
          <v-text-field
            v-model.number="exportLimit"
            type="number"
            label="导出上限"
            min="1"
            max="500"
            density="compact"
            variant="outlined"
            hide-details
          />
          <v-btn
            color="primary"
            prepend-icon="mdi-export"
            :loading="actionLoading === 'export'"
            @click="exportCandidates"
          >
            导出候选
          </v-btn>
        </div>
        <div class="action-row">
          <div class="action-copy">从 Obsidian `40_MemoryGovernance` 读取审核结果。</div>
          <v-btn
            color="secondary"
            prepend-icon="mdi-import"
            :loading="actionLoading === 'import'"
            @click="importNotes"
          >
            导入审核
          </v-btn>
        </div>
        <div class="action-row">
          <v-switch
            v-model="promoteDryRun"
            color="primary"
            label="发布前干跑"
            density="compact"
            hide-details
          />
          <v-btn
            color="success"
            prepend-icon="mdi-upload"
            :loading="actionLoading === 'promote'"
            @click="promoteMemories"
          >
            发布到召回
          </v-btn>
        </div>
      </v-sheet>

      <v-sheet class="path-panel" border rounded="sm">
        <div class="panel-heading">
          <h2>系统路径</h2>
        </div>
        <v-table density="compact">
          <tbody>
            <tr v-for="row in pathRows" :key="row.key">
              <td class="path-key">{{ row.key }}</td>
              <td class="path-value">{{ row.value }}</td>
            </tr>
          </tbody>
        </v-table>
      </v-sheet>
    </div>

    <v-sheet class="table-panel" border rounded="sm">
      <div class="panel-heading">
        <h2>最近记忆</h2>
      </div>
      <v-table density="compact">
        <thead>
          <tr>
            <th>标题</th>
            <th>状态</th>
            <th>敏感级别</th>
            <th>来源</th>
            <th>更新时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in status.recent" :key="String(item.memory_id)">
            <td>{{ item.title }}</td>
            <td>{{ item.review_status }}</td>
            <td>{{ item.sensitivity }}</td>
            <td>{{ item.source_system }}</td>
            <td>{{ item.updated_at }}</td>
          </tr>
          <tr v-if="status.recent.length === 0">
            <td colspan="5" class="empty-cell">暂无治理记忆</td>
          </tr>
        </tbody>
      </v-table>
    </v-sheet>

    <v-sheet class="table-panel" border rounded="sm">
      <div class="panel-heading">
        <h2>最近审计</h2>
      </div>
      <v-table density="compact">
        <thead>
          <tr>
            <th>动作</th>
            <th>记忆 ID</th>
            <th>操作者</th>
            <th>摘要</th>
            <th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in auditItems" :key="item.audit_id">
            <td>{{ item.action }}</td>
            <td class="mono">{{ item.memory_id }}</td>
            <td>{{ item.actor }}</td>
            <td>{{ formatAuditPayload(item) }}</td>
            <td>{{ item.created_at }}</td>
          </tr>
          <tr v-if="auditItems.length === 0">
            <td colspan="5" class="empty-cell">暂无审计记录</td>
          </tr>
        </tbody>
      </v-table>
    </v-sheet>

    <v-snackbar v-model="snackbar.show" :color="snackbar.color" timeout="3500">
      {{ snackbar.text }}
    </v-snackbar>
  </div>
</template>

<style scoped>
.memory-governance-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  color: rgb(var(--v-theme-on-surface));
}

.page-toolbar,
.panel-heading,
.action-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
}

.page-toolbar h1,
.panel-heading h2 {
  margin: 0;
  font-weight: 700;
  letter-spacing: 0;
}

.page-toolbar h1 {
  font-size: 28px;
}

.page-toolbar p {
  margin: 4px 0 0;
  color: rgb(var(--v-theme-on-surface-variant));
}

.status-grid {
  display: grid;
  grid-template-columns: minmax(180px, 1.2fr) repeat(4, minmax(132px, 1fr));
  gap: 12px;
}

.metric-panel,
.action-panel,
.path-panel,
.table-panel {
  padding: 16px;
  background: rgb(var(--v-theme-surface));
}

.metric-panel {
  display: flex;
  align-items: center;
  gap: 12px;
  min-height: 96px;
}

.total-panel {
  align-items: flex-start;
  flex-direction: column;
}

.metric-label {
  color: rgb(var(--v-theme-on-surface-variant));
  font-size: 13px;
}

.metric-value {
  font-size: 32px;
  font-weight: 700;
  line-height: 1.1;
}

.metric-value.compact {
  font-size: 24px;
}

.workbench-grid {
  display: grid;
  grid-template-columns: minmax(320px, 0.9fr) minmax(360px, 1.1fr);
  gap: 12px;
}

.action-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.action-copy {
  color: rgb(var(--v-theme-on-surface-variant));
  font-size: 13px;
  line-height: 1.5;
}

.path-key {
  width: 120px;
  color: rgb(var(--v-theme-on-surface-variant));
  font-weight: 600;
}

.path-value,
.mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  word-break: break-all;
}

.empty-cell {
  color: rgb(var(--v-theme-on-surface-variant));
  padding: 24px 0;
  text-align: center;
}

@media (max-width: 1100px) {
  .status-grid,
  .workbench-grid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 720px) {
  .page-toolbar,
  .panel-heading,
  .action-row {
    align-items: stretch;
    flex-direction: column;
  }
}
</style>
