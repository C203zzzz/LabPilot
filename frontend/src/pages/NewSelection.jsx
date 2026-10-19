import React, { useRef, useState } from 'react'
import { ArrowRight, FileText, Paperclip, PackageOpen, ReceiptText, ShieldCheck, Sparkles, X } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { ActionButton, InlineNotice, LoadingLabel, PageHeading, PilotBeacon, StatusTag } from '../components/ui'
import { useTask } from '../features/TaskContext'
import { jsonRequest } from '../services/api'

const materialCategories = [
  { value: 'product', label: '产品资料', description: '说明书、参数表、产品图片', icon: PackageOpen },
  { value: 'quote', label: '报价资料', description: '报价单、价格截图、商务条款', icon: ReceiptText },
  { value: 'other', label: '其他附件', description: '检测材料、补充说明与表格', icon: Paperclip },
]

export default function NewSelection() {
  const { scenario, setScenario, createTask, busy, error, setError } = useTask()
  const [files, setFiles] = useState([])
  const [starting, setStarting] = useState(false)
  const startLock = useRef(false)
  const inputRefs = useRef({})
  const navigate = useNavigate()

  async function handleSubmit(event) {
    event.preventDefault()
    if (startLock.current) return
    startLock.current = true; setStarting(true)
    const taskId = await createTask(scenario, files)
    if (taskId) {
      try { await jsonRequest(`/tasks/${taskId}/intake`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({message:scenario,revision:0,initialize:true})}) }
      catch { setError('任务已保存，暂未自动整理产品信息。可以在对话中继续补充，或直接核对比较基准。') }
      navigate(`/app/task/${taskId}?tab=${encodeURIComponent('资料')}`)
    }
    startLock.current = false; setStarting(false)
  }

  function addFiles(category, event) {
    const selectedFiles = Array.from(event.target.files, file => ({ file, category }))
    setFiles(current => [...current, ...selectedFiles])
    event.target.value = ''
  }

  return <div className="page-canvas new-page chat-home">
    <div className="chat-welcome"><h1>这次实验，需要什么仪器？</h1><p>发来型号、销售报价或一份 PDF。我们一起核对配置、查成交，再比较其他品牌。</p></div>
    <div className="home-workflow" aria-label="采购流程"><span>材料与报价</span><ArrowRight size={16}/><span>成交项目核价</span><ArrowRight size={16}/><span>其他品牌比较</span><ArrowRight size={16}/><span>分析报告</span></div>
    {error && <InlineNotice tone="error" title="需要继续处理">{error}</InlineNotice>}
    <form className="home-composer" onSubmit={handleSubmit}>
      <label htmlFor="scenario">告诉我产品或采购需求</label>
      <textarea id="scenario" rows="3" maxLength={2000} value={scenario} onChange={event => setScenario(event.target.value)} placeholder="例如：生化培养箱，泰斯特 SPX-150BIII，1台，报价6800元。" />
      <div className="pending-files" aria-live="polite">{files.map((entry,index) => <div key={index}><FileText size={15}/><span>{entry.file.name}</span><button type="button" aria-label={`移除 ${entry.file.name}`} onClick={() => setFiles(current => current.filter((_,i) => i !== index))}><X size={15}/></button></div>)}</div>
      <div className="home-composer-actions"><div className="home-upload-actions">{materialCategories.map(({value,label}) => <label key={value} className="compact-upload"><Paperclip size={16}/>{label}<input type="file" multiple accept=".pdf,.jpg,.jpeg,.png,.webp,.xlsx,.csv,.txt" aria-label={`选择${label}`} disabled={Boolean(busy) || starting} onChange={event => addFiles(value,event)}/></label>)}</div><ActionButton type="submit" disabled={!scenario.trim() || Boolean(busy) || starting} icon={ArrowRight}>{busy || starting ? '正在整理' : '开始对话'}</ActionButton></div>
      <small>资料可稍后补充。未知条件可以跳过，所有结论保留来源。</small>
    </form>
  </div>
}
