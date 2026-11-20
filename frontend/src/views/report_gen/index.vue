<template>
  <section class="page" data-module="report_gen">
    <header class="page-head">
      <div>
        <h2>报表生成管理</h2>
        <p class="page-desc">维护分析报表的模板定义、数据范围、生成周期和多渠道分发配置。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">登记分析报表</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出报表生成清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <section class="diff-sync" data-testid="diff-sync-panel">
      <header class="diff-sync-head">
        <h3>已确认版本差异报告</h3>
        <button class="btn" data-testid="diff-sync-refresh" type="button" @click="loadPublishedReports">刷新已确认数据</button>
      </header>
      <p class="page-desc">只同步已发布报告冻结的两侧版本与阈值，汇总超阈值触点和金额，不混入其他归因版本的单侧数据。</p>
      <table v-if="publishedReports.length" class="data-table">
        <thead>
          <tr>
            <th v-for="column in diffColumns" :key="column">{{ column }}</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="report in publishedReports"
            :key="String(report.id)"
            :data-testid="`diff-report-row-${report.id}`"
          >
            <td v-for="column in diffColumns" :key="column">{{ formatDiffCell(report, column) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-else class="empty-state">暂无可引用的已发布版本差异报告</p>
    </section>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无报表生成数据，可先登记分析报表</td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记分析报表</h3>
        <label v-for="field in columns" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条报表生成记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type DiffReportRow = Record<string, string | number | string[] | null>

const ENDPOINT = '/api/report_gen'
const PUBLISHED_DIFF_ENDPOINT = '/api/version_diff_report/published'
const columns = ["报表编号", "报表类型", "数据范围", "包含图表", "生成周期", "输出格式", "分发渠道", "下次生成"]
const actions = ["提交生成", "确认分发", "停用报表"]
const statuses = ["草稿", "待生成", "已生成", "已停用"]
const metricNames = ["报表总数", "已生成报表", "待生成报表"]
const diffColumns = ["报告编号", "基准版本", "对照版本", "差异阈值", "超阈值触点", "超阈值触点数", "超阈值金额", "published_at"]

const rows = ref<Row[]>([])
const publishedReports = ref<DiffReportRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: metricNames[0], value: total.value },
  { label: metricNames[1], value: rows.value.filter((row) => row.status === statuses[1]).length },
  { label: metricNames[2], value: rows.value.filter((row) => row.status === statuses[statuses.length - 1]).length },
])

function formatDiffCell(report: DiffReportRow, column: string): string {
  const value = report[column]
  if (Array.isArray(value)) return value.length ? value.join('、') : '—'
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

async function loadPublishedReports() {
  errorMessage.value = ''
  try {
    const response = await request(PUBLISHED_DIFF_ENDPOINT)
    if (!response.ok) throw new Error('已确认版本差异报告读取失败')
    const payload = await response.json()
    publishedReports.value = (payload.items ?? []) as DiffReportRow[]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '已确认版本差异报告读取失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value).toString()
  try {
    const response = await request(`${ENDPOINT}/export?${query}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'report_gen.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(columns.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const response = await request(ENDPOINT, {
      method: 'POST', body: JSON.stringify(createValues.value),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '登记失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('报表生成动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '报表生成操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('分析报表列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '报表生成列表读取失败'
  }
}

onMounted(() => {
  void reload()
  void loadPublishedReports()
})
</script>

<style scoped>
.diff-sync {
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 12px;
  margin-bottom: 14px;
}
.diff-sync-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 6px;
}
.diff-sync-head h3 {
  margin: 0;
  font-size: 15px;
}
</style>
