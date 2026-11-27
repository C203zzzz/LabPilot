import React, { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ActionButton, InlineNotice, LoadingLabel } from '../components/ui'
import ResearchProgress from '../components/ResearchProgress'
import SupplementPanel from './SupplementPanel'
import ProcurementIntake from './ProcurementIntake'
import ResearchResults from './ResearchResults'
import { lookupCandidatePrice } from '../services/alternativePriceLookup'
import { retryResearchLanePath } from '../services/api'
import { researchIssue } from '../services/researchCopy'
import { documentBasisConflict, quoteAttribution, quotedNameOf, quotedModelOf } from '../services/procurementIdentity'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000/api/v1'
const displayValue = value => Array.isArray(value) ? value.join(' 至 ') : value == null ? '未核实' : typeof value === 'object' ? Object.entries(value).map(([k, v]) => `${k}：${displayValue(v)}`).join('；') : String(value)
const emptyParameter = () => ({ name: '', value: '', unit: '', direction: 'equal', conditions: '' })
const businessTerms = [['configuration','配置'],['quantity','数量'],['tax','税费'],['shipping','运费'],['installation','安装培训'],['warranty','保修'],['service_response','报修响应'],['service_onsite','上门处理'],['delivery','交付时间']]

// 「不能判断报价」这句话只能有一个出口：下面「分析结论」那张卡里的 ``run.price_assessment``
// 是后端按本轮证据算出来的正式结论。所以这里的缺报价说明只留它独有的含义（"只能整理
// 历史参考"），只有当结论卡还没有结论（``price_assessment`` 为空、那里显示的是默认引导语）
// 时才补上「不能判断报价高低」——两个分支都不丢信息，同一屏也不会说两遍。
const baselineDraftCache = new Map()
const mergeDraftKeepingScenario = (prev, incoming) => ({
  ...prev,
  ...incoming,
  scenario: (incoming?.scenario || '').trim() ? incoming.scenario : (prev?.scenario || ''),
})

function PriceTargetOverview({ baseline, conclusionStated, onViewCompare }) {
  const hasQuote = baseline.supplier_price != null && baseline.supplier_price !== ''
  const quote = hasQuote
    ? `${Number(baseline.supplier_price).toLocaleString('zh-CN', { maximumFractionDigits: 2 })} ${baseline.currency === 'CNY' ? '元' : baseline.currency || '币种未说明'}`
    : '尚未提供'
  const attribution = quoteAttribution(baseline)
  const filledTerms = businessTerms.filter(([key]) => baseline[key] && baseline[key] !== 'unknown')
  const missingTerms = businessTerms.filter(([key]) => !baseline[key] || baseline[key] === 'unknown')
  return <section className="price-target-overview" aria-labelledby="price-target-title">
    <div className="price-target-heading">
      <div><h3 id="price-target-title">本次核价对象</h3><strong>{[baseline.brand, baseline.model].filter(Boolean).join(' ') || '目标型号未确认'}</strong><p>{baseline.category || '仪器品类未填写'} · {baseline.scenario || '实际用途未填写'}</p>{baseline.model && onViewCompare && <button type="button" className="text-action compare-jump-link" onClick={onViewCompare}>在历史比价库查看同型号 →</button>}</div>
      <div className="supplier-quote"><span>销售报价</span><strong>{quote}</strong></div>
    </div>
    {filledTerms.length > 0 && <dl className="price-target-terms">{filledTerms.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{baseline[key]}</dd></div>)}</dl>}
    {missingTerms.length > 0 && <p className="price-target-missing-inline">未填项：{missingTerms.map(([, label]) => label).join('、')}</p>}
  </section>
}

