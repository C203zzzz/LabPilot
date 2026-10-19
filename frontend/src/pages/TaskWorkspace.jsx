import React, { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, ArrowRight, Bot, Check, ChevronRight, CircleHelp, ClipboardCheck, Database, Download, FileSearch, FileText, FolderSearch, GitCompare, History, ListChecks, PackageOpen, Paperclip, ReceiptText, RefreshCw, Search, Send, ShieldAlert, Upload, XCircle } from 'lucide-react'
import { useParams, useSearchParams } from 'react-router-dom'
import { ActionButton, EmptyState, InlineNotice, LoadingLabel, PilotBeacon, StatusTag } from '../components/ui'
import ModelBudgetNotice from '../components/ModelBudgetNotice'
import { useTask } from '../features/TaskContext'
import { formatConstraints, formatConstraintValue } from '../services/api'
import { evidencePageLabel, focusCandidates, mergeEvidence, structuredUnparsedRows, userModelText } from '../services/analysisPresentation'
import { pageProgress, pageRouteSummary } from '../services/pageProgress'
import { visionState } from '../services/providerAvailability'
import { candidateName, publicText } from '../services/publicText'
import { reportSummary } from '../services/reportSummary'

import ProcurementPanel from './ProcurementPanel'
import ProcurementIntake from './ProcurementIntake'
import AnalysisProgress from './AnalysisProgress'
import SourceLink, { LinkedText } from '../components/SourceLink'
import ReportSummaryBlock from '../components/ReportSummaryBlock'
import ResearchIssues from '../components/ResearchIssues'

const taskTabs = [
  ['核价与替代', ReceiptText], ['替代产品', GitCompare],
  ['助手', Bot], ['资料', FileText], ['分析', Bot], ['候选', FolderSearch], ['需求', ListChecks], ['联网', Search], ['证据', FileSearch], ['适配', GitCompare], ['推荐与报价', ClipboardCheck], ['追问', Send], ['报告', Download], ['版本', History],
]

const primaryTabs = ['资料', '核价与替代', '替代产品', '报告']
const tabNames = {资料: '材料与报价', 核价与替代: '成交项目核价', 替代产品: '其他品牌比较', 报告: '分析报告'}

const sourceTypeLabels = {
  manufacturer_website: '制造商官网',
  official_source: '官方资料',
  manufacturer_download: '制造商原始资料',
  aggregator: '转载或聚合页面',
  unknown: '来源待核验',
  tender_document: '正式采购文件',
  dealer: '经销商页面',
  search_snippet: '搜索摘要',
}

const documentCategories = [
  { value: 'product', label: '产品资料', description: '说明书、参数表与产品图片', icon: PackageOpen },
  { value: 'quote', label: '报价资料', description: '报价单、价格截图与商务条款', icon: ReceiptText },
  { value: 'other', label: '其他附件', description: '检测材料、补充说明与表格', icon: Paperclip },
]
const documentCategoryLabels = Object.fromEntries(documentCategories.map(item => [item.value, item.label]))

const priceLevelLabels = {
  historical_tender: '历史招标价',
  official_public: '官方公开价',
  dealer_lead: '经销商线索',
  unknown: '价格待核验',
}

const humanLabel = (value, labels) => labels[value] || '待判断'

const reviewLabel = value => ({
  pending_review: '已提取，尚未人工核对',
  confirmed: '已确认',
  rejected: '已驳回',
  eligible: '符合条件',
  ineligible: '不符合条件',
  needs_review: '需要复核',
  unknown: '资料未提供',
  conflicted: '存在冲突',
  no_recommendation: '暂不推荐',
  recommendations: '有可选方案',
}[value] || '待判断')

export default function TaskWorkspace() {
  const { taskId } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const taskState = useTask()
  const { task, requirement, documents, candidates, searchRuns, reports, busy, error, setError, loadTask } = taskState
  const requestedTab = searchParams.get('tab')
  const requestedReport = searchParams.get('report')
  const [tab, setTab] = useState(() => taskTabs.some(([label]) => label === requestedTab) ? requestedTab : '资料')
  const [reportToOpen, setReportToOpen] = useState(requestedReport || '')
  const [loadingTask, setLoadingTask] = useState(true)
  const [composerHost, setComposerHost] = useState(null)

  const openTab = (nextTab, reportId = '') => {
    setTab(nextTab)
    setSearchParams(current => { const params = new URLSearchParams(current); params.set('tab', nextTab); if (reportId) params.set('report', reportId); else params.delete('report'); return params }, { replace: true })
    setReportToOpen(reportId)
  }

  useEffect(() => {
    if (taskTabs.some(([label]) => label === requestedTab)) setTab(requestedTab)
    if (requestedReport) setReportToOpen(requestedReport)
  }, [requestedReport, requestedTab])

  useEffect(() => {
    let active = true
    setLoadingTask(true)
    loadTask(taskId).finally(() => { if (active) setLoadingTask(false) })
    return () => { active = false }
  }, [loadTask, taskId])

  const unknownCount = Array.isArray(requirement?.unknowns) ? requirement.unknowns.length : 0

  if (loadingTask || busy === 'load') return <div className="task-loading"><PilotBeacon state="running" label="正在恢复任务" /></div>

  if (!task) return <div className="task-page task-unavailable-page">
    <header className="task-header">
      <div className="task-title"><span>采购任务</span><h1>任务暂不可用</h1><div><StatusTag tone="danger">暂不可用</StatusTag></div></div>
    </header>
    {error && <InlineNotice tone="error" title="操作没有完成">{error}</InlineNotice>}
    <EmptyState icon={CircleHelp} title="暂时无法读取这项任务" action={<ActionButton variant="secondary" icon={RefreshCw} onClick={() => loadTask(taskId)}>重新读取</ActionButton>}>请检查网络连接后重试，已有任务不会丢失。</EmptyState>
  </div>

  return <div className="task-page conversation-workspace">
    {busy && <OperationProgress busy={busy} progress={taskState.searchProgress} />}
    <header className="task-header">
      <div className="task-title"><h1 title={task?.scenario || ''}>{task?.title || '实验室仪器选型'}</h1><p className="task-identity">品类：{task.category || '尚未识别，请在比较基准中填写'}{task.category_source === 'inferred' ? '（依据资料识别）' : ''}{task.model ? ` · ${task.brand || ''} ${task.model}` : ''}</p><StatusTag tone={task.procurement_status === 'completed' ? 'success' : 'warning'}>{{ready: '采购条件已确认', queued: '准备检索', running: '检索中', partial: '结果不完整', completed: '本轮检索完成', cancelled: '检索已停止', interrupted: '检索已中断', failed: '检索未完成'}[task.procurement_status] || (documents.length ? '已有资料，可核对型号与参数' : '先聊用途、报价和售后条件')}</StatusTag></div>
      <div className="task-summary"><span>{documents.length} 份原始资料</span><span>{reports.length} 份报告</span></div>
    </header>
    <nav className="task-tabs primary-flow" aria-label="四步采购分析流程">{taskTabs.filter(([label]) => primaryTabs.includes(label)).sort(([a],[b]) => primaryTabs.indexOf(a)-primaryTabs.indexOf(b)).map(([label]) => { const step = primaryTabs.indexOf(label) + 1; return <button key={label} type="button" className={tab === label ? 'active' : ''} aria-current={tab === label ? 'step' : undefined} aria-pressed={tab === label} onClick={() => openTab(label)}><span className="task-step-index" aria-hidden="true">{step}</span><span className="task-step-copy"><small>步骤 {step}</small><strong>{tabNames[label]}</strong></span></button> })}</nav>
    <details className="advanced-tools" open={!primaryTabs.includes(tab) ? true : undefined}><summary>手工核验工具{!primaryTabs.includes(tab) ? ` · 当前：${tab}` : ''}</summary><p>需要单独调整需求、核验来源或联系供应商时使用。完成主流程不必逐项操作这些工具。</p><div className="module-actions">{taskTabs.filter(([label]) => !primaryTabs.includes(label)).map(([label]) => <button type="button" className="text-action" key={label} aria-pressed={tab === label} onClick={() => openTab(label)}>{label}</button>)}</div></details>

    {error && <InlineNotice tone="error" title="操作没有完成" action={<button className="text-action" onClick={() => setError('')}>关闭</button>}>{error}</InlineNotice>}
    <div className="workspace-content">
    <ProcurementIntake key={`intake-${taskId}`} composerHost={composerHost} taskId={taskId} analysisId={taskState.analysis?.analysis_id} showConversation={tab === '资料'} onUpload={files => {taskState.uploadFiles(files, 'product'); openTab('资料')}} onUpdated={draft => {window.dispatchEvent(new CustomEvent('labpilot-intake-update', {detail:{taskId,draft}})); if (tab !== '资料') openTab('资料')}} />
    {(tab === '分析' || busy === 'analysis') && <AnalysisProgress key={task.id} taskId={task.id} busy={busy} documents={documents} />}
    <fieldset className="task-surface" disabled={Boolean(busy)}>
      {tab === '核价与替代' && <ProcurementPanel key="price" mode="price" taskState={taskState} openTab={openTab} />}
      {tab === '替代产品' && <ProcurementPanel key="alternatives" mode="alternatives" taskState={taskState} openTab={openTab} />}
      {tab === '助手' && <AgentPanel taskState={taskState} openTab={openTab} />}
      {tab === '需求' && <RequirementPanel taskState={taskState} openTab={openTab} />}
      {tab === '资料' && <><ProcurementPanel key="material" mode="material" taskState={taskState} openTab={openTab} /><details className="material-details"><summary>上传或查看材料（{documents.length} 份）</summary><DocumentsPanel taskState={taskState} openTab={openTab} /></details><details className="material-details" open={Boolean(taskState.analysis?.candidates?.length) || undefined}><summary>从已有资料提取参数与配置</summary><AnalysisPanel taskState={taskState} openTab={openTab} /></details></>}
      {tab === '分析' && <AnalysisPanel taskState={taskState} openTab={openTab} />}
      {tab === '候选' && <CandidatesPanel taskState={taskState} openTab={openTab} />}
      {tab === '联网' && <SearchPanel taskState={taskState} openTab={openTab} />}
      {tab === '证据' && <EvidencePanel taskState={taskState} openTab={openTab} />}
      {tab === '适配' && <EvaluationPanel taskState={taskState} openTab={openTab} />}
      {tab === '推荐与报价' && <RecommendationPanel taskState={taskState} openTab={openTab} />}
      {tab === '追问' && <SupplierPanel taskState={taskState} openTab={openTab} />}
      {tab === '报告' && <ReportsPanel taskState={taskState} openTab={openTab} reportId={reportToOpen} />}
      {tab === '版本' && <VersionsPanel taskState={taskState} openTab={openTab} />}
    </fieldset>
    </div>
    <div className="workspace-composer-host" ref={setComposerHost} />
  </div>
}

