import React, { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, ArrowRight, BookOpen, Check, ClipboardList, FileCheck2, FileDown, KeyRound, Link2, Network, Rocket, Search, Server, ShieldCheck, SlidersHorizontal, Trash2, Wifi } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { ActionButton, EmptyState, InlineNotice, LoadingLabel, PageHeading, StatusTag } from '../components/ui'
import { useTask } from '../features/TaskContext'
import { fetchProviderStatus, providerHealthCheck } from '../services/api'
import ReportLibrary from './ReportLibrary'

export function HistoryPage() {
  const { task, tasks, loadTasks, reset } = useTask()
  // 页面上只有历史报告这一块；任务列表仍要加载：一是删掉的若是当前打开的任务，
  // 工作区不能继续停在它上面；二是删除确认框要知道选中的任务里有没有正在研究的。
  useEffect(() => { loadTasks() }, [loadTasks])
  return <div className="page-canvas"><PageHeading title="历史报告" />
    <ReportLibrary tasks={tasks} onTasksDeleted={async taskIds => {
      await loadTasks()
      if (task && taskIds.includes(task.id)) reset()
    }} />
  </div>
}

export function ReviewPage() {
  const { task, requirement, documents, candidates } = useTask()
  const navigate = useNavigate()
  const items = useMemo(() => {
    if (!task) return []
    const list = []
    if (requirement?.unknowns?.length) list.push({ title: '需求仍有未知项', detail: `${requirement.unknowns.length} 项条件需要采购方补充或确认`, target: '需求', tone: 'coral' })
    if (!documents.length) list.push({ title: '缺少采购资料', detail: '上传说明书或报价资料后才能识别候选', target: '资料', tone: 'blue' })
    if (documents.length && !candidates.length) list.push({ title: '候选尚未识别', detail: '已有资料，但尚未识别候选设备', target: '候选', tone: 'lime' })
    if (candidates.some(item => item.status !== 'confirmed' && item.status !== 'excluded')) list.push({ title: '候选型号待确认', detail: `${candidates.filter(item => item.status !== 'confirmed' && item.status !== 'excluded').length} 个候选需要核对品牌、完整型号和配置`, target: '候选', tone: 'coral' })
    return list
  }, [task, requirement, documents.length, candidates.length])
  return <div className="page-canvas"><PageHeading title="待复核" description="只列出当前任务中仍需人工处理的真实缺口。" meta={<StatusTag tone={items.length ? 'warning' : 'success'}>{items.length} 项待办</StatusTag>} />
    {!task ? <EmptyState icon={FileCheck2} title="没有可复核的任务" action={<ActionButton onClick={() => navigate('/app/new')}>新建选型</ActionButton>}>建立任务后，这里会汇总未知项、失败资料和待确认候选。</EmptyState> : !items.length ? <EmptyState icon={Check} title="当前没有待处理事项">可进入任务继续核对来源、产品参数和价格。</EmptyState> : <div className="review-list">{items.map((item, index) => <article key={item.title} className={`review-${item.tone}`}><span>{String(index + 1).padStart(2, '0')}</span><div><h2>{item.title}</h2><p>{item.detail}</p></div><button className="text-action" onClick={() => navigate(`/app/task/${task.id}?tab=${encodeURIComponent(item.target)}`)}>进入任务<ArrowRight size={14} /></button></article>)}</div>}
  </div>
}

const CONNECTION_STORAGE_KEY = 'labpilot.connection-settings.v2'
const supportedSearchModes = new Set(['model', 'cn', 'public', 'tavily'])
const defaultModel = { model: '', baseUrl: '', apiKey: '' }
const defaultSearch = { modes: ['cn', 'public'], tavilyKey: '' }

function readConnectionSettings() {
  if (typeof window === 'undefined') return { model: defaultModel, search: defaultSearch }
  try {
    const parsed = JSON.parse(window.localStorage.getItem(CONNECTION_STORAGE_KEY) || 'null')
    const validModel = parsed?.version === 2
      && typeof parsed?.model?.model === 'string'
      && typeof parsed?.model?.baseUrl === 'string'
      && typeof parsed?.model?.apiKey === 'string'
    const validSearch = Array.isArray(parsed?.search?.modes)
      && parsed.search.modes.every(mode => typeof mode === 'string' && supportedSearchModes.has(mode))
      && typeof parsed?.search?.tavilyKey === 'string'
    if (!validModel || !validSearch) throw new Error('invalid connection settings')
    return {
      model: { model: parsed.model.model, baseUrl: parsed.model.baseUrl, apiKey: parsed.model.apiKey },
      search: { modes: [...new Set(parsed.search.modes)], tavilyKey: parsed.search.tavilyKey },
    }
  } catch {
    return { model: { ...defaultModel }, search: { ...defaultSearch, modes: [...defaultSearch.modes] } }
  }
}

