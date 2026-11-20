<template>
  <section class="page" data-module="version_diff_report">
    <header class="page-head">
      <div>
        <h2>版本差异报告</h2>
        <p class="page-desc">按触点对齐两侧归因版本金额，计算差异并按阈值确认发布；正式报告冻结两侧完整版本与阈值，供报表生成引用同一份已确认结果。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">生成版本差异报告</button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">导出报告汇总</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label class="filter-item">
        <span>状态</span>
        <select v-model="filters.status" data-testid="filter-status">
          <option value="">全部</option>
          <option v-for="s in statuses" :key="s" :value="s">{{ s }}</option>
        </select>
      </label>
      <label class="filter-item">
        <span>对比任务 id</span>
        <input v-model="filters.compare_job_id" data-testid="filter-compare_job_id" placeholder="如 1" />
      </label>
      <label class="filter-item">
        <span>报告编号</span>
        <input v-model="filters.keyword" data-testid="filter-keyword" placeholder="按报告编号检索" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in columns" :key="column">{{ formatCell(row, column) }}</td>
          <td class="row-actions">
            <button class="link" type="button" :data-testid="`action-${row.id}-查看明细`" @click="openDetail(row)">查看明细</button>
            <button
              v-for="action in availableActions(row)"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="handleAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无版本差异报告，可先基于对比任务生成</td>
        </tr>
      </tbody>
    </table>

    <!-- 生成报告 -->
    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>生成版本差异报告</h3>
        <label class="create-field" v-for="field in metaFields" :key="field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" :required="field === 'compare_job_id'" />
        </label>
        <div class="touch-head">
          <span>触点金额（按触点对齐两侧版本）</span>
          <button class="btn" type="button" data-testid="add-touch" @click="addTouch">添加触点</button>
        </div>
        <div
          v-for="(touch, index) in touchRows"
          :key="index"
          class="touch-row"
          :data-testid="`touch-row-${index}`"
        >
          <input v-model="touch.触点类型" :data-testid="`touch-name-${index}`" placeholder="触点类型" required />
          <input v-model="touch.基准金额" :data-testid="`touch-base-${index}`" placeholder="基准金额" required />
          <input v-model="touch.对照金额" :data-testid="`touch-control-${index}`" placeholder="对照金额" required />
          <button class="link" type="button" @click="removeTouch(index)">移除</button>
        </div>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '生成草稿' }}
          </button>
        </div>
      </form>
    </div>

    <!-- 触点明细 -->
    <div v-if="detailRow" class="modal-backdrop" data-testid="detail-modal" @click.self="detailRow = null">
      <div class="modal">
        <h3>{{ detailRow.报告编号 }} · 触点明细</h3>
        <p class="page-desc">
          两侧版本：{{ detailRow.基准版本 }} 对比 {{ detailRow.对照版本 }} ｜ 差异阈值：{{ detailRow.差异阈值 }}
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th v-for="h in detailColumns" :key="h">{{ h }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(item, index) in detailRow.触点明细"
              :key="String(item.触点类型)"
              :data-testid="`detail-touch-${index}`"
            >
              <td v-for="h in detailColumns" :key="h">{{ item[h] ?? '—' }}</td>
            </tr>
          </tbody>
        </table>
        <div class="page-actions modal-foot">
          <button class="btn" type="button" @click="detailRow = null">关闭</button>
        </div>
      </div>
    </div>

    <!-- 更正金额 -->
    <div v-if="correctRow" class="modal-backdrop" data-testid="correct-modal" @click.self="correctRow = null">
      <form class="modal" @submit.prevent="submitCorrect">
        <h3>{{ correctSide === '基准' ? '更正基准金额' : '更正对照金额' }}（仅草稿可改，提交后自动重算）</h3>
        <label class="create-field">
          <span>触点类型</span>
          <select v-model="correctForm.触点类型" data-testid="correct-touch">
            <option v-for="item in correctRow.触点明细" :key="String(item.触点类型)" :value="item.触点类型">
              {{ item.触点类型 }}
            </option>
          </select>
        </label>
        <label class="create-field">
          <span>更正后金额</span>
          <input v-model="correctForm.金额" data-testid="correct-amount" type="number" step="0.01" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="correctRow = null">取消</button>
          <button class="btn primary" data-testid="submit-correct" type="submit">确认更正并重算</button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 份版本差异报告</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { request } from '@/api/client'

type TouchDetail = Record<string, string | number | boolean | null>
type ReportRow = Record<string, string | number | boolean | null | TouchDetail[]> & {
  触点明细?: TouchDetail[]
}

const ENDPOINT = '/api/version_diff_report'
const columns = [
  '报告编号', '版本号', '状态', '基准版本', '对照版本', '差异阈值',
  '触点总数', '超阈值触点数', '超阈值金额', '基准金额合计', '对照金额合计', 'published_at',
]
const detailColumns = ['触点类型', '基准金额', '对照金额', '差异', '差异分类', '单侧缺失']
const metaFields = ['compare_job_id', '基准版本', '对照版本', '差异阈值', '链路编号']
const statuses = ['草稿', '已发布']
const draftActions = ['更正基准', '更正对照', '确认发布', '下载报告']
const publishedActions = ['同步报表', '重跑上游', '下载报告']

const rows = ref<ReportRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({ status: '', compare_job_id: '', keyword: '' })
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const touchRows = ref<Array<Record<string, string>>>([])
const detailRow = ref<ReportRow | null>(null)
const correctRow = ref<ReportRow | null>(null)
const correctSide = ref<'基准' | '对照'>('基准')
const correctForm = ref<Record<string, string>>({ 触点类型: '', 金额: '' })

const stats = computed(() => [
  { label: '报告总数', value: total.value },
  { label: '草稿报告', value: rows.value.filter((row) => row.status === '草稿').length },
  { label: '已发布报告', value: rows.value.filter((row) => row.status === '已发布').length },
  {
    label: '超阈值触点合计',
    value: rows.value.reduce((sum, row) => sum + Number(row.超阈值触点数 ?? 0), 0),
  },
])

function availableActions(row: ReportRow): string[] {
  return row.status === '已发布' ? publishedActions : draftActions
}

function formatCell(row: ReportRow, column: string): string {
  if (column === 'published_at') return row.published_at ? String(row.published_at) : '—'
  const value = row[column]
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

function resetFilters() {
  filters.value = { status: '', compare_job_id: '', keyword: '' }
  void reload()
}

function buildQuery(): string {
  const params = new URLSearchParams()
  if (filters.value.status) params.set('status', filters.value.status)
  if (filters.value.compare_job_id) params.set('compare_job_id', filters.value.compare_job_id)
  if (filters.value.keyword) params.set('keyword', filters.value.keyword)
  return params.toString()
}

async function reload() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}?${buildQuery()}`)
    if (!response.ok) throw new Error('版本差异报告列表读取失败')
    const payload = await response.json()
    rows.value = (payload.items ?? []) as ReportRow[]
    total.value = payload.total ?? rows.value.length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '版本差异报告列表读取失败'
  }
}

async function exportRows() {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/export?${buildQuery()}`)
    if (!response.ok) throw new Error('导出失败，请稍后重试')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = 'version_diff_reports.csv'
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