function AgentPanel({ taskState, openTab }) {
  const { task, requirement, documents, candidates, busy, confirmRequirement, providerStatus } = taskState
  const modelConnected = providerStatus?.items?.some(item => item.id === 'reasoning' && item.status === 'configured')
  const isRunning = Boolean(busy)
  const [draft, setDraft] = useState('')
  const { messages, sendMessage, chatBusy, chatError } = taskState
  const status = busy === 'confirm' ? '正在确认需求' : busy === 'upload' ? '正在接收资料' : busy === 'candidates' ? '正在识别候选' : busy === 'report-create' ? '正在生成报告' : task ? '等待下一步' : '任务不可用'
  const thinkingStage = busy === 'confirm' ? 1 : busy === 'upload' ? 2 : busy === 'candidates' ? 3 : busy === 'report-create' ? 4 : 0
  const thinkingCopy = busy === 'confirm' ? '正在把已经确认的条件写回任务。' : busy === 'upload' ? '正在上传文件并等待解析服务返回。' : busy === 'candidates' ? '正在从当前任务资料中读取品牌、型号和配置。' : busy === 'report-create' ? '正在整理报告内容，缺少证据的部分会继续保留。' : ''
  const hard = formatConstraints(requirement?.hard_constraints, '尚未识别必须满足的条件')
  const unknowns = formatConstraints(requirement?.unknowns, '当前没有未知项')
  const hasConfirmedProduct = candidates.some(item => item.status === 'confirmed')
  return <div className="agent-layout">
    <section className="conversation-panel">
      <div className="conversation-status">
              <div className="conversation-heading"><span>采购助手</span><h2>整理采购需求</h2></div>
        <div className="conversation-status-meta"><PilotBeacon state={isRunning ? 'running' : task ? 'idle' : 'error'} label={status} /><StatusTag tone={modelConnected ? 'success' : 'prototype'}>{modelConnected ? '思考模型已连接' : '思考模型未连接'}</StatusTag></div>
      </div>
      <article className="agent-message">
        <div className="message-author"><Bot size={16} /><strong>LabPilot</strong><span>任务说明</span></div>
        <div className="message-copy"><p>先核对采购条件，确认后自动查找产品、参数和价格来源。已有说明书或报价单也可以上传补充。</p></div>
        <div className="evidence-chips"><span>事实边界</span><span>保留未知项</span><span>不自动推荐</span></div>
      </article>
      <article className="user-message"><div className="message-author"><strong>采购场景</strong></div><p>{task?.scenario || '场景尚未载入'}</p></article>
      <article className="confirmation-card">
        <h2>{requirement?.status === 'confirmed' ? '采购条件已确认' : '先核对采购条件'}</h2>
        <p><strong>必须满足：</strong>{hard}</p>
        <p><strong>还需补充：</strong>{unknowns}</p>
        <p>确认后自动查找官网参数、公开价格、招标成交记录和同类产品。没有资料也可以开始。</p>
        <div className="module-actions"><ActionButton variant="secondary" onClick={() => openTab('需求')}>编辑采购条件</ActionButton>
        <ActionButton disabled={Boolean(busy)} onClick={confirmRequirement}>{requirement?.status === 'confirmed' ? '按当前条件重新检索' : '确认条件并检索'}</ActionButton>
        {taskState.searchRuns.length > 0 && <ActionButton variant="ghost" onClick={() => openTab('联网')}>查看检索结果</ActionButton>}</div>
      </article>
      <div className="chat-messages" aria-live="polite">{messages.map((message, index) => <article key={index} className={message.role === 'user' ? 'user-message' : 'agent-message'}>{message.role !== 'user' && <span className="msg-avatar" aria-hidden="true"><Bot size={15} /></span>}<strong>{message.role === 'user' ? '你' : 'LabPilot'}</strong><p>{message.content}</p></article>)}</div>
      {chatError && <p role="alert">{chatError}</p>}
      {chatBusy && <div className="chat-messages"><article className="agent-message"><span className="msg-avatar" aria-hidden="true"><Bot size={15} /></span><p role="status"><span className="reply-dots" aria-hidden="true"><i /><i /><i /></span> 正在回复，你可以继续处理资料。</p></article></div>}
      <form className="agent-composer" onSubmit={async event => { event.preventDefault(); if (await sendMessage(draft)) setDraft('') }}>
        <button type="button" className="composer-icon-button" aria-label="前往资料页" onClick={() => openTab('资料')}><Paperclip size={18} /></button>
        <label htmlFor="agent-message" className="visually-hidden">向 LabPilot 输入消息</label>
        <textarea id="agent-message" rows="3" maxLength={4000} value={draft} onChange={event => setDraft(event.target.value)} placeholder={modelConnected ? '例如：每天检测 100 份样品，应该关注哪些参数？' : '请先到连接设置连接思考模型'} disabled={!modelConnected || chatBusy} />
        <ActionButton type="submit" icon={Send} disabled={!modelConnected || chatBusy || !draft.trim()}>发送</ActionButton>
        <small>回答用于解释和讨论。采购条件请在需求页编辑后确认；每次发送只请求一次模型。</small>
      </form>
    </section>
    <aside className="context-panel">
      <div className="context-heading"><h2>采购上下文</h2><StatusTag tone={documents.length ? 'success' : 'warning'}>{documents.length ? '已有资料' : '缺少资料'}</StatusTag></div>
      <dl><div><dt>使用场景</dt><dd>{requirement?.scenario || task?.scenario || '未识别'}</dd></div><div><dt>必须满足的条件</dt><dd>{hard}</dd></div><div className="context-warning"><dt>未知项</dt><dd>{unknowns}</dd></div></dl>
      <div className="next-actions"><h3>下一步</h3><button onClick={() => openTab(documents.length ? '分析' : '资料')}>{documents.length ? <Bot size={16} /> : <Upload size={16} />}{documents.length ? '分析产品资料' : '上传产品资料'}<ArrowRight size={15} /></button><button onClick={() => openTab(hasConfirmedProduct ? '需求' : '候选')} disabled={!documents.length}>{hasConfirmedProduct ? <ListChecks size={16} /> : <Search size={16} />}{hasConfirmedProduct ? '确认筛选条件' : '确认候选设备'}<ArrowRight size={15} /></button></div>
      <div className="boundary-note"><ShieldAlert size={17} /><p>供应商陈述和解析文本仍需复核；没有来源支持的内容不会显示为“已核验”。</p></div>
    </aside>
  </div>
}

