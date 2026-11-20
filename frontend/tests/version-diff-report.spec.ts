import { expect, test } from '@playwright/test'

const ENDPOINT = '/version_diff_report'

type ActionEnvelope = { ok: boolean; message: string; entry: Record<string, any> }

test('版本差异报告：生成、更正、发布、同步、两版下载复算全链路', async ({ page }) => {
  test.setTimeout(120_000)
  const browserErrors: string[] = []
  page.on('pageerror', (e) => browserErrors.push(e.message))
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 400) browserErrors.push(`${r.status()} ${r.url()}`)
  })

  await page.goto(ENDPOINT)
  await expect(page.locator('[data-module="version_diff_report"] h2')).toHaveText('版本差异报告')

  // ---- 生成 V1：基准 100 / 对照 115 / 阈值 10，差异 15 应判超阈值 ----
  await page.getByTestId('open-create').click()
  await expect(page.getByTestId('create-modal')).toBeVisible()
  await page.getByTestId('create-compare_job_id').fill('1')
  await page.getByTestId('create-基准版本').fill('首次归因-v1.0')
  await page.getByTestId('create-对照版本').fill('末次归因-v2.0')
  await page.getByTestId('create-差异阈值').fill('10')
  await page.getByTestId('touch-name-0').fill('搜索广告')
  await page.getByTestId('touch-base-0').fill('100')
  await page.getByTestId('touch-control-0').fill('115')
  await page.getByTestId('submit-create').click()
  await expect(page.getByTestId('create-modal')).toHaveCount(0)

  const firstRow = page.locator('tbody tr[data-testid^="row-"]').first()
  await expect(firstRow).toHaveAttribute('data-status', '草稿')
  await expect(firstRow).toContainText('超阈值')

  const reportIdV1 = (await firstRow.getAttribute('data-testid'))!.replace('row-', '')

  // ---- 查看明细：超阈值结论可回到两侧实际金额 ----
  await firstRow.getByRole('button', { name: '查看明细', exact: true }).click()
  await expect(page.getByTestId('detail-modal')).toBeVisible()
  const detailTouch = page.getByTestId('detail-touch-0')
  await expect(detailTouch).toContainText('100')
  await expect(detailTouch).toContainText('115')
  await expect(detailTouch).toContainText('15')
  await expect(detailTouch).toContainText('超阈值')
  await page.getByTestId('detail-modal').getByRole('button', { name: '关闭' }).click()

  // ---- 确认前更正基准为 112，差异变 3，不再超阈值 ----
  await firstRow.getByRole('button', { name: '更正基准', exact: true }).click()
  await expect(page.getByTestId('correct-modal')).toBeVisible()
  await page.getByTestId('correct-amount').fill('112')
  await page.getByTestId('submit-correct').click()
  await expect(page.getByTestId('correct-modal')).toHaveCount(0)
  await expect(firstRow).toContainText('未超阈值')

  // ---- 确认发布（动作按钮随状态切换）----
  await firstRow.getByRole('button', { name: '确认发布', exact: true }).click()
  await expect(firstRow).toHaveAttribute('data-status', '已发布')
  await expect(firstRow).toContainText('已发布')

  // ---- 同步到报表生成 ----
  const syncResponse = page.waitForResponse((r) => r.url().includes('/actions') && r.status() === 200)
  await firstRow.getByRole('button', { name: '同步报表', exact: true }).click()
  await syncResponse
  await expect(firstRow).toBeVisible()

  // ---- 不同输入再次提交生成 V2 ----
  await page.getByTestId('open-create').click()
  await page.getByTestId('create-compare_job_id').fill('1')
  await page.getByTestId('create-基准版本').fill('首次归因-v1.1')
  await page.getByTestId('create-对照版本').fill('末次归因-v2.1')
  await page.getByTestId('create-差异阈值').fill('8')
  await page.getByTestId('touch-name-0').fill('搜索广告')
  await page.getByTestId('touch-base-0').fill('200')
  await page.getByTestId('touch-control-0').fill('210')
  await page.getByTestId('add-touch').click()
  await page.getByTestId('touch-name-1').fill('推荐流')
  await page.getByTestId('touch-base-1').fill('60')
  await page.getByTestId('touch-control-1').fill('61')
  await page.getByTestId('submit-create').click()
  await expect(page.getByTestId('create-modal')).toHaveCount(0)
  await expect(page.locator('tbody tr[data-testid^="row-"]')).toHaveCount(2)

  const secondRow = page.locator('tbody tr[data-testid^="row-"]').nth(1)
  const reportIdV2 = (await secondRow.getAttribute('data-testid'))!.replace('row-', '')
  await secondRow.getByRole('button', { name: '确认发布', exact: true }).click()
  await expect(secondRow).toHaveAttribute('data-status', '已发布')

  // ---- 下载两版文件并按文件内两侧金额与阈值各自复算 ----
  const downloaded = new Map<string, string>()
  for (const row of [firstRow, secondRow]) {
    const downloadPromise = page.waitForEvent('download')
    await row.getByRole('button', { name: '下载报告', exact: true }).click()
    const download = await downloadPromise
    const filename = download.suggestedFilename()
    expect(filename).toMatch(/^VDR-1-V\d+\.csv$/)
    downloaded.set(filename, await download.path().then((p) => p || ''))
  }
  expect(downloaded.size).toBe(2)

  const parseCsv = (text: string): string[][] =>
    text.replace(/^﻿/, '').trim().split('\n').map((line) => line.split(','))
  const files = [...downloaded.entries()]
  const v1File = files.find(([name]) => name.includes('V1.'))!
  const v2File = files.find(([name]) => name.includes('V2.'))!

  for (const [, path] of [v1File, v2File]) {
    const fs = await import('node:fs/promises')
    const text = await fs.readFile(path, 'utf-8')
    const rows = parseCsv(text)
    const detail = rows.filter((r) => r.length === 7).slice(1)
    expect(detail.length).toBeGreaterThan(0)
    for (const d of detail) {
      const base = Number(d[1])
      const control = Number(d[2])
      const diff = Number(d[3])
      const threshold = Number(d[4])
      const calc = Math.abs(control - base)
      const classification = calc > threshold ? '超阈值' : '未超阈值'
      expect(calc).toBe(diff)
      expect(d[5]).toBe(classification)
    }
  }

  // V1 文件应保留更正后的 112 且未超阈值；V2 保留 200/210 且搜索广告超阈值
  const fs = await import('node:fs/promises')
  const v1Text = await fs.readFile(v1File[1], 'utf-8')
  const v2Text = await fs.readFile(v2File[1], 'utf-8')
  expect(v1Text).toContain('112')
  expect(v1Text).toContain('首次归因-v1.0')
  expect(v2Text).toContain('200')
  expect(v2Text).toContain('超阈值')

  // ---- 已发布查询接口只返回正式报告，且两侧版本各自独立 ----
  const publishedResp = await page.request.get('/api/version_diff_report/published')
  const published = (await publishedResp.json()) as { items: Record<string, any>[] }
  const ids = published.items.map((i) => i.id)
  expect(ids).toContain(Number(reportIdV1))
  expect(ids).toContain(Number(reportIdV2))
  for (const item of published.items) {
    expect(item['状态']).toBe('已发布')
    expect(item['基准版本']).toContain('首次归因')
  }

  // ---- 报表生成入口同步展示已确认数据与超阈值汇总 ----
  await page.goto('/report_gen')
  const panel = page.getByTestId('diff-sync-panel')
  await expect(panel).toBeVisible()
  await expect(panel.locator('tbody tr')).toHaveCount(2)
  const panelText = await panel.innerText()
  expect(panelText).toContain('VDR-1-V1')
  expect(panelText).toContain('VDR-1-V2')
  expect(panelText).toContain('首次归因-v1.0')
  expect(panelText).toContain('末次归因-v2.1')

  // ---- 重跑上游后，已发布报告历史金额保持原组合 ----
  const rerun = await page.request.post(
    `/api/version_diff_report/${reportIdV1}/actions`,
    { data: { action: '重跑上游' }, headers: { 'Content-Type': 'application/json' } },
  )
  const rerunBody = (await rerun.json()) as ActionEnvelope
  expect(rerunBody.ok).toBe(true)
  expect(rerunBody.entry.snapshot_unchanged).toBe(true)

  expect(browserErrors).toEqual([])
})