async function downloadReport(row: ReportRow) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/export`)
    if (!response.ok) throw new Error('报告文件下载失败')
    const url = URL.createObjectURL(await response.blob())
    const link = document.createElement('a')
    link.href = url
    link.download = `${String(row.报告编号)}.csv`
    link.click()
    URL.revokeObjectURL(url)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '报告文件下载失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(metaFields.map((field) => [field, '']))
  touchRows.value = [{ 触点类型: '', 基准金额: '', 对照金额: '' }]
  errorMessage.value = ''
  showCreate.value = true
}

function addTouch() {
  touchRows.value.push({ 触点类型: '', 基准金额: '', 对照金额: '' })
}

function removeTouch(index: number) {
  touchRows.value.splice(index, 1)
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const body: Record<string, unknown> = { ...createValues.value, touches: touchRows.value }
    const response = await request(ENDPOINT, { method: 'POST', body: JSON.stringify(body) })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '生成失败')
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '生成失败'
  } finally {
    creating.value = false
  }
}

function openDetail(row: ReportRow) {
  detailRow.value = row
}

function openCorrect(row: ReportRow, side: '基准' | '对照') {
  correctRow.value = row
  correctSide.value = side
  correctForm.value = { 触点类型: row.触点明细?.[0]?.触点类型 as string ?? '', 金额: '' }
}

async function submitCorrect() {
  if (!correctRow.value) return
  const action = correctSide.value === '基准' ? '更正基准' : '更正对照'
  try {
    const response = await request(`${ENDPOINT}/${correctRow.value.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action, ...correctForm.value }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '更正失败')
    correctRow.value = null
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '更正失败'
  }
}

async function handleAction(action: string, row: ReportRow) {
  errorMessage.value = ''
  if (action === '查看明细') {
    openDetail(row)
    return
  }
  if (action === '更正基准') {
    openCorrect(row, '基准')
    return
  }
  if (action === '更正对照') {
    openCorrect(row, '对照')
    return
  }
  if (action === '下载报告') {
    await downloadReport(row)
    return
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    const payload = await response.json()
    if (!response.ok || !payload.ok) throw new Error(payload.message || '动作执行失败')
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '动作执行失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.touch-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin: 12px 0 6px;
  font-size: 13px;
}
.touch-row {
  display: grid;
  grid-template-columns: 2fr 1fr 1fr auto;
  gap: 8px;
  margin-bottom: 8px;
}
.touch-row input {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
}
.modal-foot {
  margin-top: 14px;
}
select {
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 8px;
}
</style>