function RequirementPanel({ taskState, openTab }) {
  const { requirement, setRequirement, confirmRequirement, busy } = taskState
  const update = patch => setRequirement(current => ({ ...current, ...patch, status: 'draft' }))
  const updateCondition = (group, index, text) => update({ [group]: requirement[group].map((item, i) => {
    if (i !== index) return item
    let value = text
    if (item.name === 'budget_max' && /^\d+(\.\d+)?$/.test(text.trim())) value = { amount: Number(text), currency: 'CNY' }
    if (item.name === 'throughput' && /^\d+$/.test(text.trim())) value = { per_day: Number(text), unit: item.value?.unit || 'samples' }
    return { ...item, name: ['必须满足', '优先考虑'].includes(item.name) ? (text.trim() || item.name) : item.name, value, source_text: text, status: 'edited' }
  }) })
  return <section className="module-panel requirements-module">
    <div className="module-title"><div><h2>采购条件</h2><p>用日常语言填写即可。不确定的内容可以留空，后续再补充。</p></div></div>
    <fieldset className="requirement-editor" disabled={Boolean(busy)}>
      <label>设备用途与使用场景<textarea rows="3" value={requirement?.scenario || ''} onChange={event => update({ scenario: event.target.value })} placeholder="例如：检测培养液吸光度，每天约 100 份样品" /></label>
      {[['hard_constraints', '必须满足', '不满足就不能采购，例如预算不超过 5 万元、支持 600 nm 检测。'], ['soft_constraints', '优先考虑', '可以权衡的偏好，例如占地小、操作方便、售后响应快。']].map(([group, label, help]) => <div key={group}><h3>{label}</h3><p>{help}</p>{(requirement?.[group] || []).map((item, index) => <div className="condition-row" key={index}><label><span>{item.name === 'budget_max' ? '预算上限（元）' : item.name === 'throughput' ? (item.value?.unit === 'plates' ? '每天检测板数' : '每天样品数') : `${label} ${index + 1}`}</span><input value={item.name === 'budget_max' && typeof item.value === 'object' ? item.value.amount : item.name === 'throughput' && item.value?.per_day !== undefined ? item.value.per_day : formatConstraintValue(item.value)} onChange={event => updateCondition(group, index, event.target.value)} /></label><button type="button" className="text-action" onClick={() => update({ [group]: requirement[group].filter((_, i) => i !== index) })}>移除</button></div>)}<button type="button" className="text-action" onClick={() => update({ [group]: [...(requirement?.[group] || []), { name: label, value: '', kind: group === 'hard_constraints' ? 'hard' : 'soft', status: 'edited' }] })}>添加一条{label}的条件</button></div>)}
      <label>还不确定的内容<small>每行一项，例如预算是否含税、交货时间。</small><textarea rows="3" value={(requirement?.unknowns || []).join('\n')} onChange={event => update({ unknowns: event.target.value.split('\n') })} /></label>
    </fieldset>
    <p>确认后将自动检索官网参数、官方公开价格、招标成交记录和同类具体型号。价格与参数仍需核对来源。</p>
    <div className="module-actions"><ActionButton icon={Search} onClick={confirmRequirement} disabled={!requirement?.scenario?.trim() || Boolean(busy)}>确认条件并检索</ActionButton><ActionButton variant="ghost" onClick={() => openTab('联网')}>查看检索结果</ActionButton></div>
  </section>
}

// 资料卡上的识别状态词。页数分区（pageProgress）已经把「还剩几页没读」说全了，
// 所以「视觉识别 2 页 · 待识别 0 页」本身就是"识别完了"，后面再跟一句"视觉识别已完成"
// 是同一件事的第二遍（用户截图指出）。保留信息量更大的分区数字，只在分区说不出来时
// 才补状态词：部分完成/失败/未运行，以及完全没有页数据、分区无从判断的资料。
function ocrStatusText(document) {
  if (document.ocr_status !== 'completed') {
    return document.ocr_status === 'partial' ? '视觉识别部分完成（有页面待处理）'
      : document.ocr_status === 'failed' ? '视觉识别失败' : '视觉识别未运行'
  }
  const pages = document.pages || []
  return pages.length > 0 && pageProgress(pages).pending === 0 ? '' : '视觉识别已完成'
}

function DocumentsPanel({ taskState, openTab }) {
  const { documents, uploadFiles, runDocumentOCR, busy, providerStatus } = taskState
  const vision = visionState(providerStatus)
  const inputRefs = useRef({})
  return <section className="module-panel documents-module">
    <div className="module-title"><div><h2>采购资料</h2><p>先选择资料类别再上传；失败项会保留原因和原始类别。</p></div>{busy === 'upload' && <LoadingLabel>正在上传资料</LoadingLabel>}</div>
    {/* 上传之前先让用户看到本小时还剩多少次模型调用：一份需要逐页视觉分析的 PDF
        实测要约 30 次，默认上限 20 次/小时，传之前不知道就会传到一半被挡住。 */}
    <ModelBudgetNotice />
    <div className="document-upload-grid" aria-label="按类别上传资料">
      {documentCategories.map(({ value, label, description, icon: Icon }) => <div key={value}>
        <label className="document-upload-option native-file-picker"><Icon size={19} aria-hidden="true" /><span><strong>{label}</strong><small>{description}</small></span><Upload size={16} aria-hidden="true" /><input aria-label={`上传${label}`} type="file" multiple disabled={Boolean(busy)} accept=".pdf,.jpg,.jpeg,.png,.webp,.bmp,.tiff,.xlsx,.csv,.txt" onChange={event => { uploadFiles(Array.from(event.target.files), value); event.target.value = '' }} /></label>
      </div>)}
    </div>
    {!documents.length ? <EmptyState icon={Upload} title="还没有采购资料">从上方三个入口选择正确类别。文件会保留名称、页码与解析警告。</EmptyState> : <div className="document-ledger">{documents.map((document, index) => <article key={document.id || document.key || index}>
      <div className="file-sign"><FileText size={18} /><span>{(document.source_name || document.name || 'FILE').split('.').pop()?.toUpperCase()}</span></div>
      <div className="file-main"><strong>{document.source_name || document.name}</strong>{!document.extracted_text && <p>{document.status === '上传中' ? '文件正在提交到解析服务。' : '暂无可显示的文本摘要。'}</p>}<p className="file-progress"><strong>{pageRouteSummary(document.pages)}</strong>{ocrStatusText(document) && <span>{ocrStatusText(document)}</span>}</p><div><span>{documentCategoryLabels[document.category] || '其他附件'}</span></div>{document.error && <small>{document.error}</small>}{document.status === '上传失败' && document.file && <ActionButton variant="ghost" disabled={Boolean(busy)} onClick={() => uploadFiles([document.file], document.category)}>重传此文件</ActionButton>}{document.warnings?.length > 0 && <details><summary>解析提示（{document.warnings.length}）</summary>{document.warnings.map((w, i) => <p key={i}>{w.message}</p>)}</details>} {(document.document_type === 'image' || document.document_type === 'pdf') && document.id && <div className="module-actions"><ActionButton variant="ghost" disabled={!vision.available || busy === `ocr:${document.id}` || busy === 'upload'} title={vision.hint} onClick={() => runDocumentOCR(document.id)}>{busy === `ocr:${document.id}` ? '识别中' : '识别图片内容'}</ActionButton>{!vision.verified && <StatusTag tone={vision.tone}>{vision.label}</StatusTag>}{document.model_extraction?.review_status && <StatusTag tone="warning">{reviewLabel(document.model_extraction.review_status)}</StatusTag>}</div>}</div>
      <StatusTag tone={document.status === '已解析' ? 'success' : document.status === '上传失败' ? 'danger' : 'warning'}>{document.status}</StatusTag>
    </article>)}</div>}{documents.length > 0 && <div className="module-actions next-step-actions"><ActionButton variant="secondary" icon={FolderSearch} onClick={() => openTab('分析')}>分析资料并提取型号</ActionButton></div>}
  </section>
}

function CandidatesPanel({ taskState, openTab }) {
  const { documents, candidates, identifyCandidates, decideCandidate, busy } = taskState
  return <section className="module-panel candidates-module">
    <div className="module-title"><div><h2>候选设备</h2><p>候选只从当前任务资料识别，品牌、型号和配置仍需人工确认。</p></div><ActionButton variant="secondary" icon={Search} disabled={!documents.length || busy === 'candidates'} onClick={identifyCandidates}>{busy === 'candidates' ? '识别中' : '从资料识别'}</ActionButton></div>
    {!documents.length ? <EmptyState icon={FileText} title="缺少可识别资料">先上传包含品牌和完整型号的供应商资料。</EmptyState> : !candidates.length ? <EmptyState icon={FolderSearch} title="尚未识别候选" action={<ActionButton variant="secondary" icon={Search} onClick={identifyCandidates}>开始识别</ActionButton>}>识别为空时不会生成示例设备，也不会用常识补全型号。</EmptyState> : <><div className="candidate-list">{candidates.map((candidate, index) => <article key={candidate.id || index}><div className="candidate-index">{String(index + 1).padStart(2, '0')}</div><div><span>{candidate.brand || '品牌待确认'}</span><h3>{candidate.model || '型号待确认'}</h3><p>{candidate.configuration || '配置未说明'}</p><div className="module-actions"><ActionButton variant="ghost" disabled={busy === 'candidate-decision'} onClick={() => decideCandidate(candidate.id, 'confirmed')}>确认候选</ActionButton><ActionButton variant="ghost" disabled={busy === 'candidate-decision'} onClick={() => decideCandidate(candidate.id, 'excluded')}>排除</ActionButton></div></div><StatusTag tone={candidate.status === 'confirmed' ? 'success' : candidate.status === 'excluded' ? 'danger' : 'warning'}>{candidate.status === 'confirmed' ? '已确认' : candidate.status === 'excluded' ? '已排除' : '待确认'}</StatusTag></article>)}</div>{candidates.some(item => item.status === 'confirmed') && <div className="module-actions next-step-actions"><ActionButton variant="secondary" icon={Bot} onClick={() => openTab('分析')}>进入资料分析</ActionButton></div>}</>}
  </section>
}

