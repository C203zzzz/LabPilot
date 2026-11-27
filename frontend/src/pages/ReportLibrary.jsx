import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { Download, FileText, FolderOpen, RefreshCw, Search, Trash2 } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { ActionButton, EmptyState, InlineNotice, LoadingLabel } from '../components/ui'
import { taskApi } from '../services/api'
import { REPORT_BATCH_LIMIT, reportTaskDeletion } from '../services/reportBatch'
import { changeLines, diffGroups, unchangedSummary } from '../services/reportDiff'

// 注意删除口径：勾的是报告行，删的是这些报告所属的**整项任务**——连同任务
// 名下全部报告、原始资料和价格记录，不可恢复（POST /tasks/delete）。所以确认
// 框必须同时给出"报告数"和"任务数"，两者不等时尤其要说清多删了什么；如果其中
// 有任务还在研究，要照旧提醒"删了会中断它"。
export default function ReportLibrary({ onTasksDeleted, tasks = [] }) {
  const navigate = useNavigate()
  const [items, setItems] = useState([])
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState('')
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [datesEdited, setDatesEdited] = useState(false)
  const [selected, setSelected] = useState([])
  const [busy, setBusy] = useState('load')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [confirmDelete, setConfirmDelete] = useState(false)
  // 报告版本对比：点开就地展开，不另开页面。key 是报告 id，值是
  // {state: 'loading'|'ready'|'error', data|message}；同一行再点一次收起。
  const [diffs, setDiffs] = useState({})

  const refresh = useCallback(async (manual = false) => {
    setBusy('load'); setError(''); setSelected([]); setConfirmDelete(false)
    try {
      const data = await taskApi.reportLibrary()
      const next = data.items || []
      setItems(next)
      if (manual) setNotice(`已于 ${new Date().toLocaleTimeString('zh-CN', { hour12: false })} 刷新报告库，当前共 ${next.length} 份报告。`)
      return true
    }
    catch (error) { setError(error.message); return false }
    finally { setBusy('') }
  }, [])
  useEffect(() => { refresh(false) }, [refresh])

  const dateBounds = useMemo(() => {
    const dates = items.map(item => item.date).filter(value => /^\d{4}-\d{2}-\d{2}$/.test(value || '')).sort()
    return { start: dates[0] || '', end: dates.at(-1) || '' }
  }, [items])
  useEffect(() => {
    if (!datesEdited) { setStart(dateBounds.start); setEnd(dateBounds.end) }
  }, [dateBounds, datesEdited])
  const categories = useMemo(() => [...new Set(items.flatMap(item => item.categories))].sort((a, b) => a.localeCompare(b, 'zh-CN')), [items])
  const invalidDates = Boolean(start && end && start > end)
  const filtered = useMemo(() => {
    if (invalidDates) return []
    const words = query.trim().toLocaleLowerCase().split(/\s+/).filter(Boolean)
    return items.filter(item => (!category || item.categories.includes(category))
      && (!datesEdited || ((!start || (item.date && item.date >= start)) && (!end || (item.date && item.date <= end))))
      && words.every(word => `${item.title} ${item.scenario} ${item.summary}`.toLocaleLowerCase().includes(word)))
  }, [items, query, category, start, end, invalidDates, datesEdited])
  // Changing filters must never leave hidden reports selected for deletion. The
  // date inputs are also refilled automatically on every reload, so they may only
  // clear the selection after the user really edited them; otherwise the
  // "deleted N tasks" notice would be wiped by the refresh that follows it.
  useEffect(() => { setSelected([]); setConfirmDelete(false); setNotice('') }, [query, category])
  useEffect(() => { if (datesEdited) { setSelected([]); setConfirmDelete(false); setNotice('') } }, [datesEdited, start, end])

  const allSelected = filtered.length > 0 && filtered.every(item => selected.includes(item.id))
  const toggle = id => { setConfirmDelete(false); setSelected(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id]) }
  const selectedItems = useMemo(() => filtered.filter(item => selected.includes(item.id)), [filtered, selected])
  const deletion = useMemo(() => reportTaskDeletion(selectedItems, items, tasks), [selectedItems, items, tasks])
  const tooMany = selected.length > REPORT_BATCH_LIMIT
  const batchDisabled = Boolean(busy) || !selected.length || tooMany

  async function exportSelected() {
    setBusy('export'); setError(''); setNotice('')
    try {
      const blob = await taskApi.exportReports(selected)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url; anchor.download = `选型报告-${new Date().toISOString().slice(0, 10)}.zip`
      document.body.appendChild(anchor); anchor.click(); anchor.remove()
      setTimeout(() => URL.revokeObjectURL(url), 30000)
      setNotice(`已准备 ${selected.length} 份报告的压缩包，包含可阅读文稿与完整数据。`)
    } catch (error) { setError(error.message) }
    finally { setBusy('') }
  }

  async function exportSingle(item, format) {
    setBusy(`export-${format}`); setError(''); setNotice('')
    try {
      const blob = await taskApi.exportReportFile(item.id, format)
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      const safeTitle = String(item.title || '采购比选报告').replace(/[\\/:*?"<>|]/g, '_')
      anchor.href = url; anchor.download = `${safeTitle}-版本${item.version}.${format}`
      document.body.appendChild(anchor); anchor.click(); anchor.remove()
      setTimeout(() => URL.revokeObjectURL(url), 30000)
      setNotice(`已导出 ${item.title}（${format === 'docx' ? 'Word 文档' : 'Excel 工作簿'}）。`)
    } catch (error) { setError(error.message) }
    finally { setBusy('') }
  }

  async function removeSelected() {
    setBusy('delete'); setError(''); setNotice('')
    const { taskIds } = deletion
    if (!taskIds.length) { setError('选中的报告没有记录所属任务，无法按任务删除。'); setBusy(''); return }
    try {
      const result = await taskApi.deleteTasks(taskIds)
      setSelected([]); setConfirmDelete(false)
      // 报告列表和任务列表都要刷新：任务没了，任务列表里也不能再留着它。
      await onTasksDeleted?.(taskIds)
      window.dispatchEvent(new Event('labpilot-tasks-change'))
      const reloaded = await refresh()
      if (reloaded) setNotice(`已删除 ${result.deleted_tasks} 个任务，名下全部报告、原始资料和价格记录一并删除，无法恢复。`)
    } catch (error) { setError(error.message) }
    finally { setBusy('') }
  }

  // 同一行再点一次收起；请求失败只报这一行，不动整页（对比是只读操作，
  // 失败不该把报告列表也变成错误态）。
  async function toggleDiff(item) {
    if (diffs[item.id]) { setDiffs(all => Object.fromEntries(Object.entries(all).filter(([key]) => key !== item.id))); return }
    setDiffs(all => ({ ...all, [item.id]: { state: 'loading' } }))
    try { const data = await taskApi.reportDiff(item.id); setDiffs(all => ({ ...all, [item.id]: { state: 'ready', data } })) }
    catch (error) { setDiffs(all => ({ ...all, [item.id]: { state: 'error', message: error.message } })) }
  }

  // 一条差异画哪几行由 ``services/reportDiff.js`` 定（那一层有测试盯着）：
  // 新增的条目不印"上一版：（空）"，被删掉的条目要写明"本版不再出现"。
  function diffRows(changes) {
    return <ul className="report-diff-list">{changes.map((change, index) => <li key={`${change.section}-${change.label}-${index}`}>
      <p className="report-diff-label">{change.label || '（无标题条目）'}{change.section_title && change.section_title !== change.label ? <span> · {change.section_title}</span> : null}</p>
      {changeLines(change).map(line => <p className={`report-diff-${line.tone}`} key={line.tone}>{line.text}</p>)}
    </li>)}</ul>
  }

  // 对比面板就开在报告行下面：报告库这一页已经能表达"哪一版、差在哪"，不为它新开页面。
  function diffPanel(item) {
    const entry = diffs[item.id]
    if (!entry) return null
    if (entry.state === 'loading') return <div className="report-library-diff" aria-live="polite"><LoadingLabel>正在对比上一版</LoadingLabel></div>
    if (entry.state === 'error') return <div className="report-library-diff"><InlineNotice tone="error" title="对比未完成">{entry.message}</InlineNotice></div>
    const data = entry.data || {}
    if (!data.available) return <div className="report-library-diff"><InlineNotice tone="warning" title="没有可对比的上一版">{data.message}</InlineNotice></div>
    const previous = data.previous_created_at ? new Date(data.previous_created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '生成时间未记录'
    return <div className="report-library-diff">
      <p className="report-diff-note">与上一版（{previous}）逐条对照。下面只列变了的条目，没列出来的两版逐字相同；价格、配置、缺口分开说，同一处变化只算一类。</p>
      {diffGroups(data).map(group => <div className="report-diff-group" key={group.key}>
        <h4>{group.title}<span>{group.empty ? '与上一版相同' : `${group.items.length} 处变化`}</span></h4>
        {group.empty ? <p className="report-diff-same">这一类没有变化。</p> : diffRows(group.items)}
      </div>)}
      {data.other?.length > 0 && <div className="report-diff-group">
        <h4>其它变化<span>{data.other.length} 处</span></h4>
        {(data.other || []).some(change => change.section === 'supplier_revision') && <p className="report-diff-same">「销售回复更新」节是本次回复的原始记账（补了什么、依据在哪一句），与上面几类讲的是同一批事实的出处。</p>}
        {diffRows(data.other)}
      </div>}
      {data.notes?.length > 0 && <div className="report-diff-group">
        <h4>总述措辞<span>仅文字</span></h4>
        {diffRows(data.notes)}
        <p className="report-diff-same">总述由模型生成，措辞每次可能不同，不代表结论变了。</p>
      </div>}
      <p className="report-diff-unchanged">{unchangedSummary(data)}</p>
    </div>
  }

  return <section className="report-library" aria-label="历史报告">
    <div className="report-library-bar"><ActionButton variant="ghost" icon={RefreshCw} onClick={() => refresh(true)} disabled={Boolean(busy)}>刷新报告</ActionButton></div>
    {error && <InlineNotice tone="error" title="操作未完成">{error}</InlineNotice>}
    {notice && <InlineNotice title="处理结果">{notice}</InlineNotice>}
    <fieldset className="report-filters" disabled={Boolean(busy)}>
      <label>搜索报告<div className="report-search-field"><Search size={17} aria-hidden="true" /><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="仪器名称、采购用途或报告内容" /></div></label>
      <label>仪器种类<select value={category} onChange={event => setCategory(event.target.value)}><option value="">全部种类</option>{categories.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
      <label>生成日期从<input type="date" value={start} min={dateBounds.start || undefined} max={end || dateBounds.end || undefined} disabled={busy === 'load' || !dateBounds.start} onChange={event => { setDatesEdited(true); setStart(event.target.value) }} onBlur={event => { const value = event.currentTarget.value || dateBounds.start; if (value !== start) { setDatesEdited(true); setStart(value) } }} /></label>
      <label>至<input type="date" value={end} min={start || dateBounds.start || undefined} max={dateBounds.end || undefined} disabled={busy === 'load' || !dateBounds.end} onChange={event => { setDatesEdited(true); setEnd(event.target.value) }} onBlur={event => { const value = event.currentTarget.value || dateBounds.end; if (value !== end) { setDatesEdited(true); setEnd(value) } }} /></label>
      <button className="text-action" type="button" onClick={() => { setQuery(''); setCategory(''); setDatesEdited(false); setStart(dateBounds.start); setEnd(dateBounds.end); setSelected([]); setConfirmDelete(false); setNotice('') }}>重置筛选</button>
    </fieldset>
    {invalidDates && <InlineNotice tone="warning" title="请检查日期">结束日期不能早于开始日期。</InlineNotice>}
    <div className="report-batch-toolbar">
      <label><input type="checkbox" aria-label="选择当前筛选的全部报告" disabled={Boolean(busy) || !filtered.length} checked={allSelected} onChange={() => { setSelected(allSelected ? [] : filtered.map(item => item.id)); setConfirmDelete(false) }} />全选筛选结果</label>
      <span aria-live="polite">共 {filtered.length} 份 · 已选 {selected.length} 份{tooMany ? `（单批上限 ${REPORT_BATCH_LIMIT} 份）` : ''}</span>
      <ActionButton variant="secondary" icon={Download} disabled={batchDisabled} onClick={exportSelected}>{busy === 'export' ? '正在打包' : '导出所选'}</ActionButton>
      <ActionButton variant="ghost" icon={Trash2} disabled={Boolean(busy) || !deletion.canDelete || tooMany} onClick={() => setConfirmDelete(true)}>删除所选</ActionButton>
    </div>
    {confirmDelete && <div className="report-delete-confirm" role="alert"><strong>{deletion.title}</strong><p>{deletion.detail}</p><div className="module-actions"><ActionButton variant="coral" icon={Trash2} disabled={Boolean(busy)} onClick={removeSelected}>{busy === 'delete' ? '正在删除' : `确认删除 ${deletion.taskCount} 个任务`}</ActionButton><ActionButton variant="ghost" disabled={Boolean(busy)} onClick={() => setConfirmDelete(false)}>取消</ActionButton></div></div>}
    {busy === 'load' ? <LoadingLabel>正在读取历史报告</LoadingLabel> : !filtered.length ? <EmptyState icon={FileText} title={items.length ? '没有符合条件的报告' : '还没有历史报告'}>{items.length ? '清除筛选即可看到全部报告。' : '在任务里生成报告后，会自动出现在这里。'}</EmptyState> : <div className="report-library-list">{filtered.map(item => <article className="report-library-row" key={item.id}>
      <input type="checkbox" aria-label={`选择${item.title}版本${item.version}`} checked={selected.includes(item.id)} disabled={Boolean(busy)} onChange={() => toggle(item.id)} />
      <div className="report-library-body">
        <h3>{item.title}<span>版本 {item.version}</span></h3>
        <p className="report-library-meta">{item.categories.join('、')} · {item.created_at ? new Date(item.created_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }) : '日期未记录'}{item.task_id ? '' : ' · 所属任务未记录'}</p>
        <p className="report-library-summary">{item.scenario}</p>
      </div>
      <div className="module-actions">
        <ActionButton variant="secondary" icon={FileText} disabled={Boolean(busy) || !item.task_id} title={item.task_id ? '打开这份报告' : '这份报告没有记录所属任务'} onClick={() => navigate(`/app/task/${item.task_id}?tab=${encodeURIComponent('报告')}&report=${encodeURIComponent(item.id)}`)}>查看报告</ActionButton>
        <ActionButton variant="ghost" icon={Download} disabled={Boolean(busy)} title="导出标准 Word 比选文档 (.docx)" onClick={() => exportSingle(item, 'docx')}>导出 Word</ActionButton>
        <ActionButton variant="ghost" icon={Download} disabled={Boolean(busy)} title="导出多工作表 Excel 对比表 (.xlsx)" onClick={() => exportSingle(item, 'xlsx')}>导出 Excel</ActionButton>
        <ActionButton variant="ghost" disabled={Boolean(busy) || !item.task_id} title={item.task_id ? '打开报告所属的任务首页' : '这份报告没有记录所属任务'} onClick={() => navigate(`/app/task/${item.task_id}`)}>打开任务</ActionButton>
        {/* 只有"由上一版改出来的"这一版才有对比入口：第一版没有上一版，按钮点开也只会说
            "没有可对比的上一版"，不如不给这个入口。 */}
        {item.parent_report_id && <ActionButton variant="ghost" icon={null} disabled={Boolean(busy)} title="逐条对照这一版与上一版的价格、配置与缺口" onClick={() => toggleDiff(item)}>{diffs[item.id] ? '收起对比' : '与上一版对比'}</ActionButton>}
      </div>
      {diffPanel(item)}
    </article>)}</div>}
  </section>
}