export function ApiSettingsPage() {
  const initialSettings = useMemo(readConnectionSettings, [])
  const [model, setModel] = useState(initialSettings.model)
  const [search, setSearch] = useState(initialSettings.search)
  const [status, setStatus] = useState({ model: 'unavailable', search: 'unavailable', label: '服务未连接' })
  const [testing, setTesting] = useState('')
  const [message, setMessage] = useState('')

  useEffect(() => {
    fetchProviderStatus().then(data => {
      const byId = Object.fromEntries((data.items || []).map(item => [item.id, item.status]))
      setStatus(current => ({ ...current, model: byId.reasoning || data.status, search: byId.search || data.status, label: data.label || current.label }))
    }).catch(error => {
      // 读不到连接状态时别停在初始的"服务未连接"：后端会给出带 code 的中文原话
      // （例如"配置项 LAB_VISION_JSON_RETRIES 的值 9 超出允许范围 0～3"），照原样显示。
      setStatus(current => ({ ...current, label: '配置有误' }))
      setMessage(error?.message ? `读取连接状态失败：${error.message}` : '读取连接状态失败，请稍后重试。')
    })
  }, [])

  function persistSettings(nextModel, nextSearch) {
    try {
      window.localStorage.setItem(CONNECTION_STORAGE_KEY, JSON.stringify({ version: 2, model: nextModel, search: nextSearch }))
    } catch {
      setMessage('连接设置无法写入浏览器存储。请检查浏览器权限，或保持本页开启。')
    }
  }

  function updateModel(patch) {
    const nextModel = { ...model, ...patch }
    setModel(nextModel)
    persistSettings(nextModel, search)
  }

  function updateSearch(patch) {
    const nextSearch = { ...search, ...patch }
    setSearch(nextSearch)
    persistSettings(model, nextSearch)
  }

  async function test(kind) {
    setMessage('')
    setTesting(kind)
    if (kind === 'search' && !search.modes.length) {
      setMessage('至少选择一种联网方式后再测试。')
      setTesting('')
      return
    }
    const payload = kind === 'model'
      ? { model: model.model, base_url: model.baseUrl, api_key: model.apiKey }
      : {
          model: model.model,
          base_url: model.baseUrl,
          api_key: model.apiKey,
          modes: search.modes,
          credentials: search.modes.includes('tavily') && search.tavilyKey ? { tavily: search.tavilyKey } : {},
        }
    try {
      const result = await providerHealthCheck(kind === 'model' ? 'reasoning' : 'search', payload)
      setStatus(current => ({ ...current, [kind]: result.status, label: result.label || current.label }))
      window.dispatchEvent(new Event('labpilot-provider-change'))
      setMessage(result.status === 'unavailable' ? '当前服务未连接。设置已保存在此浏览器，可检查后重试。' : '连接测试已完成，设置已保存在此浏览器。')
    } catch (error) {
      setStatus(current => ({ ...current, [kind]: 'error' }))
      setMessage(`连接测试未完成：${error.message}`)
    } finally {
      setTesting('')
    }
  }

  function toggleSearchMode(mode) {
    const modes = search.modes.includes(mode) ? search.modes.filter(item => item !== mode) : [...search.modes, mode]
    updateSearch({ modes })
  }

  function clearSettings() {
    window.localStorage.removeItem(CONNECTION_STORAGE_KEY)
    setModel({ ...defaultModel })
    setSearch({ ...defaultSearch, modes: [...defaultSearch.modes] })
    setStatus({ model: 'unavailable', search: 'unavailable', label: '服务未连接' })
    setMessage('已清除此浏览器保存的模型与联网设置。')
  }

  const searchNeedsKey = search.modes.includes('tavily')
  return <div className="page-canvas api-page"><PageHeading title="连接设置" description="配置思考模型和可并行使用的联网来源。设置保存在当前浏览器，关闭或离开页面不会自动清除。" meta={<StatusTag tone={status.model === 'configured' ? 'success' : 'prototype'}>{status.label}</StatusTag>} />
    {message && <InlineNotice tone="warning" title="连接测试结果">{message}</InlineNotice>}
    <div className="api-columns">
      <section className="api-panel model-panel"><div className="api-panel-heading"><span><Server size={20} /></span><div><h2>思考模型</h2><p>用于采购对话和资料分析。连接测试仅作简短验证，不运行采购分析。</p></div><StatusTag tone={status.model === 'configured' ? 'success' : 'prototype'}>{status.model === 'configured' ? '已连接' : '未连接'}</StatusTag></div>
        <label>模型名称<input value={model.model} onChange={event => updateModel({ model: event.target.value })} placeholder="例如：gpt-5" autoComplete="off" /></label>
        <label>Base URL<input value={model.baseUrl} onChange={event => updateModel({ baseUrl: event.target.value })} placeholder="https://api.example.com/v1" inputMode="url" autoComplete="off" /></label>
        <label>API Key<div className="secret-field"><KeyRound size={16} /><input type="password" value={model.apiKey} onChange={event => updateModel({ apiKey: event.target.value })} placeholder="输入模型 API Key" autoComplete="new-password" /></div><small>加密传输取决于 Base URL 是否使用 HTTPS；Key 会写入此浏览器的专用本地存储键。</small></label>
        <ActionButton variant="secondary" icon={testing === 'model' ? null : Link2} disabled={testing === 'model'} onClick={() => test('model')}>{testing === 'model' ? <LoadingLabel>正在测试连接</LoadingLabel> : '测试连通'}</ActionButton>
      </section>
      <section className="api-panel search-panel"><div className="api-panel-heading"><span><Network size={20} /></span><div><h2>联网能力</h2><p>默认使用公开网页搜索。兼容接口可在启用模型联网时返回带来源结果；端点未启用时仍可使用公开网页来源。</p></div><StatusTag tone={status.search === 'configured' ? 'success' : 'prototype'}>{status.search === 'configured' ? '已连接' : '未连接'}</StatusTag></div>
        <fieldset><legend>联网方式</legend>{[
          ['model', '模型自带联网', '由模型返回带来源的公开网页结果'],
          ['cn', '中文公开网页', '检索中文产品资料与公开页面'],
          ['public', '英文公开网页', '检索英文厂商与公开资料页面'],
          ['tavily', 'Tavily', '使用独立 Key 检索公开网页'],
        ].map(([value, label, description]) => <label className="connection-option" key={value}><input type="checkbox" value={value} checked={search.modes.includes(value)} onChange={() => toggleSearchMode(value)} /><span><strong>{label}</strong><small>{description}</small></span></label>)}</fieldset>
        {searchNeedsKey && <label>Tavily API Key<div className="secret-field"><KeyRound size={16} /><input type="password" value={search.tavilyKey} onChange={event => updateSearch({ tavilyKey: event.target.value })} placeholder="输入 Tavily 专用 Key" autoComplete="new-password" /></div><small>此 Key 独立于思考模型 API Key，仅在选择 Tavily 时发送给连接测试接口。</small></label>}
        <ActionButton variant="secondary" icon={testing === 'search' ? null : Wifi} disabled={testing === 'search'} onClick={() => test('search')}>{testing === 'search' ? <LoadingLabel>正在测试连接</LoadingLabel> : '测试联网能力'}</ActionButton>
      </section>
    </div>
    <div className="api-security"><ShieldCheck size={19} /><div><strong>本地保存边界</strong><p>连接设置只保存在当前浏览器；连接时将密钥交给本机服务临时使用，不写入项目文件或任务记录。同一浏览器中的脚本可能读取本地存储，请只在可信设备使用，并在共用设备上及时清除。</p></div><ActionButton variant="ghost" icon={Trash2} onClick={clearSettings}>清除本地设置</ActionButton></div>
  </div>
}