function AnalysisPanel({ taskState, openTab }) {
  const { analysis, documents, runAnalysis, decideAnalysis, busy, providerStatus, requirement } = taskState
  const modelConnected = providerStatus?.items?.some(item => item.id === 'reasoning' && item.status === 'configured')
  const hasAnalysis = analysis && analysis.status !== 'not_generated'
  const focus = focusCandidates(analysis?.candidates, userModelText(requirement, taskState.task))
  const renderField = (candidate, field) => {
    const { entries, unlocated } = mergeEvidence(field.sources, field.raw_evidence)
    const unparsed = structuredUnparsedRows(field.unparsed_values)
    const rawEvidence = (field.raw_evidence || []).filter(source => source?.excerpt)
    const base = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000/api/v1'
    return <div key={field.field_name}>
    <div><strong>{fieldLabel(field.field_name)}</strong><p>{displayValue(field.value)}{field.unit ? ` ${field.unit}` : ''}</p>
      {/* 「有原文未结构化」这件事在一张字段卡里原来说了三遍（这条状态行、下面那块的标题、
          原始正文折叠块的说明）。保留位置最贴近它所解释的对象的那处：下面那块直接列着
          没拆成参数的原文，标题就该由它来说；这条状态行只在**那块不渲染时**（``unparsed_values``
          拆不出任何一行）才补上，任何分支都不丢信息。275 行那句只留它独有的"怎么用"
          （"未拆成参数"已经写在它自己的 summary 里）。 */}
      {field.evidence_status === 'unparsed' && !unparsed.rows.length && <small>已取得相关原文，但当前词条未完成结构化；以下仅供人工核对，不计入正式确认。</small>}
      {unparsed.rows.length > 0 && <div className="unparsed-values"><span>资料里有原文、但未结构化成参数：以下仅供人工核对，不计入已确认参数</span><dl>{unparsed.rows.map(row => <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}</dl>{unparsed.dropped > 0 && <small>另有 {unparsed.dropped} 段表格原文无法拆成单条参数，未在此展示，可经下方原文入口核对。</small>}</div>}
      {entries.map(entry => <small key={entry.key}>{entry.kind === 'document' ? <a href={`${base}/tasks/${taskState.task.id}/documents/${entry.document_id}/original#page=${entry.page || 1}`} target="_blank" rel="noreferrer">{entry.unparsed ? '查看原文线索' : '查看原始资料'} · {evidencePageLabel(entry.location)}</a> : <SourceLink url={entry.url}>{entry.unparsed ? '查看原文线索' : '查看参数来源'}</SourceLink>}{entry.count > 1 && <span className="evidence-count">{entry.count} 处</span>}</small>)}
      {unlocated > 0 && <small>另有 {unlocated} 条证据只保留了正文片段，没有文档或网址可定位。</small>}
      {rawEvidence.length > 0 && <details className="raw-evidence"><summary>展开资料中的原始正文（{rawEvidence.length} 段，未结构化）</summary><p>以下原文仅供人工核对；页码入口见上。</p>{rawEvidence.map((source, index) => <blockquote key={`raw-${index}`}>{source.excerpt}</blockquote>)}</details>}
      {!entries.length && <small>尚无可定位的原文依据</small>}
    </div>
    <StatusTag tone={field.status === 'confirmed' ? 'success' : field.status === 'rejected' ? 'danger' : 'warning'}>{field.evidence_status === 'unparsed' ? '已有原文待核对' : ({pending_review:'尚未人工核对',unknown:'资料未提供',conflicted:'原文存在冲突',confirmed:'已人工核对',rejected:'不采用'}[field.status] || '尚未核实')}</StatusTag>
    {['pending_review', 'unknown', 'conflicted'].includes(field.status) && <div className="module-actions">{field.status === 'pending_review' && <ActionButton variant="ghost" icon={null} disabled={busy === 'analysis-decision'} onClick={() => decideAnalysis([{ analysis_id: analysis.analysis_id, candidate_id: candidate.candidate_id, status: 'pending_review', field_statuses: { [field.field_name]: 'confirmed' } }])}>与原文一致</ActionButton>}<ActionButton variant="ghost" icon={null} disabled={busy === 'analysis-decision'} onClick={() => decideAnalysis([{ analysis_id: analysis.analysis_id, candidate_id: candidate.candidate_id, status: 'pending_review', field_statuses: { [field.field_name]: 'rejected' } }])}>不采用</ActionButton></div>}
  </div>
  }
  return <section className="module-panel">
    <div className="module-title"><div><h2>型号与参数</h2><p>这里展示从上传资料提取的内容。先核对主机与配置，再选择用于价格比较的关键指标。</p></div><ActionButton variant="secondary" icon={Bot} onClick={runAnalysis} disabled={!modelConnected || !documents.length || busy === 'analysis'}>{busy === 'analysis' ? '正在提取' : hasAnalysis ? '重新分析资料' : '开始分析'}</ActionButton></div>
    {!documents.length ? <p>请先上传产品资料。</p> : !modelConnected && <p>请先在“连接设置”连接模型。</p>}
    {analysis?.gaps?.length > 0 && <details className="analysis-missing"><summary>提取不完整的内容（{analysis.gaps.length} 项）</summary><ResearchIssues values={analysis.gaps} /></details>}
    {!hasAnalysis ? <EmptyState icon={Bot} title="尚未提取型号与参数">分析后会显示主机、配置、参数和原文位置。</EmptyState> : <>
      <p>“已提取”表示找到了资料内容；“资料未提供”表示缺少信息。你只需在比较基准中确认本轮要比较的指标，不必逐个确认所有字段。</p>
      <ActionButton variant="secondary" onClick={() => openTab('核价与替代')}>核对比较基准，查找价格与其他品牌</ActionButton>
      <div className="candidate-list analysis-candidates">{focus.candidates.map(candidate => {
        const fields = candidate.fields || []
        const known = fields.filter(f => f.value != null && f.value !== 'unknown' && f.status !== 'unknown')
        const missing = fields.filter(f => !known.includes(f))
        const unresolved = fields.filter(f => ['pending_review','conflicted'].includes(f.status))
        return <article key={candidate.candidate_id}><div className="candidate-index"><FileText size={18} /></div><div>
          <h3>{candidate.brand || '品牌未提取'} {candidate.model}</h3><p>品类：{candidate.category || taskState.task.category || '尚未识别，请在比较基准中填写'} · 已提取 {known.length} 组信息，缺少 {missing.length} 组信息</p>
          {focus.narrowed && focus.models.includes(String(candidate.model || '').trim()) && <p className="analysis-focus-note">资料里识别到 {focus.total} 个型号，这里只展开你在需求里指定的 {focus.models.join('、')}。</p>}
          <div className="analysis-fields">{known.map(field => renderField(candidate,field))}</div>
          {/* 缺项数由上面那张卡的抬头说一次（「缺少 N 组信息」）就够了；这块的标题只说
              它装的是哪些内容，不再把同一个数字说第二遍。 */}
          {missing.length > 0 && <details className="analysis-missing"><summary>资料未提供或未能提取的内容</summary><p>以下缺项保留，不要求你猜测数值。只有涉及本轮关键指标时才需要补充。</p><div className="analysis-fields">{missing.map(field => renderField(candidate,field))}</div></details>}
          <details className="analysis-missing"><summary>人工核对整份产品信息（可选）</summary><p>逐项核对原文后可记录人工确认，不影响你先选择关键指标开展比较。</p><div className="module-actions"><ActionButton variant="secondary" disabled={busy === 'analysis-decision' || unresolved.length > 0} title={unresolved.length ? `还有 ${unresolved.length} 组已提取内容未人工核对` : '保存人工核对状态'} onClick={() => decideAnalysis([{analysis_id:analysis.analysis_id,candidate_id:candidate.candidate_id,status:'confirmed',field_statuses:{}}])}>完成该产品人工核对</ActionButton><ActionButton variant="ghost" icon={null} disabled={busy === 'analysis-decision'} onClick={() => decideAnalysis([{analysis_id:analysis.analysis_id,candidate_id:candidate.candidate_id,status:'rejected',field_statuses:{}}])}>排除产品</ActionButton></div></details>
        </div></article>
      })}</div>
      {focus.narrowed && <details className="analysis-missing analysis-other-models"><summary>资料里识别到的其他型号（{focus.others.length} 个，未纳入本次比较）</summary><p>这些型号来自同一份资料的其他列或选配说明，不是你在需求里指定的机器；需要比较时在需求里写明型号再重新分析。</p><ul>{focus.others.map(other => <li key={other.candidate_id || other.model}>{other.brand || '品牌未提取'} {other.model || '型号未提取'}</li>)}</ul></details>}
    </>}
  </section>
}

function SearchPanel({ taskState, openTab }) {
  const { searchRuns, searchPlan, candidates, requirement, createSearchPlan, confirmAndRunSearchPlan, runSearch, discoverWebCandidates, decideSource, runComparison, busy, providerStatus } = taskState
  const [query, setQuery] = useState('')
  const [priceCandidate, setPriceCandidate] = useState({})
  const sources = [...new Map(searchRuns.flatMap(run => (run.sources || []).map(source => ({ ...source, queryCandidateId: run.query?.candidate_id }))).map(source => [source.url, source])).values()]
  const confirmedCandidate = candidates.some(item => item.status === 'confirmed')
  const confirmedBody = sources.some(source => source.decision_status === 'confirmed' && source.body_status === 'fetched')
  const modelConnected = providerStatus?.items?.some(item => item.id === 'reasoning' && item.status === 'configured')
  const searchConnected = providerStatus?.items?.some(item => item.id === 'search' && item.status === 'configured')
  const requirementConfirmed = requirement?.status === 'confirmed'
  const canDirectSearch = searchConnected && requirementConfirmed && (confirmedCandidate || query.trim())
  return <section className="module-panel"><div className="module-title"><div><h2>联网查找</h2><p>确认采购条件后查找官方页面与采购结果；无需先有型号。摘要只作线索。</p></div><div className="search-actions"><ActionButton variant="ghost" icon={ArrowRight} disabled={!requirementConfirmed || Boolean(busy)} title={!requirementConfirmed ? '请先确认采购需求' : '生成本次检索范围'} onClick={() => createSearchPlan({ purpose: 'official' })}>{busy === 'search-plan' ? '准备中' : '准备检索'}</ActionButton><input value={query} onChange={event => setQuery(event.target.value)} placeholder="品牌、型号或关键词" aria-label="检索词" /><ActionButton variant="secondary" icon={Search} disabled={!canDirectSearch || busy === 'search'} title={!requirementConfirmed ? '请先确认采购需求' : !searchConnected ? '先在连接设置中测试联网能力' : !confirmedCandidate && !query.trim() ? '确认候选或输入明确检索词' : '开始检索'} onClick={() => runSearch(query.trim() ? { query: query.trim(), fetch_body: true } : { fetch_body: true })}>{busy === 'search' ? '检索中' : '直接检索'}</ActionButton><ActionButton variant="primary" icon={FolderSearch} disabled={!modelConnected || !confirmedBody || busy === 'web-candidates'} title={!modelConnected ? '先在连接设置中测试思考模型' : !confirmedBody ? '先确认一条已抓取正文的来源' : '从已确认正文中发现相近产品'} onClick={discoverWebCandidates}>{busy === 'web-candidates' ? '整理中' : '发现产品'}</ActionButton></div></div><p className="action-hint">{!requirementConfirmed ? '请先在“需求”页确认采购条件。' : !searchConnected ? '联网能力未连接；检索按钮已禁用，不会伪造来源。' : !confirmedCandidate && !query.trim() ? '请确认候选或输入明确检索词。' : !confirmedBody ? '确认来源后，才能从正文中发现产品。' : !modelConnected ? '已可联网检索；发现产品还需连接思考模型。' : '已具备来源正文，可以继续发现产品。'}</p>{searchPlan && <div className="search-plan-review"><div><strong>{searchPlan.status === 'confirmed' ? '检索范围已确认' : '确认本次检索范围'}</strong><p>{searchPlan.queries?.map(item => item.text).join('；')}</p></div>{searchPlan.status !== 'confirmed' && <ActionButton variant="secondary" icon={Search} disabled={!searchConnected || busy === 'search-plan-run'} title={!searchConnected ? '先在连接设置中测试联网能力' : '确认范围并开始检索'} onClick={confirmAndRunSearchPlan}>{busy === 'search-plan-run' ? <LoadingLabel>正在检索</LoadingLabel> : '确认并检索'}</ActionButton>}</div>}{!searchRuns.length || !sources.length ? <EmptyState icon={Search} title={searchRuns.length ? "未找到可用来源" : "尚未检索"}>{searchRuns.length ? "可以缩短关键词、改用具体设备名称，或在连接设置中更换搜索来源后重试。" : "确认采购条件后即可开始查找。"}</EmptyState> : <div className="document-ledger">{sources.map(source => { const candidateId = priceCandidate[source.url] || source.queryCandidateId || candidates[0]?.id || ''; return <article key={source.url}><div className="file-sign"><Search size={18} /></div><div className="file-main"><strong>{source.title || source.url}</strong><p>{source.body_text ? source.body_text.slice(0, 180) : source.snippet || '暂无正文'}</p>{source.body_text && <details><summary>阅读来源正文</summary><p className="source-body">{source.body_text}</p></details>}<div><a href={source.url} target="_blank" rel="noreferrer">打开原始来源</a><span>{humanLabel(source.source_type, sourceTypeLabels)}</span><span>{humanLabel(source.price_evidence_level, priceLevelLabels)}</span>{source.price_amount != null && <span>历史金额 {source.price_amount} {source.currency || ''}</span>}</div>{source.price_amount != null && <div className="quote-boundary"><span>金额来自正文：{source.price_quote || '已提取历史金额'}；不等同于当前报价。</span><div className="module-actions"><select value={candidateId} onChange={event => setPriceCandidate(current => ({ ...current, [source.url]: event.target.value }))}><option value="">选择关联候选</option>{candidates.map(candidate => <option key={candidate.id} value={candidate.id}>{candidate.brand ? `${candidate.brand} ` : ''}{candidate.model}</option>)}</select><ActionButton variant="secondary" disabled={source.decision_status !== 'confirmed' || !candidateId || busy === 'comparison'} onClick={() => runComparison({ candidate_id: candidateId, quotes: [{ quote_id: `tender-${Date.now()}`, candidate_id: candidateId, supplier: '正式招标文件', model: source.title || '未识别型号', configuration: '正文配置待核验', amount: source.price_amount, currency: source.currency || 'CNY', source_type: 'historical_tender_price', source: source.title || source.url, source_location: source.url, note: source.price_quote || '来源正文已提取历史金额' }] })}>加入历史价格</ActionButton></div></div>}{source.decision_status === 'confirmed' && <WebFactForm taskState={taskState} source={source} />}<div className="module-actions"><ActionButton variant="ghost" disabled={Boolean(busy) || source.body_status !== 'fetched' || !source.body_text} onClick={() => decideSource(source.url, 'confirmed')}>确认来源</ActionButton><ActionButton variant="ghost" disabled={busy === 'source-decision'} onClick={() => decideSource(source.url, 'rejected')}>排除</ActionButton></div></div><StatusTag tone={source.decision_status === 'confirmed' ? 'success' : source.decision_status === 'rejected' ? 'danger' : 'warning'}>{source.decision_status === 'confirmed' ? '已确认来源' : source.decision_status === 'rejected' ? '已排除来源' : '待确认来源'}</StatusTag></article> })}</div>}{confirmedBody && <div className="module-actions next-step-actions"><ActionButton variant="secondary" icon={FileSearch} onClick={() => openTab('证据')}>进入证据核对</ActionButton></div>}</section>
}

function EvidencePanel({ taskState, openTab }) {
  const { facts, rebuildIndex, reviewFact, busy } = taskState
  return <section className="module-panel evidence-module"><div className="module-title"><div><h2>事实与来源</h2><p>只有带页码或网页正文位置的内容才会进入判断；摘要和模型猜测不会进入这里。</p></div><ActionButton variant="secondary" onClick={rebuildIndex} disabled={busy === 'index'}>{busy === 'index' ? '更新中' : '更新资料索引'}</ActionButton></div>
    {!facts.length ? <EmptyState icon={FileSearch} title="尚无待复核事实">确认来源或供应商回复后，事实草稿会出现在这里。这里只保留有来源位置的正文。</EmptyState> : <><div className="document-ledger">{facts.map(fact => <article key={fact.fact_id}><div className="file-sign"><FileSearch size={18} /></div><div className="file-main"><strong>{fieldLabel(fact.field_name)}</strong><p>{displayValue(fact.value)}{fact.unit ? ` ${fact.unit}` : ''}</p><div><span>{candidateName(taskState.candidates, fact.candidate_id)}</span><span>{fact.status === 'pending_review' ? '待复核' : fact.status === 'confirmed' ? '已确认' : fact.status === 'rejected' ? '已驳回' : fact.status}</span></div>{fact.evidence?.map(evidence => <small key={evidence.evidence_id}>来源：{evidence.url ? <SourceLink url={evidence.url}>打开原始来源</SourceLink> : taskState.documents.find(item => item.id === evidence.document_id)?.source_name || '采购资料'} · {evidence.page ? `第 ${evidence.page} 页` : evidence.location || evidence.body_location || '位置未知'}</small>)}<div className="module-actions">{fact.status === 'pending_review' && <><ActionButton variant="ghost" disabled={busy === 'fact-review'} onClick={() => reviewFact(fact.fact_id, { action: 'confirm', reviewer_id: 'current-user', note: '用户在证据页确认事实' })}>确认事实</ActionButton><ActionButton variant="ghost" disabled={busy === 'fact-review'} onClick={() => reviewFact(fact.fact_id, { action: 'reject', reviewer_id: 'current-user', note: '用户在证据页驳回事实' })}>驳回</ActionButton></>}</div></div><StatusTag tone={fact.status === 'confirmed' ? 'success' : fact.status === 'rejected' ? 'danger' : 'warning'}>{fact.status === 'pending_review' ? '待复核' : fact.status === 'confirmed' ? '已确认' : fact.status === 'rejected' ? '已驳回' : fact.status}</StatusTag></article>)}</div>{facts.some(item => item.status === 'confirmed') && <div className="module-actions next-step-actions"><ActionButton variant="secondary" icon={GitCompare} onClick={() => openTab('适配')}>进入适配判断</ActionButton></div>}</>}
  </section>
}

function EvaluationPanel({ taskState, openTab }) {
  const { facts, evaluations, runEvaluation, busy } = taskState
  return <section className="module-panel"><div className="module-title"><div><h2>需求适配判断</h2><p>只用已确认的事实和必须满足的条件判断，缺少依据时会标记为待复核。</p></div><ActionButton variant="secondary" icon={GitCompare} title={!facts.some(item => item.status === 'confirmed') ? '先确认至少一条事实' : '开始判断候选是否满足需求'} disabled={busy === 'evaluation' || !facts.some(item => item.status === 'confirmed')} onClick={runEvaluation}>{busy === 'evaluation' ? '判断中' : '开始判断'}</ActionButton></div><div className="quote-boundary"><strong>当前事实：</strong>{facts.length} 条；其中已确认 {facts.filter(item => item.status === 'confirmed').length} 条。</div>{!evaluations.length ? <EmptyState icon={GitCompare} title="尚未运行适配判断">先确认事实，再判断候选是否满足需求。</EmptyState> : <><div className="candidate-list">{evaluations.map(item => <article key={item.candidate_id}><div className="candidate-index"><PackageOpen size={18} /></div><div><h3>{candidateName(taskState.candidates, item.candidate_id)}</h3><p>{(item.hard_constraints || []).map(value => `${value.name || value.constraint_name || '必须满足的条件'}：${reviewLabel(value.status)}`).join('；') || '没有必须满足的条件结果'}</p></div><StatusTag tone={item.status === 'eligible' ? 'success' : item.status === 'ineligible' ? 'danger' : 'warning'}>{reviewLabel(item.status)}</StatusTag></article>)}</div><div className="module-actions next-step-actions"><ActionButton variant="secondary" icon={ClipboardCheck} onClick={() => openTab('推荐与报价')}>进入推荐与报价</ActionButton></div></>}</section>
}

function SupplierPanel({ taskState }) {
  const { inquiries, replies, candidates, generatedQuestions, persistGeneratedQuestions, submitSupplierReply, reviewSupplierReply, createFactDraft, busy, finalDecision } = taskState
  const [selected, setSelected] = useState('')
  const [supplier, setSupplier] = useState('')
  const [questionSupplier, setQuestionSupplier] = useState('')
  const [text, setText] = useState('')
  const [factField, setFactField] = useState('')
  const [factValue, setFactValue] = useState('')
  const inquiry = inquiries.find(item => item.inquiry_id === selected)
  return <section className="module-panel"><div className="module-title"><div><h2>供应商追问</h2><p>回复先留存原文，再由你确认；确认后才能进入事实判断。</p></div><StatusTag tone={finalDecision?.status === 'confirmed' ? 'success' : 'warning'}>{finalDecision?.status === 'confirmed' ? '可以追问' : '等待结论确认'}</StatusTag></div>{!inquiries.length && generatedQuestions?.questions?.length > 0 && <div className="confirmation-card"><span>已生成 {generatedQuestions.questions.length} 条追问</span><input value={questionSupplier} onChange={event => setQuestionSupplier(event.target.value)} placeholder="输入供应商名称" /><ActionButton variant="secondary" disabled={!questionSupplier.trim() || finalDecision?.status !== 'confirmed' || busy === 'inquiries'} onClick={() => persistGeneratedQuestions(questionSupplier)}>{busy === 'inquiries' ? '保存中' : '保存追问'}</ActionButton></div>}{!inquiries.length ? <EmptyState icon={Send} title="尚未保存追问">先在“比较与评分”生成问题，并在报告页确认结论。</EmptyState> : <><div className="document-ledger">{inquiries.map(item => <article key={item.inquiry_id}><div className="file-sign"><Send size={18} /></div><div className="file-main"><strong>{item.supplier}</strong><p>{item.question}</p><div><span>{item.reason}</span><span>{item.status === 'answered' ? '已回复' : '待回复'}</span></div><ActionButton variant="ghost" onClick={() => { setSelected(item.inquiry_id); setSupplier(item.supplier) }}>回填回复</ActionButton></div><StatusTag tone={item.status === 'answered' ? 'success' : 'warning'}>{item.status === 'answered' ? '已回复' : '待回复'}</StatusTag></article>)}</div>{inquiry && <form className="confirmation-card" onSubmit={event => { event.preventDefault(); if (supplier.trim() && text.trim()) { submitSupplierReply(inquiry.inquiry_id, { supplier, received_at: new Date().toISOString(), original_text: text, source_location: '用户录入' }); setText('') } }}><span>回复回填</span><h3>{inquiry.question}</h3><input value={supplier} onChange={event => setSupplier(event.target.value)} placeholder="供应商名称" /><textarea value={text} onChange={event => setText(event.target.value)} placeholder="粘贴供应商原文回复" rows="4" /><ActionButton variant="coral" type="submit" disabled={busy === 'supplier-reply'}>{busy === 'supplier-reply' ? '保存中' : '保存待复核回复'}</ActionButton></form>}</>}{replies.length > 0 && <div className="document-ledger">{replies.map(reply => { const inquiryForReply = inquiries.find(item => item.inquiry_id === reply.inquiry_id); const candidateId = inquiryForReply?.candidate_id || candidates[0]?.id; return <article key={reply.reply_id}><div className="file-sign"><FileText size={18} /></div><div className="file-main"><strong>{reply.supplier}</strong><p>{reply.original_text}</p><div><span>供应商回复原文</span><span>{reply.review_status === 'pending_review' ? '待复核' : reply.review_status === 'confirmed' ? '已确认' : '已驳回'}</span></div>{reply.review_status === 'pending_review' && <ActionButton variant="ghost" disabled={busy === 'reply-review'} onClick={() => reviewSupplierReply(reply.reply_id, { status: 'confirmed', explicit_user_confirmation: true, reviewed_by: 'current-user', note: '用户确认回复原文' })}>确认回复</ActionButton>}{reply.review_status === 'confirmed' && <div className="confirmation-card"><span>转为事实草稿</span><div className="module-actions"><input value={reply.reply_id === selected ? factField : ''} onChange={event => { setSelected(reply.reply_id); setFactField(event.target.value) }} placeholder="事实字段，如检测波长" /><input value={reply.reply_id === selected ? factValue : ''} onChange={event => { setSelected(reply.reply_id); setFactValue(event.target.value) }} placeholder="事实值" /><ActionButton variant="ghost" disabled={!candidateId || !factField.trim() || !factValue.trim() || busy === 'fact-draft'} onClick={async () => { const value = await createFactDraft(reply.reply_id, { candidate_id: candidateId, field_name: factField.trim(), value: factValue.trim(), value_type: 'text' }); if (value) { setFactField(''); setFactValue('') } }}>创建事实草稿</ActionButton></div></div>}</div><StatusTag tone={reply.review_status === 'confirmed' ? 'success' : reply.review_status === 'rejected' ? 'danger' : 'warning'}>{reply.review_status === 'confirmed' ? '已确认' : reply.review_status === 'pending_review' ? '待复核' : '已驳回'}</StatusTag></article> })}</div>}</section>
}

function RecommendationPanel({ taskState, openTab }) {
  const { candidates, evaluations, ranking, generatedQuestions, comparison, runRecommendation, runComparison, generateQuestions, busy } = taskState
  const [candidateId, setCandidateId] = useState('')
  const [supplier, setSupplier] = useState('')
  const [model, setModel] = useState('')
  const [amount, setAmount] = useState('')
  const [currency, setCurrency] = useState('CNY')
  const [sourceType, setSourceType] = useState('current_supplier_quote')
  const [sourceLocation, setSourceLocation] = useState('用户录入报价')
  const [taxCondition, setTaxCondition] = useState('')
  const [deliveryCondition, setDeliveryCondition] = useState('')
  const [installationCondition, setInstallationCondition] = useState('')
  const [warrantyCondition, setWarrantyCondition] = useState('')
  const [afterSalesCondition, setAfterSalesCondition] = useState('')
  const [paymentCondition, setPaymentCondition] = useState('')
  const selectedCandidate = candidates.find(item => item.id === candidateId)
  const quoteConditions = [
    ['tax', taxCondition], ['delivery', deliveryCondition], ['installation', installationCondition],
    ['warranty', warrantyCondition], ['after_sales', afterSalesCondition], ['payment', paymentCondition],
  ].map(([condition_type, value]) => ({ condition_type, value: value.trim() || 'unknown', source_location: sourceLocation.trim() || '用户录入报价' }))
  const canScore = evaluations.length > 0
  const priceEntryMissing = [!candidateId && '候选', !supplier.trim() && '供应商', !model.trim() && '型号', !amount && '金额'].filter(Boolean)
  return <section className="module-panel recommendation-module"><div className="module-title"><div><h2>比较与评分</h2><p>把技术事实和价格条件放在一起比较；证据不足时不会给出首选。</p></div><div className="module-actions"><ActionButton variant="secondary" disabled={!canScore || busy === 'recommendation'} title={!canScore ? '请先运行适配判断' : '开始比较并评分'} onClick={runRecommendation}>{busy === 'recommendation' ? '评分中' : '开始评分'}</ActionButton><ActionButton variant="ghost" disabled={!canScore || busy === 'questions'} title={!canScore ? '请先完成适配判断' : '生成供应商问题'} onClick={generateQuestions}>{busy === 'questions' ? '生成中' : '生成追问'}</ActionButton></div></div><p className="action-hint">{canScore ? '可以开始评分或生成供应商问题。' : '先在“适配”页运行判断，再继续。'}</p>
    {!ranking || ranking.status === 'no_recommendation' ? <div className="no-recommendation"><XCircle size={28} /><div><strong>暂时没有首选</strong><p>{ranking?.status === 'no_recommendation' ? '请先处理必须满足的条件、待确认事实和价格条件。' : candidates.length ? `已有 ${candidates.length} 个候选，先完成适配判断。` : '还没有候选设备，请先上传资料并识别。'}</p></div><button className="text-action" onClick={() => openTab(candidates.length ? '适配' : '候选')}>查看下一步<ArrowRight size={14} /></button></div> : <div className="candidate-list"><article><div className="candidate-index">01</div><div><span>首选</span><h3>{candidateName(candidates, ranking.preferred?.candidate_id)}</h3><p>{ranking.preferred?.reason || ranking.explanation || '依据详见来源与适配结果'}</p></div><StatusTag tone="success">{ranking.status === 'recommendations' ? '已完成评分' : ranking.status}</StatusTag></article></div>}
    {generatedQuestions?.questions?.length > 0 && <div className="document-ledger">{generatedQuestions.questions.map((question, index) => <article key={question.question_id || index}><div className="file-sign"><Send size={18} /></div><div className="file-main"><strong>{question.candidate_id || '候选待指定'}</strong><p>{question.question || question.text}</p><div><span>{question.reason || question.category}</span></div></div><StatusTag tone="warning">待发送</StatusTag></article>)}</div>}
    <div className="quote-boundary"><AlertTriangle size={18} /><p>历史招标价只能作为历史参考，不等于当前报价。税费、安装、附件、培训、维保和交付条件必须拆开比较。</p></div>
    <form className="confirmation-card quote-entry" onSubmit={event => { event.preventDefault(); if (priceEntryMissing.length) return; runComparison({ candidate_id: candidateId, quotes: [{ quote_id: `quote-${Date.now()}`, candidate_id: candidateId, supplier: supplier.trim(), model: model.trim(), configuration: selectedCandidate?.configuration || '未说明', amount: Number(amount), currency: currency.toUpperCase(), source_type: sourceType, source: supplier.trim(), source_location: sourceLocation.trim() || '用户录入报价', conditions: quoteConditions }] }) }}><span>加入一份价格</span><p className="quote-entry-purpose">用于核对不同价格是否包含相同配置、服务和交付条件。</p><div className="quote-entry-grid quote-core-grid"><select value={candidateId} onChange={event => setCandidateId(event.target.value)}><option value="">选择候选</option>{candidates.map(candidate => <option key={candidate.id} value={candidate.id}>{candidate.brand ? `${candidate.brand} ` : ''}{candidate.model}</option>)}</select><input value={supplier} onChange={event => setSupplier(event.target.value)} placeholder="供应商" /><input value={model} onChange={event => setModel(event.target.value)} placeholder="报价型号" /><input value={amount} onChange={event => setAmount(event.target.value)} type="number" min="0" step="0.01" placeholder="金额" /><input value={currency} onChange={event => setCurrency(event.target.value)} maxLength={3} placeholder="币种" /><select value={sourceType} onChange={event => setSourceType(event.target.value)}><option value="current_supplier_quote">当前供应商报价</option><option value="historical_tender_price">历史招标/采购结果</option><option value="official_public_price">官方公开价格</option><option value="dealer_lead">经销商线索</option><option value="unknown">来源待核验</option></select></div><details className="quote-advanced"><summary>补充条件与来源（可选）</summary><div className="quote-entry-grid quote-conditions-grid"><input value={taxCondition} onChange={event => setTaxCondition(event.target.value)} placeholder="税费，如含税" /><input value={deliveryCondition} onChange={event => setDeliveryCondition(event.target.value)} placeholder="交付条件" /><input value={installationCondition} onChange={event => setInstallationCondition(event.target.value)} placeholder="安装/培训" /><input value={warrantyCondition} onChange={event => setWarrantyCondition(event.target.value)} placeholder="质保" /><input value={afterSalesCondition} onChange={event => setAfterSalesCondition(event.target.value)} placeholder="售后" /><input value={paymentCondition} onChange={event => setPaymentCondition(event.target.value)} placeholder="付款条件" /><input value={sourceLocation} onChange={event => setSourceLocation(event.target.value)} placeholder="来源定位（页码/网址/文件位置）" /></div></details><small>{priceEntryMissing.length ? `还需填写：${priceEntryMissing.join('、')}。` : '可以加入比较；未填写的附加条件会标为待确认。'}</small><ActionButton variant="secondary" type="submit" disabled={busy === 'comparison' || priceEntryMissing.length > 0}>{busy === 'comparison' ? '比较中' : '加入价格比较'}</ActionButton></form>
    {comparison && <div className="quote-boundary"><strong>本次比较结果</strong><p>{comparison.comparable?.length || 0} 组可比，{comparison.incomparable?.length || 0} 组不可比。{comparison.gaps?.length ? `缺口：${comparison.gaps.join('、')}` : '当前没有额外缺口。'}</p></div>}
    {ranking && <div className="module-actions next-step-actions"><ActionButton variant="secondary" icon={Download} onClick={() => openTab('报告')}>进入报告生成</ActionButton></div>}
  </section>
}

function evidenceLocation(evidence) {
  if (evidence?.document_id) return `采购资料${evidence.page ? ` · 第 ${evidence.page} 页` : evidence.location ? ` · ${evidence.location}` : ''}`
  if (evidence?.url) return `${evidence.url}${evidence.body_location || evidence.location ? ` · ${evidence.body_location || evidence.location}` : ''}`
  if (evidence?.quote_id) return `报价资料${evidence.location ? ` · ${evidence.location}` : ''}`
  return evidence?.missing_reason || '来源位置待补充'
}

function ReportsPanel({ taskState, openTab, reportId }) {
  const { reports, createReport, loadReports, busy, requirement, finalDecision, saveFinalDecision, createArchive } = taskState
  const [selectedReportId, setSelectedReportId] = useState(reportId || '')
  const [exporting, setExporting] = useState(false)
  const [exportError, setExportError] = useState('')
  const exportLock = useRef(false)
  async function exportReport(savedId) {
    if (exportLock.current) return
    exportLock.current = true; setExporting(true); setExportError('')
    const base = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000/api/v1'
    try {
      let response
      if (savedId) response = await fetch(`${base}/reports/export`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({report_ids:[savedId]})})
      else {
        const baselineResponse = await fetch(`${base}/tasks/${taskState.task.id}/baseline`)
        if (!baselineResponse.ok) throw new Error('暂时无法读取本轮结果，请重试。')
        const data = await baselineResponse.json()
        if (!data.run) throw new Error('先确认材料与报价中的采购条件，再生成报告。')
        if ((data.intake_draft?.intake_revision || 0) > (data.baseline?.intake_revision || 0)) throw new Error('你补充了新的采购条件，请先返回“材料与报价”确认，再生成本轮报告。历史报告仍可单独下载。')
        if (['queued','running'].includes(data.run.status)) throw new Error('本轮仍在检索，请完成或停止后生成报告。')
        response = await fetch(`${base}/tasks/${taskState.task.id}/research-runs/${data.run.id}/report`, {method:'POST'})
      }
      if (!response.ok) throw new Error('报告暂未导出，请重试；已有结果会保留。')
      // Read the archive as a buffer instead of Response.blob(): on a large
      // export (tens of MB) Response.blob() rejects with "Failed to fetch" in
      // Chromium, while arrayBuffer() + new Blob() is reliable.
      const archive = new Blob([await response.arrayBuffer()], { type: response.headers.get('Content-Type') || 'application/zip' })
      const url=URL.createObjectURL(archive), a=document.createElement('a')
      a.href=url; a.download='仪器采购分析报告.zip'; document.body.appendChild(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),30000)
      await loadReports()
    } catch(e) {setExportError(e.message)}
    finally {setExporting(false); exportLock.current=false}
  }

  useEffect(() => { setSelectedReportId(reportId || '') }, [reportId])
  const evidenceLink = (evidence,index) => <small key={evidence.evidence_id || index}>来源：{evidence.url ? <SourceLink url={evidence.url}>打开原始来源</SourceLink> : evidence.document_id ? <a href={`${import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000/api/v1'}/tasks/${taskState.task.id}/documents/${evidence.document_id}/original#page=${evidence.page || 1}`} target="_blank" rel="noreferrer">{evidenceLocation(evidence)}</a> : evidenceLocation(evidence)}</small>
  const content = section => <><p><LinkedText>{publicText(section.summary, taskState)}</LinkedText></p>{section.missing_reason && <p>尚缺：<LinkedText>{publicText(section.missing_reason, taskState)}</LinkedText></p>}{(section.evidence || []).map(evidenceLink)}{!(section.items || []).length && ['prices','alternatives'].includes(section.key) && <p>{section.key === 'prices' ? '本轮没有已核实的价格记录。' : '本轮没有完成可替代性核验的产品。'}</p>}{(section.items || []).map((item,index) => <div className="report-detail-item" key={index}><strong>{publicText(item.label, taskState)}</strong><p><LinkedText>{publicText(item.text, taskState)}</LinkedText></p>{item.candidate_id && <span>{candidateName(taskState.candidates,item.candidate_id)}</span>}{item.missing_reason && <small>尚缺：{publicText(item.missing_reason,taskState)}</small>}{(item.evidence || []).map(evidenceLink)}</div>)}</>
  return <section className="module-panel reports-module">
    <div className="module-title"><div><h2>采购分析报告</h2><p>汇总已确认条件、成交价格依据、其他品牌的优势与取舍，以及采购前需要补齐的承诺。</p></div><div className="module-actions"><ActionButton variant="ghost" icon={RefreshCw} onClick={loadReports}>刷新报告</ActionButton><ActionButton variant="secondary" icon={Download} disabled={exporting || Boolean(busy)} onClick={() => exportReport()}>{exporting ? '正在保存与导出' : '生成并下载本轮报告'}</ActionButton></div></div>
    {exportError && <InlineNotice tone="error" title="报告未导出">{exportError}</InlineNotice>}

    {!reports.length ? <EmptyState icon={Download} title="尚未生成报告">完成本轮检索后，报告会自动保存。未完成的结果也会保留原因。</EmptyState> : <div className="report-list">{[...reports].sort((a,b) => new Date((b.report || b).created_at || 0) - new Date((a.report || a).created_at || 0)).map((report,index) => {
      const payload = report.report || report
      const sections = payload.sections || []
      const key = payload.report_id || `report-${index + 1}`
      const gaps = sections.filter(s => s.key === 'gaps').flatMap(s => s.items || [])
      const expanded = selectedReportId === key
      const summary = reportSummary(payload)
      return <article key={key} className={expanded ? 'report-expanded' : ''}><FileText size={18} /><div><strong>{taskState.task.category || '仪器'}比较报告 · {payload.created_at ? new Date(payload.created_at).toLocaleString('zh-CN') : `记录 ${index + 1}`}</strong><p>{gaps.length ? `保留 ${gaps.length} 条未完成事项或来源说明` : summary ? '展开可查看逐项依据与来源' : '展开查看结论与原文'}</p><ReportSummaryBlock summary={summary} taskState={taskState} /><div className="module-actions"><ActionButton variant="ghost" onClick={() => { setSelectedReportId(expanded ? '' : key); openTab('报告', expanded ? '' : key) }}>{expanded ? '收起报告' : '查看报告'}</ActionButton><ActionButton variant="ghost" disabled={exporting} onClick={() => exportReport(payload.report_id)}>下载这份报告</ActionButton></div>
        {expanded && <div className="report-detail" aria-label="报告正文">{sections.map(section => section.key === 'gaps' ? <details key={section.key} className="analysis-missing"><summary>影响采购判断的未完成事项</summary><ResearchIssues values={[section.summary, ...(section.items || []).map(item => item.text)].filter(Boolean)} /></details> : <section key={section.key}><h3>{publicText(section.title,taskState)}</h3>{content(section)}</section>)}</div>}
      </div></article>
    })}</div>}
    <details className="analysis-missing"><summary>记录最终决策或归档</summary><p>这一步由采购方在核对报告后完成，不会改变原始来源或自动补全缺失参数。</p><p>当前决策：{finalDecision?.status === 'confirmed' ? '已人工确认' : '尚未记录人工决策'}</p><div className="module-actions"><ActionButton variant="secondary" disabled={busy === 'final-decision'} onClick={() => saveFinalDecision({ status: 'confirmed', explicit_user_confirmation: true, decided_by: 'current-user', note: '用户在报告页确认' })}>记录为已人工确认</ActionButton><ActionButton variant="ghost" disabled={busy === 'archive'} onClick={createArchive}>归档当前任务</ActionButton></div></details>
  </section>
}

