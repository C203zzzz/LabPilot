import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const read = rel => readFileSync(new URL(rel, import.meta.url), 'utf8')

test('Phase 4 API wrappers expose diagnostics, knowledge index, and office export', () => {
  const api = read('../src/services/api.js')
  assert.match(api, /export async function fetchSystemDiagnostics/)
  assert.match(api, /\/system\/diagnostics/)
  assert.match(api, /export const knowledgeApi =/)
  assert.match(api, /\/knowledge\/compare/)
  assert.match(api, /exportReportFile:/)
})

test('AppShell registers PriceComparePage navigation and sidebar diagnostics alert', () => {
  const shell = read('../src/app/AppShell.jsx')
  const main = read('../src/main.jsx')
  assert.match(shell, /to: '\/app\/compare', label: '历史比价库'/)
  assert.match(shell, /sidebar-diag-alert/)
  assert.match(main, /path="compare" element=\{<PriceComparePage \/>\}/)
})

test('ApiSettingsPage renders startup diagnostics panel', () => {
  const pages = read('../src/pages/WorkspacePages.jsx')
  assert.match(pages, /启动与配置自检/)
  assert.match(pages, /silent_degradation_risks/)
  assert.match(pages, /重新自检/)
})

test('ProcurementPanel wires failed lanes to /lanes/<lane>/retry checkpoint endpoint via service helper', () => {
  const api = read('../src/services/api.js')
  const panel = read('../src/pages/ProcurementPanel.jsx')
  assert.match(api, /retryResearchLanePath\s*=.*\/research-runs\/\$\{runId\}\/lanes\/\$\{lane\}\/retry/)
  assert.match(panel, /retryResearchLanePath\(run\.id, 'price'\)/)
  assert.match(panel, /retryResearchLanePath\(run\.id, 'alternatives'\)/)
  assert.match(panel, /重试核价（断点续跑）/)
  assert.match(panel, /重试其他品牌（断点续跑）/)
})

test('PriceComparePage and ReportLibrary expose comparison filters and Word/Excel exports', () => {
  const cmp = read('../src/pages/PriceComparePage.jsx')
  const lib = read('../src/pages/ReportLibrary.jsx')
  assert.match(cmp, /exact_model/)
  assert.match(cmp, /供应商横向对比/)
  assert.match(lib, /导出 Word/)
  assert.match(lib, /导出 Excel/)
  assert.match(lib, /exportReportFile/)
})