const helpSections = [
  {
    id: 'start', number: '01', title: '开始一次采购', icon: Rocket, tone: 'blue',
    intro: '先把采购场景写完整，再让任务进入工作区。这里不要求一次写出标准答案，重要的是把已经确定的条件和仍然缺失的条件分开。',
    paragraphs: [
      '打开“新建选型”，在采购场景中写下设备用途、样品类型、处理量、预算范围、实验环境和交付要求。可以直接粘贴已有的采购申请，也可以先写一段草稿，进入任务后再补充。',
      '提交后先在“需求”页编辑必须满足的条件、优先考虑的条件和待补充内容。确认后自动检索，无需先上传文件或确定型号。',
    ],
    steps: ['描述设备和使用场景', '补充预算、通量、环境、交付与售后', '检查结构化需求，再确认版本'],
    note: '无法连接时，页面会显示错误原因，不会用示例任务代替真实结果。', action: '打开新建选型', target: '/app/new',
  },
  {
    id: 'materials', number: '02', title: '准备采购资料', icon: ClipboardList, tone: 'lime',
    intro: '资料是候选识别和后续核对的起点。文件可以不完整，但来源和缺口要留下来。',
    paragraphs: [
      '支持上传说明书、报价单、参数表、图片和常见表格文件。建议保留供应商原文件名，不要把多家供应商的资料合并成一张截图；这样更容易回到原文核对页码、型号和配置。',
      '新建任务和任务资料页都提供“产品资料”“报价资料”“其他附件”三个独立入口。对话底部的回形针只负责前往资料页，避免跳过资料分类。解析完成后，资料列表会显示类别、文件名、页数、视觉识别状态和文本摘录。',
    ],
    steps: ['保留原始文件和文件名', '检查解析警告、页码与 OCR 状态', '发现上传失败时按原提示重新上传'],
    note: '没有来源页码、完整型号或明确配置的内容，后续仍会标记为待确认。', action: '进入当前任务', target: '/app/new',
  },
  {
    id: 'status', number: '03', title: '读懂状态', icon: SlidersHorizontal, tone: 'coral',
    intro: '状态词描述的是当前证据和处理阶段，不是对设备好坏的判断。颜色只作辅助，页面会同时给出文字。',
    paragraphs: [
      '“已确认”表示采购方已经确认这一版需求或资料状态；“待确认”表示仍需要人工核对；“未知”表示当前资料没有给出足够信息；“冲突”表示不同来源的说法不一致，需要回到原文处理。',
      '侧栏的“待复核”会汇总当前任务中的未知项、资料缺失和候选确认项。证据、适配或联网服务尚未接入时，相关区域会明确显示“未连接”，不会显示虚假进度。',
    ],
    steps: ['先看文字状态，再看颜色标记', '把未知项当成需要补充的问题', '不要把解析摘录直接当作采购事实'],
    note: '状态改变需要有对应的用户确认、资料更新或处理结果，页面不会凭空推进阶段。', action: '查看待复核', target: '/app/review',
  },
  {
    id: 'compare', number: '04', title: '完成一次比选', icon: Search, tone: 'green',
    intro: '按照需求、资料、候选、证据、适配、报价和报告的顺序推进，能减少“先看价格、后补条件”的反复。',
    paragraphs: [
      '候选识别只读取当前任务资料中的品牌、型号和配置。识别完成后，先确认完整型号和版本，再逐项核对必须满足的条件。必须满足的条件没有证据支持时，候选不能进入可靠推荐。',
      '推荐与报价区域会把缺口留在页面上。历史招标价、经销商报价和当前供应商报价需要分开记录；税费、安装、附件、培训、维保和交付条件也不要合并成一个数字。',
    ],
    steps: ['从资料识别候选', '核对型号、必须满足的条件和证据来源', '拆开比较报价口径，再决定是否形成推荐'],
    note: '当前证据和适配接口尚未接入时，正确结果是“不能形成推荐”。', action: '进入工作台', target: '/app/new',
  },
  {
    id: 'api', number: '05', title: '配置模型与联网能力', icon: Network, tone: 'blue',
    intro: '连接设置页用于连接本次运行所需的思考模型和联网能力。服务尚未接通时，页面会保留“未连接”状态，不会伪造成功。',
    paragraphs: [
      '思考模型用于需求整理、阶段计划和文字解释。打开“API 设置”，填写模型名称、Base URL 和 API Key。Base URL 应填写兼容接口的根地址，例如以 /v1 结尾的地址；不要把具体模型路径重复拼到 URL 后面。',
      '联网方式可以多选模型自带联网、中文公开网页、英文公开网页和 Tavily。选择 Tavily 后会显示它的独立 Key 输入框；模型联网则复用思考模型的连接信息。测试按钮只检查当前配置，没有可用服务时会保留“未连接”状态。',
      '模型连接和联网配置保存在当前浏览器，离开页面不会重置。连接时密钥仅在本机服务内存中临时使用，不写入项目文件或任务记录。共用设备上完成操作后，应使用“清除本地设置”。',
    ],
    steps: ['先确认 Base URL、模型名和模型 Key 可用', '按需要勾选一个或多个联网来源', '共用设备使用后清除本地设置'],
    note: '连接测试返回成功前，不要把模型或搜索结果当成已经接入。', action: '打开 API 设置', target: '/app/api',
  },
  {
    id: 'recovery', number: '06', title: '失败、导出与版本', icon: FileDown, tone: 'coral',
    intro: '失败时先保留原因，再处理输入。任务内容不会因为一次失败被覆盖，修正后可以从当前状态继续。',
    paragraphs: [
      '上传失败时，保留文件并查看错误文字；解析失败时，优先确认文件是否损坏、是否需要 OCR，以及是否有更清晰的原件。候选为空不代表没有合适设备，只说明当前资料没有识别出可用型号。',
      '报告生成后，请复核必须满足的条件、来源、价格口径和待确认项。历史页面记录的是已保存的历史版本，旧版本保持只读；新的报告会从当前任务状态建立新的记录，不会改写旧版本。',
    ],
    steps: ['按错误原因修正输入后重试', '导出前检查来源、缺口和人工复核项', '从历史记录确认版本和修订内容'],
    note: '服务不可用会显示为失败或未连接，不会包装成成功结果。', action: '查看历史记录', target: '/app/history',
  },
]