function VersionsPanel({ taskState, openTab }) {
  const { history, archives, reports, loadHistory, restoreArchive, busy } = taskState
  useEffect(() => { loadHistory() }, [loadHistory])
  return <section className="module-panel versions-module"><div className="module-title"><div><h2>版本记录</h2><p>旧版本保持只读；新报告会从当前状态建立新的任务快照。</p></div><ActionButton variant="ghost" icon={RefreshCw} onClick={loadHistory}>{busy === 'history' ? '加载中' : '刷新'}</ActionButton></div>
    {!history.length ? <EmptyState icon={History} title="还没有版本记录">生成第一份报告后，这里会显示版本号、修订原因和内容校验值。</EmptyState> : <div className="version-timeline">{history.map((version, index) => { const linkedReport = reports.find(item => item.history_version?.report_version_id === version.report_version_id); return <article key={version.version_id || index}><span>{version.version_number || index + 1}</span><div><strong>{`版本 ${version.version_number || index + 1}`}</strong><p>{version.revision_reason || '未填写修订原因'}</p></div><div className="module-actions"><StatusTag tone="neutral">只读</StatusTag><ActionButton variant="ghost" disabled={!linkedReport?.report?.report_id} title={!linkedReport ? '对应报告记录不存在' : '打开此版本对应的报告'} onClick={() => openTab('报告', linkedReport?.report?.report_id)}>查看报告</ActionButton></div></article> })}</div>}
    <div className="quote-boundary"><strong>任务归档</strong><p>{archives.length ? `已有 ${archives.length} 份归档，恢复后可以继续处理当时的任务。` : '当前还没有归档快照。'}</p>{archives[0] && <ActionButton variant="ghost" disabled={busy === 'restore'} onClick={() => restoreArchive(archives[0].archive_id)}>{busy === 'restore' ? '恢复中' : '恢复最近归档'}</ActionButton>}</div>
  </section>
}