test('草稿报告不能发布半份对比，且不会出现在已发布入口', async ({ page }) => {
  test.setTimeout(60_000)
  const browserErrors: string[] = []
  page.on('pageerror', (e) => browserErrors.push(e.message))

  await page.goto(ENDPOINT)
  await page.getByTestId('open-create').click()
  await page.getByTestId('create-compare_job_id').fill('2')
  await page.getByTestId('create-差异阈值').fill('10')
  await page.getByTestId('touch-name-0').fill('直播')
  await page.getByTestId('touch-base-0').fill('50')
  await page.getByTestId('touch-control-0').fill('80')
  await page.getByTestId('submit-create').click()

  const row = page.locator('tbody tr[data-testid^="row-"]').first()
  const reportId = (await row.getAttribute('data-testid'))!.replace('row-', '')

  // 直接调接口制造单侧缺失，再尝试发布，应被拒绝
  await page.request.post(`/api/version_diff_report/${reportId}/actions`, {
    data: { action: '更正对照', 触点类型: '单侧触点', 金额: 9 },
    headers: { 'Content-Type': 'application/json' },
  })
  const blocked = await page.request.post(`/api/version_diff_report/${reportId}/actions`, {
    data: { action: '确认发布' },
    headers: { 'Content-Type': 'application/json' },
  })
  const body = (await blocked.json()) as ActionEnvelope
  expect(body.ok).toBe(false)
  expect(body.message).toContain('半份对比')

  const published = await (await page.request.get('/api/version_diff_report/published')).json()
  expect(published.items.every((i: Record<string, any>) => i.id !== Number(reportId))).toBe(true)

  expect(browserErrors).toEqual([])
})
