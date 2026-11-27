import React, { useEffect, useRef, useState } from 'react'
import { ActionButton, InlineNotice } from '../components/ui'
import { researchIssue } from '../services/researchCopy'
import { publicText } from '../services/publicText'

export default function SupplementPanel({ taskId, run, api, onImported, disabled }) {
  const [gate, setGate] = useState(null)
  const [prompt, setPrompt] = useState('')
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const generation = useRef(0)
  const lock = useRef(false)
  const prefix = `/research-runs/${run.id}/supplement`
  useEffect(() => {
    const current = ++generation.current
    const controller = new AbortController()
    setGate(null); setPrompt(''); setInput(''); setError(''); setBusy(''); setNotice(''); lock.current = false
    if (['partial', 'completed'].includes(run.status)) {
      api(taskId, prefix, undefined, controller.signal).then(value => {
        if (current === generation.current) setGate(value)
      }).catch(e => { if (e.name !== 'AbortError' && current === generation.current) setError('补证条件读取失败，请重新读取研究状态。') })
    }
    return () => { controller.abort(); generation.current++ }
  }, [taskId, run.id, run.round, run.status])

  async function act(kind) {
    if (lock.current) return
    lock.current = true; setBusy(kind); setError(''); setNotice('')
    const current = generation.current
    try {
      let payload = {}
      if (kind === 'import') {
        if (input.length > 200000) throw new Error('补充资料超过20万字符，请仅保留相关线索。')
        try { payload = JSON.parse(input.replace(/^\s*```(?:json)?\s*|\s*```\s*$/g, '')) } catch { throw new Error('JSON格式有误，请粘贴Agent返回的完整JSON。') }
        if (!payload || Array.isArray(payload) || typeof payload !== 'object') throw new Error('请粘贴包含report_id和sources的JSON对象。')
      }
      const result = await api(taskId, `${prefix}/${kind}`, payload)
      if (current !== generation.current) return
      if (kind === 'prompt') setPrompt(result.prompt)
      else { onImported(result); setNotice('核验结果已另存新报告，原报告保留。') }
    } catch (e) { if (current === generation.current) setError(e.message) }
    finally { if (current === generation.current) { lock.current = false; setBusy('') } }
  }
  if (run.supplement_retryable && run.parent_run_id) return <InlineNotice tone="error" title="补证核验尚未完成">
    <p>已有结果已保留。请返回原报告，重新导入同一份资料；已成功读取的来源会复用，暂未完成的部分会继续核验。</p>
    {error && <p>{error}</p>}
    <ActionButton variant="secondary" disabled={Boolean(busy) || disabled} onClick={async () => {
      if (lock.current) return
      lock.current = true; setBusy('retry'); setError('')
      const current = generation.current
      try { const parent = await api(taskId, `/research-runs/${run.parent_run_id}`); if (current === generation.current) onImported(parent) }
      catch (e) { if (current === generation.current) setError(e.message) }
      finally { if (current === generation.current) { lock.current = false; setBusy('') } }
    }}>返回原报告重试导入</ActionButton>
  </InlineNotice>
  if (!gate?.allowed) return error ? <InlineNotice tone="error" title="补证入口暂不可用">{error}</InlineNotice>
    : gate && run.status === 'partial' ? <InlineNotice title="补证条件">{researchIssue(gate.reason).message}</InlineNotice> : null
  return <section className="supplement-panel" aria-label="外部Agent补充检索">
    <h3>补充缺少的证据</h3>
    <p>本项目已完成本轮研究并保存报告。可把具体缺口交给其他通用AI Agent补查，再导入核验。</p>
    {/* gate.gaps[].missing[] 里，字符串那一路就是「来源读取失败的原因」原文
        （source['reason']，后端 supplemental_research.py:40 直接放进来），
        形如「该来源连续两次无法读取（empty_body），其他来源可正常访问；保留URL及原始失败原因待补查」。
        全量实测 376 条字符串里 28 条带内部代号。跟 error_reason 一样用 publicText 做句内替换：
        只换括号里的代号，句子其余部分一字不动（别用 researchIssue —— 那是整句话术，
        而且这一列是「、」连接的短条目，塞整句会读不通）。
        对象那一路是参数名（{name, value, unit…} 的 name），不是代号，原样输出。 */}
    <ul>{gate.gaps.map((g, i) => <li key={i}>{g.model || '替代产品'}：{g.missing.map(m => typeof m === 'string' ? publicText(m, {}) : m.name).join('、')}</li>)}</ul>
    {error && <InlineNotice tone="error" title="需要处理">{error}</InlineNotice>}
    <ActionButton variant="secondary" disabled={Boolean(busy) || disabled} onClick={() => act('prompt')}>{busy === 'prompt' ? '正在生成' : '生成补充检索提示词'}</ActionButton>
    {prompt && <>
      <label className="form-field">复制给其他Agent的提示词<textarea rows={12} readOnly value={prompt} onFocus={e => e.target.select()} /></label>
      <p>选中并复制提示词，自行交给其他Agent。本项目不会自动代发资料。返回内容必须包含原始来源网址；导入将优先复用快照和缓存，原始来源无法读取时会明确说明。</p>
      <label className="form-field">导入补充资料（JSON）<textarea rows={10} value={input} maxLength={200001} onChange={e => setInput(e.target.value)} placeholder="粘贴其他Agent返回的JSON" /></label>
      <label className="form-field">或选择JSON文件<input type="file" accept=".json,application/json" disabled={Boolean(busy) || disabled} onChange={async e => {
        const file = e.target.files?.[0]; if (!file) return
        const current = generation.current
        if (file.size > 800000) { setError('文件过大，请仅保留相关线索。'); return }
        try { const text = await file.text(); if (current === generation.current) setInput(text) } catch { if (current === generation.current) setError('文件读取失败，请重新选择。') }
      }} /></label>
      <ActionButton disabled={!input.trim() || Boolean(busy) || disabled} onClick={() => act('import')}>{busy === 'import' ? '正在核验并另存报告' : '导入补充资料并核验'}</ActionButton>
    </>}
    {notice && <p role="status">{notice}</p>}
  </section>
}