export function HelpPage() {
  const navigate = useNavigate()
  const [activeSection, setActiveSection] = useState(helpSections[0].id)

  useEffect(() => {
    const elements = helpSections.map(section => document.getElementById(`help-${section.id}`)).filter(Boolean)
    const observer = new IntersectionObserver(entries => {
      const visible = entries.filter(entry => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0]
      if (visible) setActiveSection(visible.target.id.replace('help-', ''))
    }, { rootMargin: '-15% 0px -65% 0px', threshold: [0, .2, .5, .9] })
    elements.forEach(element => observer.observe(element))
    return () => observer.disconnect()
  }, [])

  function jumpTo(id) {
    document.getElementById(`help-${id}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    setActiveSection(id)
  }

  return <div className="page-canvas help-page"><PageHeading title="使用说明" description="从建立任务到配置 API，按真实采购的顺序说明每一步怎么操作、结果从哪里来，以及什么时候需要人工复核。" meta={<StatusTag tone="neutral"><BookOpen size={13} />6 个章节</StatusTag>} />
    <div className="help-doc-layout">
      <aside className="help-toc" aria-label="使用说明目录">
        <div className="help-toc-label"><BookOpen size={15} />目录</div>
        <nav>{helpSections.map(section => <button key={section.id} className={activeSection === section.id ? 'active' : ''} onClick={() => jumpTo(section.id)}><span>{section.number}</span><strong>{section.title}</strong><ArrowRight size={14} aria-hidden="true" /></button>)}</nav>
        <div className="help-toc-note"><span>需要直接操作？</span><button onClick={() => navigate('/app/new')}>建立采购任务<ArrowRight size={14} /></button></div>
      </aside>
      <main className="help-document">{helpSections.map(section => <article id={`help-${section.id}`} className={`help-section help-section-${section.tone}`} key={section.id}>
        <div className="help-section-marker"><span>{section.number}</span><section.icon size={20} aria-hidden="true" /></div>
        <div className="help-section-body"><div className="help-section-title"><h2>{section.title}</h2><span>章节 {section.number}</span></div><p className="help-section-intro">{section.intro}</p>{section.paragraphs.map(paragraph => <p key={paragraph}>{paragraph}</p>)}<div className="help-section-steps"><h3>操作顺序</h3><ol>{section.steps.map(step => <li key={step}>{step}</li>)}</ol></div><div className="help-section-note"><AlertTriangle size={15} /><span>{section.note}</span></div><button className="text-action" onClick={() => navigate(section.target)}>{section.action}<ArrowRight size={14} /></button></div>
      </article>)}</main>
    </div>
  </div>
}