function UnavailablePanel({ title, description, icon: Icon }) {
  return <section className="module-panel unavailable-module"><StatusTag tone="prototype">暂未接入</StatusTag><Icon size={36} aria-hidden="true" /><h2>{title}</h2><p>{description}</p><div><CircleHelp size={16} /><span>接入后只展示真实返回的状态和来源，不播放虚假进度。</span></div></section>
}

function OperationProgress({ busy, progress }) {
  const [seconds, setSeconds] = useState(0)
  useEffect(() => { setSeconds(0); const timer = setInterval(() => setSeconds(value => value + 1), 1000); return () => clearInterval(timer) }, [busy])
  const title = ({ chat: '正在生成回答', analysis: '正在分析资料', search: '正在检索并读取来源', 'search-plan': '正在准备检索', 'search-plan-run': '正在检索并读取来源', upload: '正在上传资料', confirm: '正在保存采购条件', 'web-candidates': '正在整理具体型号' })[busy] || '正在处理，请稍候'
  const determinate = busy === 'search' && progress && progress.completed < progress.total
  return <div className="operation-progress" role="status" aria-live="polite"><div><strong>{title}</strong><span>已等待 {seconds} 秒</span></div><progress aria-label={title} {...(determinate ? { value: progress.completed, max: progress.total } : {})} /><p>{determinate ? `已完成 ${progress.completed} / ${progress.total} 组检索 · 当前：${({ official: '官网参数', price: '公开价格', tender: '招标成交记录', discovery: '同类产品' })[progress.purpose] || '产品资料'}` : '处理完成后会显示结果，请勿重复提交。'}</p></div>
}

