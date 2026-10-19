const API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000/api/v1'

export class ApiError extends Error {
  constructor(message, status = 0, code = 'network_error') {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

async function request(path, options = {}) {
  let response
  try {
    response = await fetch(`${API_BASE}${path}`, options)
  } catch (error) {
    throw new ApiError('暂时无法连接，请检查网络或重新启动应用。', 0, 'network_error')
  }
  if (!response.ok) {
    let detail = ''
    let code = 'http_error'
    try {
      const body = await response.json()
      const payload = body?.detail || body?.error || body
      detail = payload?.message || (typeof payload === 'string' ? payload : '')
      code = payload?.code || code
    } catch {
      // The response may be empty or non-JSON.
    }
    throw new ApiError(detail || `请求未完成（${response.status}）`, response.status, code)
  }
  return response
}

export async function jsonRequest(path, options) {
  return request(path, options).then(response => response.json())
}

// 只读的运行时用量（本小时共享的模型调用额度）。给「开始研究／分析资料」之前看的
// 那一条提示用，见 ``components/ModelBudgetNotice.jsx``。
export const runtimeApi = {
  modelBudget: () => jsonRequest('/runtime/model-budget'),
}

export const taskApi = {
  reportLibrary: () => jsonRequest('/reports'),
  // 历史报告页的「删除所选」删的是这些报告所属的**整项任务**，走下面的
  // ``/tasks/delete``。这个端点只删报告、保留任务，目前界面上没有入口；留着是
  // 为了让前端封装与后端路由一一对应。
  deleteReports: reportIds => jsonRequest('/reports/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ report_ids: reportIds }) }),
  // The exported archive can be tens of MB; Response.blob() rejects with
  // "Failed to fetch" at that size in Chromium, so build the Blob from a buffer.
  exportReports: reportIds => request('/reports/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ report_ids: reportIds }) })
    .then(async response => new Blob([await response.arrayBuffer()], { type: response.headers.get('Content-Type') || 'application/zip' })),
  deleteTasks: taskIds => jsonRequest('/tasks/delete', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ task_ids: taskIds }) }),
  exportTasks: taskIds => request('/tasks/export', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ task_ids: taskIds }) })
    .then(async response => new Blob([await response.arrayBuffer()], { type: response.headers.get('Content-Type') || 'application/zip' })),
  chat: (taskId, message, history = []) => jsonRequest(`/tasks/${taskId}/chat`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, history: history.slice(-8) }),
  }),
  create: scenario => jsonRequest('/tasks', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scenario }),
  }),
  list: () => jsonRequest('/tasks'),
  get: taskId => jsonRequest(`/tasks/${taskId}`),
  confirmRequirement: (taskId, requirement) => jsonRequest(`/tasks/${taskId}/requirements/confirm`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requirement }),
  }),
  upload: (taskId, file, category = 'other') => {
    const form = new FormData()
    form.append('file', file)
    form.append('category', category)
    return jsonRequest(`/tasks/${taskId}/documents`, { method: 'POST', body: form })
  },
  runDocumentOCR: (taskId, documentId) => jsonRequest(`/tasks/${taskId}/documents/${documentId}/ocr`, { method: 'POST' }),
  candidates: taskId => jsonRequest(`/tasks/${taskId}/candidates`, { method: 'POST' }),
  deepseekAnalysis: taskId => jsonRequest(`/tasks/${taskId}/analysis/deepseek`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({}) }),
  getDeepseekAnalysis: taskId => jsonRequest(`/tasks/${taskId}/analysis/deepseek`),
  decideDeepseekAnalysis: (taskId, decisions) => jsonRequest(`/tasks/${taskId}/analysis/deepseek/decisions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(decisions) }),
  decideCandidate: (taskId, candidateId, status, note) => jsonRequest(`/tasks/${taskId}/candidates/${candidateId}/decision`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ status, note }) }),
  search: (taskId, payload = {}) => jsonRequest(`/tasks/${taskId}/search`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  searchPlan: (taskId, payload = {}) => jsonRequest(`/tasks/${taskId}/search/plan`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  latestSearchPlan: taskId => jsonRequest(`/tasks/${taskId}/search/plan`),
  confirmSearchPlan: (taskId, planId) => jsonRequest(`/tasks/${taskId}/search/plan/${planId}/confirm`, { method: 'POST' }),
  searchRuns: taskId => jsonRequest(`/tasks/${taskId}/search`),
  discoverWebCandidates: (taskId, payload = {}) => jsonRequest(`/tasks/${taskId}/search/discover-candidates`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  sourceDecisions: (taskId, decisions) => jsonRequest(`/tasks/${taskId}/sources/decisions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(decisions) }),
  sourceDecision: (taskId, url, status) => jsonRequest(`/tasks/${taskId}/sources/decisions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify([{ url, status }]) }),
  finalDecision: taskId => jsonRequest(`/tasks/${taskId}/final-decision`),
  saveFinalDecision: (taskId, payload) => jsonRequest(`/tasks/${taskId}/final-decision`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  inquiries: taskId => jsonRequest(`/tasks/${taskId}/supplier-inquiries`),
  createInquiries: (taskId, items) => jsonRequest(`/tasks/${taskId}/supplier-inquiries`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ items }) }),
  replies: taskId => jsonRequest(`/tasks/${taskId}/supplier-replies`),
  createReply: (taskId, inquiryId, payload) => jsonRequest(`/tasks/${taskId}/supplier-inquiries/${inquiryId}/replies`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  reviewReply: (taskId, replyId, payload) => jsonRequest(`/tasks/${taskId}/supplier-replies/${replyId}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  factDraft: (taskId, replyId, payload) => jsonRequest(`/tasks/${taskId}/supplier-replies/${replyId}/fact-drafts`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  reviewFact: (taskId, factId, payload) => jsonRequest(`/tasks/${taskId}/facts/${factId}/review`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  archives: taskId => jsonRequest(`/tasks/${taskId}/archives`),
  createArchive: (taskId, note) => jsonRequest(`/tasks/${taskId}/archive`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ note }) }),
  restoreArchive: (taskId, archiveId) => jsonRequest(`/tasks/${taskId}/restore`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ archive_id: archiveId }) }),
  ragRebuild: taskId => jsonRequest(`/tasks/${taskId}/rag/rebuild`, { method: 'POST' }),
  ragQuery: (taskId, text) => jsonRequest(`/tasks/${taskId}/rag/query`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }) }),
  facts: taskId => jsonRequest(`/tasks/${taskId}/facts`),
  webFact: (taskId, payload) => jsonRequest(`/tasks/${taskId}/sources/fact-drafts`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  evaluate: (taskId, payload) => jsonRequest(`/tasks/${taskId}/evaluate`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  recommend: (taskId, payload) => jsonRequest(`/tasks/${taskId}/recommend`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  compare: (taskId, payload) => jsonRequest(`/tasks/${taskId}/compare`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  questions: (taskId, payload) => jsonRequest(`/tasks/${taskId}/questions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) }),
  reports: taskId => jsonRequest(`/tasks/${taskId}/reports`),
  createReport: (taskId, payload) => request(`/tasks/${taskId}/reports`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }).then(response => response.json()),
  history: taskId => jsonRequest(`/tasks/${taskId}/history`),
}

export async function providerHealthCheck(providerId, payload) {
  try {
    return await jsonRequest(`/providers/${providerId}/health-check`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  } catch (error) {
    if (error.status === 404 || error.code === 'network_error') {
      return { status: 'unavailable', label: '服务未连接' }
    }
    throw error
  }
}

export async function fetchProviderStatus() {
  try {
    return await jsonRequest('/providers')
  } catch (error) {
    if (error.status === 404 || error.code === 'network_error') {
      return { status: 'unavailable', label: '服务未连接', items: [] }
    }
    throw error
  }
}

export function formatConstraintValue(value) {
  if (value === null || value === undefined || value === '') return '未说明'
  if (typeof value !== 'object') return String(value)
  if (value.amount !== undefined) return `${value.amount}${value.currency ? ` ${value.currency}` : ''}`
  if (value.value !== undefined && typeof value.value !== 'object') return String(value.value)
  if (value.raw || value.source_text) return String(value.raw || value.source_text)
  if (value.per_day !== undefined) return `每天 ${value.per_day} ${value.unit === 'plates' ? '块板' : '个样品'}`
  if (value.per_run !== undefined) return `每次 ${value.per_run} 个样品`
  if (value.min_c !== undefined) return `${value.min_c}–${value.max_c} ℃`
  return '请补充具体条件'
}

export function formatConstraints(items, emptyLabel = '暂未识别') {
  if (!Array.isArray(items) || items.length === 0) return emptyLabel
  return items.map(item => {
    if (!item || typeof item !== 'object') return String(item)
    const rawLabel = item.name || item.label
    const label = ({ budget_max: '预算上限', throughput: '样品处理量', space_m2: '场地面积', power: '电源', temperature_range: '温度范围', environment_requirement: '环境要求', capability: '功能要求' })[rawLabel] || (/[\u3400-\u9fff]/.test(rawLabel || '') ? rawLabel : '')
    const value = formatConstraintValue(item.value)
    const source = item.source_text && item.source_text !== value ? `（原文：${item.source_text}）` : ''
    return `${label ? `${label}：` : ''}${value}${source}`
  }).join('；')
}

export function getTaskStage(task, requirement, documents, candidates, reports) {
  if (!task) return 'draft'
  if (reports.length) return 'report'
  if (candidates.length) return 'candidate'
  if (documents.length) return 'documents'
  if (requirement?.confirmed_at) return 'confirmed'
  return 'requirement'
}
