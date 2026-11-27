import React, { useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { AlertTriangle, BarChart3, BookOpen, Bot, Check, CheckCheck, History, Menu, Pencil, Plus, Settings2, Trash2, X } from 'lucide-react'
import { useTask } from '../features/TaskContext'
import { StatusTag } from '../components/ui'
import { fetchProviderStatus, fetchSystemDiagnostics, jsonRequest } from '../services/api'
import { connectionSummary } from '../services/providerAvailability'

const navigation = [
  { to: '/app/history', label: '历史报告', icon: History },
  { to: '/app/compare', label: '历史比价库', icon: BarChart3 },
  { to: '/app/review', label: '待复核', icon: CheckCheck },
  { to: '/app/api', label: '连接设置', icon: Settings2 },
  { to: '/app/help', label: '使用说明', icon: BookOpen },
]

export default function AppShell() {
  const { task, reviewItems = [], renameTask, deleteTask } = useTask()
  const [recent, setRecent] = useState([])
  const [editingId, setEditingId] = useState('')
  const [editingTitle, setEditingTitle] = useState('')
  const [deletingId, setDeletingId] = useState('')
  // 侧栏的「最近的采购」也是一份任务列表：删除整项任务后必须重新拉，否则点进
  // 去会落到一个已经不存在的任务上。
  useEffect(() => {
    const load = () => jsonRequest('/tasks').then(data => setRecent(data.items || [])).catch(() => {})
    load()
    window.addEventListener('labpilot-tasks-change', load)
    return () => window.removeEventListener('labpilot-tasks-change', load)
  }, [task?.id])
  const [open, setOpen] = useState(false)
  // 读连接状态失败时留一句原因，别落回"未连接"：配置项写错（后端返回带 code 的
  // 502）与真的连不上是两件事，判断与视觉按钮、连接设置页共用
  // `connectionSummary` / `visionState`（见 services/providerAvailability.js）。
  const [providerStatus, setProviderStatus] = useState({ items: [], error: '' })
  const [diagStatus, setDiagStatus] = useState(null)
  useEffect(() => {
    const refresh = () => {
      fetchProviderStatus()
        .then(data => setProviderStatus({ ...data, error: '' }))
        .catch(error => setProviderStatus({ items: [], error: error?.message || '读取连接状态失败，请稍后重试。' }))
      fetchSystemDiagnostics({ checkDns: true })
        .then(data => setDiagStatus(data))
        .catch(() => {})
    }
    refresh()
    window.addEventListener('labpilot-provider-change', refresh)
    return () => window.removeEventListener('labpilot-provider-change', refresh)
  }, [])
  const providers = connectionSummary(providerStatus)
  const navigate = useNavigate()
  return <div className="workbench-shell">
    <button className={`mobile-menu ${open ? 'is-open' : ''}`} aria-label={open ? '关闭导航' : '打开导航'} aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? <X /> : <Menu />}</button>
    {open && <button className="mobile-scrim" aria-label="关闭导航" onClick={() => setOpen(false)} />}
    <aside className={`app-sidebar ${open ? 'sidebar-open' : ''}`}>
      <button className="sidebar-brand" onClick={() => { navigate('/'); setOpen(false) }} aria-label="返回 LabPilot 起始页">
        <span>Lab</span><strong>Pilot</strong><i aria-hidden="true" />
      </button>
      <button className="sidebar-create" onClick={() => { navigate('/app/new'); setOpen(false) }}><Plus size={17} aria-hidden="true" />新建选型</button>
      <nav aria-label="工作台导航">
        {navigation.map(item => <NavLink key={item.to} to={item.to} onClick={() => setOpen(false)} className={({ isActive }) => isActive ? 'active' : ''}>
          <item.icon size={17} aria-hidden="true" /><span>{item.label}</span>
          {item.label === '待复核' && reviewItems.length > 0 && <b>{reviewItems.length}</b>}
        </NavLink>)}
      </nav>
      <div className="sidebar-history"><h2>最近的采购</h2>{recent.length ? recent.slice(0,12).map(item => {
        const displayName = item.custom_title || item.model || item.title || item.scenario?.slice(0,22) || '采购任务'
        const dateLabel = (item.updated_at || item.created_at) ? new Date(item.updated_at || item.created_at).toLocaleDateString('zh-CN') : '已保存'
        if (editingId === item.id) {
          return <form key={item.id} className="sidebar-history-edit" onSubmit={async e => {
            e.preventDefault()
            const updated = await renameTask?.(item.id, editingTitle)
            if (updated) {
              setRecent(cur => cur.map(row => row.id === item.id ? { ...row, ...updated } : row))
              setEditingId(''); setEditingTitle('')
            }
          }}>
            <input value={editingTitle} onChange={e => setEditingTitle(e.target.value)} aria-label="新任务名称" autoFocus maxLength={80} />
            <button type="submit" aria-label="保存名称" title="保存"><Check size={13} /></button>
            <button type="button" aria-label="取消重命名" title="取消" onClick={() => { setEditingId(''); setEditingTitle('') }}><X size={13} /></button>
          </form>
        }
        return <div key={item.id} className={`sidebar-history-item ${item.id === task?.id ? 'selected' : ''}`}>
          <button type="button" className="sidebar-history-main" onClick={() => { navigate(`/app/task/${item.id}`); setOpen(false) }}>
            <strong>{displayName}</strong>
            <small>{dateLabel}</small>
          </button>
          <div className="sidebar-history-actions">
            <button type="button" aria-label={`重命名 ${displayName}`} title="重命名任务" onClick={e => { e.stopPropagation(); setDeletingId(''); setEditingId(item.id); setEditingTitle(item.title || displayName) }}><Pencil size={12} /></button>
            <button type="button" aria-label={`删除 ${displayName}`} title="删除任务" onClick={e => { e.stopPropagation(); setEditingId(''); setDeletingId(cur => cur === item.id ? '' : item.id) }}><Trash2 size={12} /></button>
          </div>
          {deletingId === item.id && <div className="sidebar-history-confirm" role="alert">
            <span>确认删除此任务及名下所有资料与报告？</span>
            <div>
              <button type="button" className="danger" onClick={async () => {
                const isCurrent = item.id === task?.id
                if (await deleteTask?.(item.id)) {
                  setRecent(cur => cur.filter(row => row.id !== item.id))
                  setDeletingId('')
                  if (isCurrent) { navigate('/app/new'); setOpen(false) }
                }
              }}>确认删除</button>
              <button type="button" onClick={() => setDeletingId('')}>取消</button>
            </div>
          </div>}
        </div>
      }) : <p>开始一段对话后，任务会保存在这里。</p>}</div>
      <div className="sidebar-status">
        <div><span>任务状态</span><StatusTag tone={task ? 'success' : 'muted'}>{task ? '任务已连接' : '等待任务'}</StatusTag></div>
        {providers.problem
          // 配置写错时不再两行各说一句"未连接"：状态读不到，就不该假装知道是哪一项
          // 没连上。一行说"配置有误"，下一行给后端原话（顶栏窄，超长省略、悬停看全文）。
          ? <>
            <div><span>连接状态</span><StatusTag tone="danger"><AlertTriangle size={12} aria-hidden="true" />{providers.problem}</StatusTag></div>
            <p className="sidebar-status-note" title={providers.error}>{providers.error}</p>
          </>
          : <>
            <div><span>思考模型</span><StatusTag tone={providers.model ? 'success' : 'prototype'}><Bot size={12} />{providers.model ? '已连接' : '未连接'}</StatusTag></div>
            <div><span>联网能力</span><StatusTag tone={providers.search ? 'success' : 'prototype'}>{providers.search ? '已连接' : '待连接'}</StatusTag></div>
          </>}
        {diagStatus && diagStatus.overall_status !== 'healthy' && (
          <button type="button" className="sidebar-diag-alert" onClick={() => { navigate('/app/api'); setOpen(false) }} title={diagStatus.summary}>
            <AlertTriangle size={12} aria-hidden="true" />
            <span>{diagStatus.overall_status === 'broken' ? `环境自检发现 ${diagStatus.error_count} 项阻断` : `环境自检 ${diagStatus.warning_count} 项降级提醒`}</span>
          </button>
        )}
      </div>
    </aside>
    <main className="app-main"><Outlet /></main>
  </div>
}