const fieldLabels = { product_identity_and_configuration: '产品型号与配置', core_function_and_applications: '用途与主要功能', detection_or_processing_modes: '检测与处理模式', capacity_and_throughput: '容量与处理量', sample_and_format_compatibility: '样品与容器兼容性', performance_range: '性能与检测范围', accuracy_sensitivity_and_repeatability: '精度、灵敏度与重复性', temperature_environment_and_safety: '温度、环境与安全', software_data_and_compliance: '软件、数据与合规', accessories_consumables_and_options: '附件与耗材', physical_dimensions_power_and_installation: '尺寸、电源与安装', price_warranty_delivery_and_service: '价格、质保与售后', budget_max: '预算上限', throughput: '样品处理量', price: '价格', amount: '金额', currency: '币种', model: '型号', brand: '品牌' }
function fieldLabel(value) { return fieldLabels[value] || String(value || '').replace(/_/g, ' ').trim() || '参数' }
function displayValue(value) {
  if (value == null || value === 'unknown') return '资料未提供'
  if (Array.isArray(value)) return value.map(displayValue).join('；')
  if (typeof value === 'object') return Object.entries(value).map(([key, entry]) => `${fieldLabel(key)}：${displayValue(entry)}`).join('；')
  return String(value)
}

function WebFactForm({ taskState, source }) {
  const [candidate, setCandidate] = useState('')
  const [field, setField] = useState('')
  const [value, setValue] = useState('')
  const [excerpt, setExcerpt] = useState('')
  return <details className="web-fact-form"><summary>从正文记录一项产品参数</summary><form onSubmit={async event => { event.preventDefault(); if (await taskState.createWebFact({ candidate_id: candidate, field_name: field, value, value_type: 'text', url: source.url, excerpt })) { setValue(''); setExcerpt('') } }}>
    <label>对应产品<select value={candidate} onChange={event => setCandidate(event.target.value)} required><option value="">请选择产品</option>{taskState.candidates.map(item => <option key={item.id} value={item.id}>{candidateName(taskState.candidates, item.id)}</option>)}</select></label>
    <label>参数名称<input value={field} onChange={event => setField(event.target.value)} placeholder="例如：检测波长" required /></label>
    <label>参数值<input value={value} onChange={event => setValue(event.target.value)} placeholder="复制原文中的参数值" required /></label>
    <label>来源原文<textarea value={excerpt} onChange={event => setExcerpt(event.target.value)} placeholder="从上方来源正文复制包含参数值的一段话" required /></label>
    <ActionButton type="submit" disabled={Boolean(taskState.busy) || !candidate || !field.trim() || !value.trim() || !excerpt.trim()}>保存为待复核参数</ActionButton>
  </form></details>
}
