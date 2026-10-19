import React, { useCallback, useEffect, useMemo, useState } from 'react'
import { Download, FileText, FolderOpen, RefreshCw, Search, Trash2 } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { ActionButton, EmptyState, InlineNotice, LoadingLabel } from '../components/ui'
import { taskApi } from '../services/api'
import { REPORT_BATCH_LIMIT, reportTaskDeletion } from '../services/reportBatch'

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

  const refresh = useCallback(async () => {
    setBusy('load'); setError(''); setSelected([]); setConfirmDelete(false)
    try { const data = await taskApi.reportLibrary(); setItems(data.items || []); return true }
    catch (error) { setError(error.message); return false }
    finally { setBusy('') }
  }, [])
  useEffect(() => { refresh() }, [refresh])

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

  return <section className="report-library" aria-label="历史报告">
    <div className="report-library-bar"><ActionButton variant="ghost" icon={RefreshCw} onClick={refresh} disabled={Boolean(busy)}>刷新报告</ActionButton></div>
    {error && <InlineNotice tone="error" title="操作未完成">{error}</InlineNotice>}
    {notice && <InlineNotice title="处理结果">{notice}</InlineNotice>}
    <fieldset className="report-filters" disabled={Boolean(busy)}>
      <label>搜索报告<div className="report-search-field"><Search size={17} aria-hidden="true" /><input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="仪器名称、采购用途或报告内容" /></div></label>
      <label>仪器种类<select value={category} onChange={event => setCategory(event.target.value)}><option value="">全部种类</option>{categories.map(value => <option key={value} value={value}>{value}</option>)}</select></label>
      <label>生成日期从<input type="date" value={start} min={dateBounds.start || undefined} max={end || dateBounds.end || undefined} disabled={busy === 'load' || !dateBounds.start} onChange={event => { setDatesEdited(true); setStart(event.target.value) }} onBlur={event => { const value = event.currentTarget.value || dateBounds.start; if (value !== start) { setDatesEdited(true); setStart(value) } }} /></label>
      <label>至<input type="date" value={end} min={start || dateBounds.start || undefined} max={dateBounds.end || undefined} disabled={busy === 'load' || !dateBounds.end} onChange={event => { setDatesEdited(true); setEnd(event.target.value) }} onBlur={event => { const value = event.currentTarget.value || dateBounds.end; if (value !== end) { setDatesEdited(true); setEnd(value) } }} /></label>
      <button className="text-action" type="button" onClick={() => { setQuery(''); setCategory(''); setDatesEdited(false); setStart(dateBounds.start); setEnd(dateBounds.end); setSelected([]); setConfirmDelete(false); setNotice('') }}>重置筛选</button>
    </fieldset>
    <p className="report-filter-note">日期按北京时间计算，含起止当日；日期未记录的报告不计入日期筛选。</p>
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
        <ActionButton variant="ghost" disabled={Boolean(busy) || !item.task_id} title={item.task_id ? '打开报告所属的任务首页' : '这份报告没有记录所属任务'} onClick={() => navigate(`/app/task/${item.task_id}`)}>打开任务</ActionButton>
      </div>
    </article>)}</div>}
  </section>
}
