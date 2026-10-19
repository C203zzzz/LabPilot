import React, { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { ArrowUp, Paperclip, ChevronUp, FlaskConical } from 'lucide-react'
import { ActionButton, InlineNotice, LoadingLabel } from '../components/ui'
import { jsonRequest } from '../services/api'
import { documentIdentityLead } from '../services/procurementIdentity'

const labels = {category:'品类', brand:'品牌', model:'型号', scenario:'实验用途', requirements:'关键要求', recording:'记录与导出', supplier_price:'销售单价', currency:'币种', quantity:'数量', configuration:'报价配置', tax:'税费', shipping:'运费', installation:'安装培训', warranty:'保修期限', service_response:'报修响应', service_onsite:'上门处理', delivery:'交货时间'}

export default function ProcurementIntake({ taskId, onUpdated, showConversation = true, onUpload, analysisId, composerHost }) {
  const [state, setState] = useState(null)
  const [answer, setAnswer] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const guard = useRef(false)
  const generation = useRef(0)
  const [pending, setPending] = useState('')
  const [historyLimit, setHistoryLimit] = useState(6)
  const [arrived, setArrived] = useState(false)
  const latest = useRef(null)
  const input = useRef(null)
  useEffect(() => {
    if (!arrived && !busy) return
    latest.current?.scrollIntoView({block:'nearest', behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth'})
  }, [state?.revision, busy, showConversation])
  useEffect(() => {
    if (!input.current) return
    input.current.style.height = 'auto'
    input.current.style.height = `${Math.min(144, Math.max(44, input.current.scrollHeight))}px`
  }, [answer, Boolean(state), composerHost])
  useEffect(() => {
    const current = ++generation.current
    setState(null); setError(''); setAnswer(''); setPending(''); setArrived(false); setHistoryLimit(6); setBusy(false); guard.current = false
    jsonRequest(`/tasks/${taskId}/intake`).then(data => { if (current === generation.current) setState(data) })
      .catch(e => { if (current === generation.current) setError(e.message) })
    return () => { generation.current++ }
  }, [taskId, analysisId])
  async function send(defer = false) {
    if (!state || guard.current || (!answer.trim() && !defer)) return
    guard.current = true; setBusy(true); setError('')
    const current = generation.current
    const message = answer.trim() || '销售暂未说明，先保留缺项。'
    setPending(message); setAnswer('')
    try {
      const data = await jsonRequest(`/tasks/${taskId}/intake`, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({message, revision:state.revision, defer})})
      if (current !== generation.current) return
      setState(data); setArrived(true); onUpdated(data.draft)
    } catch (e) { if (current === generation.current) { setError(e.message); if (!defer) setAnswer(message) } }
    finally { if (current === generation.current) { guard.current = false; setBusy(false); setPending('') } }
  }
  const question = state?.question.group === 'review' ? '可以继续补充销售回复，也可以核对下方采购条件，开始查价格和比较产品。' : state?.question.text
  const form = state && <form className="task-chat-composer" onSubmit={e => {e.preventDefault(); send()}}>
    {error && <InlineNotice tone="error" title="本次未保存">{error}</InlineNotice>}
    <label htmlFor="intake-answer" className="visually-hidden">补充采购信息或销售回复</label>
    <textarea ref={input} id="intake-answer" rows="1" maxLength={6000} value={answer} onChange={e => setAnswer(e.target.value)} placeholder={showConversation ? '说说你的需求，或粘贴销售的回复…' : '对结果有疑问？补充需求或销售回复…'} disabled={busy} onKeyDown={e => {if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {e.preventDefault(); send()}}} />
    <div className="composer-toolbar">{onUpload && <label className="compact-upload"><Paperclip size={18} aria-hidden="true"/>添加资料<input aria-label="对话上传产品资料" type="file" multiple accept=".pdf,.png,.jpg,.txt,.xlsx" disabled={busy} onChange={e => {onUpload(Array.from(e.target.files)); e.target.value=''}}/></label>}<span className="composer-key-hint">Enter 发送 · Shift + Enter 换行</span><div className="composer-send-actions">{state.question.group !== 'review' && <ActionButton type="button" variant="ghost" icon={null} disabled={busy} onClick={() => send(true)}>先跳过</ActionButton>}<button className="composer-send-button" aria-label={busy ? '正在保存回复' : '发送并保存'} title="发送" type="submit" disabled={busy || !answer.trim()}><ArrowUp size={20} aria-hidden="true"/></button></div></div>
    {!showConversation && <span className="composer-context">补充后会更新采购条件，已有报告仍会保留。</span>}
  </form>
  return <section className={`intake-conversation${showConversation ? '' : ' composer-only'}`} aria-label="采购需求对话">
    {showConversation && <h2>采购对话</h2>}
    {!state && error && <InlineNotice tone="error" title="对话读取失败">{error}</InlineNotice>}
    {!state ? <LoadingLabel>正在读取采购对话</LoadingLabel> : <>
      {showConversation && state.document_identity && <p className="decision-lead">{documentIdentityLead(state.document_identity)}</p>}
      {showConversation && state.document_candidates?.length > 0 && <p className="decision-lead">这次资料里有多个候选型号，没有自动替换比较基准；请在下方对话里说明要用哪一个，或先去“分析”页排除不对的产品。</p>}
      {showConversation && <div className="intake-history" role="log" aria-label="采购对话记录" aria-live="polite">
        {state.messages.length > historyLimit && <button className="history-earlier" onClick={() => setHistoryLimit(n => n + 8)}><ChevronUp size={16}/>查看较早的对话</button>}
        {state.messages.slice(-historyLimit).map((m,i) => <div className={`intake-message ${m.role}`} aria-label={m.role === 'user' ? '已发送的消息' : '助手回复'} key={`${state.messages.length-historyLimit+i}:${m.content}`}>{m.role !== 'user' && <span className="msg-avatar" aria-hidden="true"><FlaskConical size={15} /></span>}<p>{m.content}</p></div>)}
        <div className={`intake-message assistant${arrived && !busy ? ' message-arrived' : ''}`} key={`reply-${state.revision}`}><span className="msg-avatar" aria-hidden="true"><FlaskConical size={15} /></span>{state.reply && <p>{state.reply}</p>}<p>{question}</p></div>
        {busy && <><div className="intake-message user message-arrived"><p>{pending}</p></div><div className="intake-message assistant message-waiting" role="status"><span className="msg-avatar" aria-hidden="true"><FlaskConical size={15} /></span><span className="reply-dots" aria-hidden="true"><i/><i/><i/></span><span>正在整理这条信息</span></div></>}
        <div ref={latest}/>
      </div>}
      {showConversation && Object.keys(state.facts).length > 0 && <details className="intake-summary"><summary>已整理的产品与采购条件</summary><dl className="result-facts intake-facts">{Object.entries(state.facts).map(([k,v]) => <div key={k}><dt>{labels[k] || k}</dt><dd>{k === 'currency' && v.value === 'CNY' ? '人民币' : String(v.value)}{k === 'supplier_price' && state.facts.currency?.value === 'CNY' ? ' 元/台' : ''}</dd></div>)}</dl></details>}
      {composerHost ? createPortal(form, composerHost) : form}
      {showConversation && <p className="result-note">回答已保存为采购条件草稿，确认后用于比较。销售陈述与厂家事实分别记录。</p>}
    </>}
  </section>
}