// 「进展到哪」只按后端已经写下来的信号说：run.stage 是当前阶段，run.stage_timings 是
// 这一轮已经走完的阶段，run.lanes 是两条检索线（price/alternatives）的真实状态。
// 字段来源与「为什么没有百分比」写在这个组件的头部注释里（components/ResearchProgress.jsx）。
async function api(taskId, path, body, signal) {
  const response = await fetch(`${API_BASE}/tasks/${encodeURIComponent(taskId)}${path}`, {
    method: body === undefined ? 'GET' : 'POST', signal,
    headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (!response.ok) {
    const data = await response.json().catch(() => ({}))
    throw new Error(typeof data.detail === 'string' ? data.detail : data.detail?.message || '操作未完成，请稍后重试。')
  }
  return response.json()
}

export default function ProcurementPanel({ taskState, openTab, mode = 'material' }) {
  const navigate = useNavigate()
  const taskId = taskState.task.id
  const [draft, setDraft] = useState({ scenario: '', model: '', parameters: [emptyParameter()], currency: 'CNY' })
  const [candidates, setCandidates] = useState([])
  const [run, setRun] = useState(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [documentConflict, setDocumentConflict] = useState('')
  const [editing, setEditing] = useState(false)
  const [refreshNotice, setRefreshNotice] = useState('')
  const generation = useRef(0)
  const lock = useRef(false)
  const refreshedReport = useRef('')
  const confirmedSnapshotRef = useRef(null)
  const [reload, setReload] = useState(0)
  const [clock, setClock] = useState(Date.now())
  const [supplierReply, setSupplierReply] = useState({})
  useEffect(() => {
    const updateIntake = event => {
      if (event.detail.taskId === taskId) {
        setDraft(d => {
          const next = mergeDraftKeepingScenario(d, event.detail.draft)
          baselineDraftCache.set(taskId, { draft: next, dirty: true })
          return next
        })
        setEditing(true)
      }
    }
    window.addEventListener('labpilot-intake-update', updateIntake)
    return () => window.removeEventListener('labpilot-intake-update', updateIntake)
  }, [taskId])
  useEffect(() => {
    if (!['queued', 'running'].includes(run?.status)) return
    const timer = setInterval(() => setClock(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [run?.status])

  useEffect(() => {
    const controller = new AbortController(); const current = ++generation.current
    setLoading(true); setError(''); setRun(null); setEditing(false); lock.current = false; setBusy(''); setDocumentConflict('')
    api(taskId, '/baseline', undefined, controller.signal).then(data => {
      if (current !== generation.current) return
      setCandidates(data.candidates || [])
      const serverDraft = {
        ...data.baseline,
        category: data.baseline.category || taskState.task.category || '',
        parameters: data.baseline.parameters?.length ? data.baseline.parameters : data.baseline.model ? [] : [emptyParameter()],
      }
      confirmedSnapshotRef.current = serverDraft
      setRun(data.run)
      const cached = baselineDraftCache.get(taskId)
      let nextDraft = serverDraft
      let shouldEdit = false
      if (data.intake_draft && (data.intake_draft.intake_revision || 0) > (data.baseline.intake_revision || 0)) {
        nextDraft = mergeDraftKeepingScenario(nextDraft, {
          ...data.intake_draft,
          parameters: data.intake_draft.parameters?.length ? data.intake_draft.parameters : nextDraft.parameters,
        })
        shouldEdit = true
      }
      // 已确认的比较基准和产品资料里的型号不是同一台：不偷偷改写已确认的基准和已经出的
      // 报告，而是把资料型号预填好、明说要重新确认。
      if (data.baseline_stale_by_document) {
        const notice = documentBasisConflict(data.document_identity, data.baseline)
        setDocumentConflict(notice)
        if (notice && data.intake_draft) {
          nextDraft = mergeDraftKeepingScenario(nextDraft, {
            ...data.intake_draft,
            parameters: data.intake_draft.parameters?.length ? data.intake_draft.parameters : nextDraft.parameters,
          })
          shouldEdit = true
        }
      }
      if (cached?.dirty) {
        nextDraft = mergeDraftKeepingScenario(nextDraft, cached.draft)
        shouldEdit = true
      }
      setDraft(nextDraft)
      setEditing(shouldEdit)
      if (reload > 0) {
        setRefreshNotice(`已于 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })} 刷新最新研究结果。`)
      }
    }).catch(e => { if (e.name !== 'AbortError' && current === generation.current) setError(e.message) })
      .finally(() => { if (current === generation.current) setLoading(false) })
    return () => { controller.abort(); generation.current++ }
  }, [taskId, reload, taskState.analysis?.analysis_id])

  useEffect(() => {
    if (!run || !['queued', 'running'].includes(run.status)) return
    let active = true; let timer; const controller = new AbortController()
    const tick = async () => {
      try {
        const value = await api(taskId, `/research-runs/${run.id}`, undefined, controller.signal)
        if (active) { setRun(value); setError('') }
      } catch (e) { if (active && e.name !== 'AbortError') setError('连接暂时中断，正在恢复研究状态；请勿重复提交。') }
      if (active) timer = setTimeout(tick, 2000)
    }
    timer = setTimeout(tick, 1000)
    return () => { active = false; clearTimeout(timer); controller.abort() }
  }, [taskId, run?.id, run?.status])

  useEffect(() => { if (run?.status) taskState.updateResearchStatus?.(taskId, run.status, run.baseline) }, [taskId, run?.status, taskState.updateResearchStatus])
  useEffect(() => {
    const key = `${run?.id}:${run?.round}:${run?.status}`
    if (run && ['completed', 'partial'].includes(run.status) && refreshedReport.current !== key) {
      refreshedReport.current = key
      taskState.loadReports?.()
    }
  }, [run?.id, run?.round, run?.status, taskState.loadReports])

  const update = (key, value) => setDraft(d => {
    const next = {
      ...d,
      [key]: value,
      ...(['brand', 'model', 'configuration'].includes(key) && d[key] !== value
        ? { parameters: d.parameters.map(p => ({ ...p, sources: [] })) } : {}),
    }
    baselineDraftCache.set(taskId, { draft: next, dirty: true })
    return next
  })
  const updateParameter = (index, key, value) => setDraft(d => {
    const next = {
      ...d,
      parameters: d.parameters.map((p, i) => i === index ? { ...p, [key]: value, ...(p[key] !== value && key !== 'direction' ? { sources: [] } : {}) } : p),
    }
    baselineDraftCache.set(taskId, { draft: next, dirty: true })
    return next
  })
  const startEditing = () => {
    setEditing(true)
    baselineDraftCache.set(taskId, { draft, dirty: true })
  }
  const cancelEditing = () => {
    baselineDraftCache.delete(taskId)
    if (confirmedSnapshotRef.current) setDraft(confirmedSnapshotRef.current)
    setEditing(false)
  }
  async function action(name, path, payload = {}) {
    if (lock.current) return
    lock.current = true; setBusy(name); setError(''); const current = generation.current
    try {
      const result = await api(taskId, path, payload)
      if (current === generation.current) {
        setRun(result)
        const saved = result.baseline || draft
        confirmedSnapshotRef.current = saved
        baselineDraftCache.delete(taskId)
        setDraft(saved)
        setEditing(false)
      }
    } catch (e) { if (current === generation.current) setError(e.message) }
    finally { if (current === generation.current) { setBusy(''); lock.current = false } }
  }
  function choose(index) {
    const c = candidates[Number(index)]; if (!c) return
    const criticalFields = ['capacity_and_throughput', 'performance_range', 'accuracy_sensitivity_and_repeatability', 'temperature_environment_and_safety']
    const fieldNames = {capacity_and_throughput: '容量与处理量', performance_range: '性能范围', accuracy_sensitivity_and_repeatability: '精度与重复性', temperature_environment_and_safety: '温度与安全'}
    const parameters = (c.fields || []).filter(f => criticalFields.includes(f.field_name) && !['unknown', 'rejected', 'conflicted'].includes(f.status) && f.value).flatMap(f => (typeof f.value === 'object' && !Array.isArray(f.value) ? Object.entries(f.value) : [[fieldNames[f.field_name], f.value]]).map(([name, value]) => {
      const text = displayValue(value)
      const numeric = text.match(/^(?:±|≤|≥)?\s*(-?\d+(?:\.\d+)?)\s*([a-zA-Z℃°%/]+)$/)
      return { name, value: numeric ? numeric[1] : text, unit: numeric ? numeric[2] : f.unit || '', direction: /精度|误差|噪声/.test(name) ? 'max' : /最高转速|最大相对离心力/.test(name) ? 'min' : 'equal', conditions: name.includes('角转子') ? '角转子' : name.includes('水平转子') ? '水平转子' : '', sources: f.sources || [] }
    }))
    setDraft(d => ({ ...d, category: c.category || d.category || taskState.task.category || '', brand: c.brand || '', model: c.model || '', parameters: parameters.length ? parameters : [emptyParameter()], configuration: typeof c.configuration === 'string' ? c.configuration : JSON.stringify(c.configuration || '') }))
  }
  if (loading) return <LoadingLabel>正在恢复比较基准与研究进度</LoadingLabel>
  return <section className="module-panel procurement-panel" aria-label={mode === 'material' ? '材料与报价确认' : mode === 'price' ? '成交项目核价' : '其他品牌比较'}>
    <div className="module-title"><div><h2>{{material:'确认参数、配置与报价',price:'这份报价有成交依据吗？',alternatives:'有没有更适合的其他品牌？'}[mode]}</h2></div><div className="module-title-actions">
      {/* 核价页的启动/停止按钮只放在顶部这一处：底部再放一个同义按钮，用户就得先滑到底
          才能开始检索，而且两个都能点的时候不知道该按哪个（交互规范）。 */}
      {mode === 'price' && run && <ActionButton variant={['running', 'queued'].includes(run.status) ? 'secondary' : 'primary'} disabled={Boolean(busy) || editing || (!['running', 'queued'].includes(run.status) && run.lanes?.price === 'completed')} onClick={() => ['running', 'queued'].includes(run.status) ? action('cancel', `/research-runs/${run.id}/cancel`) : (run.lanes?.price === 'failed' || ['interrupted', 'cancelled'].includes(run.status)) ? action('retry', retryResearchLanePath(run.id, 'price'), { force: false }) : action('start', `/research-runs/${run.id}/start/price`)}>{['running', 'queued'].includes(run.status) ? '停止后续检索' : run.lanes?.price === 'completed' ? '本轮检索已结束' : run.lanes?.price === 'failed' ? '重试核价（断点续跑）' : '查找已成交项目'}</ActionButton>}
      <ActionButton variant="ghost" icon={null} disabled={Boolean(busy)} onClick={() => setReload(n => n + 1)}>刷新结果</ActionButton>
    </div></div>
    {refreshNotice && <InlineNotice title="已刷新">{refreshNotice}</InlineNotice>}
    {error && <InlineNotice tone="error" title="需要处理">{researchIssue(error).message}</InlineNotice>}
    {documentConflict && <InlineNotice title="比较基准要按产品资料更新">{documentConflict}</InlineNotice>}
    {mode === 'material' && (!run || editing) && draft.model && <section className="baseline-quick" aria-label="确认前摘要">
      <div className="baseline-quick-facts">
        <h3>{draft.category || '品类待补充'} · {draft.brand} {draft.model}</h3>
        <p className="baseline-quick-line">报价：{draft.supplier_price ?? '未提供'}{draft.supplier_price != null && draft.supplier_price !== '' && draft.currency === 'CNY' ? ' 元' : ''} · 数量：{draft.quantity || '未提供'} · 用途：{draft.scenario || '待补充'}{draft.configuration ? ` · 配置：${draft.configuration}` : ''}</p>
      </div>
      <div className="module-actions"><ActionButton disabled={Boolean(busy) || !draft.scenario} onClick={() => action('confirm', '/baseline/save', {...draft,parameters:draft.parameters.filter(p => (p.name || '').trim() || (p.value != null && p.value !== ''))})}>{busy === 'confirm' ? '正在保存' : '确认摘要，进入核价'}</ActionButton>{run && editing && <ActionButton variant="ghost" type="button" disabled={Boolean(busy)} onClick={cancelEditing}>取消修改</ActionButton>}</div>
    </section>}
    {mode !== 'material' && !run && <InlineNotice title="先确认采购条件">请先完成材料与报价确认，后续结果才会对应同一台仪器。<button className="text-action" onClick={() => openTab('资料')}>返回材料与报价</button></InlineNotice>}
    {mode === 'material' && (!run || editing) && <details className="baseline-review" open={editing || !run || undefined}><summary>核对并确认比较基准</summary><form onSubmit={e => {
      e.preventDefault()
      try {
        // 整行都空的指标行不送后端；一项都没有也照样能确认——用户不一定有必须满足的
        // 实验要求（图6，后端 procurement_research.confirm 已不再拦空列表）。这时本轮
        // 只按价格与来源比较，后端的参数比对和报告结论都会写明"没有确认可比指标"。
        const filled = draft.parameters.filter(p => (p.name || '').trim() || (p.value != null && p.value !== ''))
        const parameters = filled.map(p => ({ ...p, value: p.direction === 'covers' && typeof p.value === 'string' ? p.value.replace(/[\[\]]/g, '').split(/\s*(?:至|~|～|,|，)\s*/).map(Number) : p.value }))
        if (parameters.some(p => p.direction === 'covers' && (!Array.isArray(p.value) || p.value.length !== 2 || p.value.some(v => !Number.isFinite(v)) || p.value[0] > p.value[1]))) throw new Error('invalid range')
        action('confirm', '/baseline/save', { ...draft, parameters })
      } catch (e) { setError('范围请填写从小到大的两个数值，例如 0 至 50。') }
    }}>
      {!candidates.length && <InlineNotice title="可以先识别资料">上传并分析资料后可预填型号；也可在下方填写已经核对的销售推荐型号。<button type="button" className="text-action" onClick={() => openTab('资料')}>上传资料</button><button type="button" className="text-action" onClick={() => openTab('分析')}>分析已有资料</button></InlineNotice>}
      {candidates.length > 0 && <label>本轮比较对象<select defaultValue="" onChange={e => choose(e.target.value)}><option value="" disabled>选择销售推荐的主仪器</option>{candidates.map((c, i) => <option key={i} value={i}>{c.brand} {c.model}</option>)}</select></label>}
      <div className="procurement-form-grid">
        {[['scenario', '实际用途', true], ['category', '仪器品类', true], ['brand', '品牌', false], ['model', '完整型号', true], ['configuration', '销售推荐配置', false], ['supplier_price', '销售单价（元，可不填）', false], ['quantity', '采购数量（可不填）', false], ['tax', '税费条件', false], ['shipping', '运费条件', false], ['installation', '安装与培训', false], ['warranty', '保修期限', false], ['service_response', '报修响应时间', false], ['service_onsite', '上门处理时间', false], ['delivery', '交货时间', false], ['preferences', '希望具备的功能（可不填）', false]].map(([key, label, required]) => <label key={key}>{label}<input required={required} type={['supplier_price', 'quantity'].includes(key) ? 'number' : 'text'} min={key === 'supplier_price' ? '0' : key === 'quantity' ? '1' : undefined} step={key === 'supplier_price' ? '0.01' : undefined} value={draft[key] ?? ''} onChange={e => update(key, e.target.value)} /></label>)}
      </div>
      <label>报价币种<select value={draft.currency || ''} onChange={e => update('currency',e.target.value)}><option value="">用户未填写</option><option value="CNY">人民币</option></select></label>
      <h3>必须满足的实验要求（可不填）</h3>
      {draft.parameters.map((p, index) => <div className="procurement-parameter" key={index}>
        <label>指标名称<input value={p.name} onChange={e => updateParameter(index, 'name', e.target.value)} /></label>
        <label>数值或要求<input value={Array.isArray(p.value) ? p.value.join(' 至 ') : p.value} onChange={e => updateParameter(index, 'value', e.target.value)} /></label>
        <label>单位<input value={p.unit || ''} onChange={e => updateParameter(index, 'unit', e.target.value)} /></label>
        <label>比较方式<select value={p.direction || 'equal'} onChange={e => updateParameter(index, 'direction', e.target.value)}><option value="equal">一致</option><option value="min">不能低于</option><option value="max">不能高于</option><option value="covers">覆盖整个范围</option></select></label>
        <label>适用条件<input value={p.conditions || ''} onChange={e => updateParameter(index, 'conditions', e.target.value)} /></label>
        <button type="button" className="text-action" disabled={draft.parameters.length === 1 && (Boolean((p.name || '').trim()) || (p.value != null && p.value !== ''))} onClick={() => setDraft(d => ({ ...d, parameters: d.parameters.filter((_, i) => i !== index) }))}>移除</button>
      </div>)}
      <div className="module-actions"><ActionButton variant="ghost" type="button" onClick={() => setDraft(d => {
        const next = { ...d, parameters: [...d.parameters, emptyParameter()] }
        baselineDraftCache.set(taskId, { draft: next, dirty: true })
        return next
      })}>添加指标</ActionButton>{run && editing && <ActionButton variant="ghost" type="button" disabled={Boolean(busy)} onClick={cancelEditing}>取消修改</ActionButton>}<ActionButton type="submit" disabled={Boolean(busy)}>{busy === 'confirm' ? '正在保存' : '确认这些采购条件'}</ActionButton></div>
    </form></details>}
    {run && <>
      {mode !== 'price' && <dl className="result-facts baseline-identity"><div><dt>仪器品类</dt><dd>{run.baseline?.category || '尚未填写品类'}</dd></div><div><dt>{documentConflict ? '上次已确认基准（待更新）' : '比较基准'}</dt><dd>{run.baseline?.brand} {run.baseline?.model}</dd></div></dl>}
      {mode === 'price' && <PriceTargetOverview baseline={run.baseline || draft} conclusionStated={Boolean(run.price_assessment)} onViewCompare={() => {
        const target = run.baseline || draft
        const params = new URLSearchParams()
        if (target?.model) { params.set('model', target.model); params.set('exact', '1') }
        if (target?.brand) params.set('brand', target.brand)
        navigate(`/app/compare?${params.toString()}`)
      }} />}
      {mode === 'material' && <>{!editing && <p>销售单价：{run.baseline.supplier_price != null && run.baseline.supplier_price !== '' ? `${run.baseline.supplier_price} ${run.baseline.currency === 'CNY' ? '元' : ''}` : '未提供'} · 数量：{run.baseline.quantity || '未提供'} · 保修：{run.baseline.warranty || '未填写'}</p>}<div className="module-actions"><ActionButton onClick={() => openTab('核价与替代')}>下一步：成交项目核价</ActionButton>{!editing && <ActionButton variant="ghost" onClick={startEditing}>修改采购条件</ActionButton>}</div></>}
      {['queued', 'running'].includes(run.status) && <ResearchProgress run={run} clock={clock} />}
      {editing && mode !== 'material' && <InlineNotice tone="warning" title="你有尚未保存的条件修改">
        当前结果仍基于上次确认的条件，发起核价或比较前请先保存修改或取消。<div className="module-actions"><button type="button" className="text-action" onClick={() => openTab('资料')}>返回材料与报价保存</button><button type="button" className="text-action" onClick={cancelEditing}>放弃修改并解锁操作</button></div>
      </InlineNotice>}
      {mode !== 'material' && <ResearchResults run={run} baseline={run.baseline || draft} section={mode} onCandidatePrice={candidateId => lookupCandidatePrice({
        runId: run.id,
        candidateId,
        // 一次点击 = 一个请求：要查哪个由**路径里那一个 candidate_id**决定，请求体是
        // 空对象（刻意不传 ``force``——默认就是"已经查过就命中已有结果"，所以点第二次
        // 不会重复花钱）。请求形状的唯一来源在 services/alternativePriceLookup.js。
        post: (path, body) => api(taskId, path, body),
      })} />}
      {mode === 'price' && <section className="price-analysis-conclusion" aria-labelledby="price-conclusion-title"><h3 id="price-conclusion-title">分析结论</h3><p>{run.price_assessment || '先查已成交项目，再按型号、配置、数量和售后条件解释这份报价。'}</p></section>}
      {mode !== 'material' && <section className={mode === 'price' ? 'price-next-actions' : 'result-actions'} aria-label={mode === 'price' ? '下一步操作' : '结果操作'}>
        <div className="module-actions">
          {mode !== 'price' && (['running','queued'].includes(run.status) ? <ActionButton variant="secondary" disabled={Boolean(busy)} onClick={() => action('cancel', `/research-runs/${run.id}/cancel`)}>停止后续检索</ActionButton> : <ActionButton disabled={Boolean(busy) || editing || run.lanes?.alternatives === 'completed'} onClick={() => (run.lanes?.alternatives === 'failed' || ['interrupted', 'cancelled'].includes(run.status)) ? action('retry', retryResearchLanePath(run.id, 'alternatives'), { force: false }) : action('start', `/research-runs/${run.id}/start/alternatives`)}>{run.lanes?.alternatives === 'completed' ? '本轮检索已结束' : run.lanes?.alternatives === 'failed' ? '重试其他品牌（断点续跑）' : '查找并比较其他品牌'}</ActionButton>)}
          {!['running','queued','completed','ready'].includes(run.status) && <ActionButton variant="secondary" disabled={Boolean(busy) || editing} onClick={() => action('continue', `/research-runs/${run.id}/continue`)}>继续补查缺少的依据</ActionButton>}
          <ActionButton variant="ghost" onClick={() => openTab(mode === 'price' ? '替代产品' : '报告')}>{mode === 'price' ? '下一步：其他品牌比较' : '查看分析报告'}</ActionButton>
        </div>
      </section>}
      {mode === 'price' && !['running','queued'].includes(run.status) && <details className="supplier-revision"><summary>回填销售回复，更新本轮判断</summary><form onSubmit={e => {e.preventDefault(); action('supplier', `/research-runs/${run.id}/supplier-reply`, supplierReply)}}><div className="procurement-form-grid">{[['configuration','本次报价配置'],['supplier_price','更新后完整方案单价（元）'],['tax','税费'],['shipping','运费'],['installation','安装培训'],['warranty','质保期'],['service_response','报修响应时间'],['service_onsite','上门时间'],['delivery','交货时间']].map(([key,label]) => <label key={key}>{label}<input type={key === 'supplier_price' ? 'number' : 'text'} min={key === 'supplier_price' ? 0 : undefined} value={supplierReply[key] || ''} onChange={e => setSupplierReply(s => ({...s,[key]:e.target.value}))}/></label>)}</div>
        {/* 销售答"资料里没有"和答不上来的长文本，原来在这张表上没有位置：答了也进不了产品
            （接口只认上面这些固定字段，多出来的字段会被明确拒绝，不再静默丢弃）。 */}
        <fieldset className="supplier-not-provided"><legend>以下项目销售已回复「资料中未提供」</legend>{[['supplier_price','含税销售单价'],['tax','税率与发票'],['shipping','运费与运输方式'],['installation','安装培训'],['warranty','整机保修期'],['service_response','报修响应时间'],['service_onsite','上门服务'],['delivery','交货与安装时间']].map(([key,label]) => <label key={key}><input type="checkbox" checked={(supplierReply.not_provided || []).includes(key)} onChange={e => setSupplierReply(s => ({...s, not_provided: e.target.checked ? [...(s.not_provided || []), key] : (s.not_provided || []).filter(item => item !== key)}))}/>{label}</label>)}</fieldset>
        <label className="supplier-reply-notes">销售答复原文或其他说明<textarea value={supplierReply.reply_notes || ''} onChange={e => setSupplierReply(s => ({...s, reply_notes: e.target.value}))} placeholder="例如：资料提供主机订购清单；含税、运费、安装培训的完整报价未提供。"/></label>
        <ActionButton type="submit" disabled={Boolean(busy)}>保存回复并更新报告</ActionButton></form></details>}
      {run.parent_report_id && <p role="status">{run.supplier_revision
        ? '已根据销售回复更新采购条件，原报告已保留。'
        : `本轮为外部补证核验版本，原报告已保留。${run.supplement_results?.filter(r => r.status === 'unverified').length || 0} 条线索未通过核验。`}</p>}
      {mode !== 'material' && <details className="supplement-disclosure"><summary>补充资料与核验</summary><SupplementPanel taskId={taskId} run={run} api={api} disabled={Boolean(busy) || editing} onImported={value => { setRun(value); taskState.loadReports?.() }} /></details>}
    </>}
  </section>
}
