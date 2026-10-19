import React, { useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import { AlertTriangle, BookOpen, Bot, CheckCheck, FlaskConical, History, Menu, Plus, Settings2, X } from 'lucide-react'
import { useTask } from '../features/TaskContext'
import { StatusTag } from '../components/ui'
import { fetchProviderStatus, jsonRequest } from '../services/api'
import { connectionSummary } from '../services/providerAvailability'

const navigation = [
  { to: '/app/new', label: '新建选型', icon: Plus },
  { to: '/app/history', label: '历史报告', icon: History },
  { to: '/app/review', label: '待复核', icon: CheckCheck },
  { to: '/app/api', label: '连接设置', icon: Settings2 },
  { to: '/app/help', label: '使用说明', icon: BookOpen },
]

export default function AppShell() {
  const { task } = useTask()
  const [recent, setRecent] = useState([])
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
  useEffect(() => {
    const refresh = () => fetchProviderStatus()
      .then(data => setProviderStatus({ ...data, error: '' }))
      .catch(error => setProviderStatus({ items: [], error: error?.message || '读取连接状态失败，请稍后重试。' }))
    refresh()
    window.addEventListener('labpilot-provider-change', refresh)
    return () => window.removeEventListener('labpilot-provider-change', refresh)
  }, [])
  const providers = connectionSummary(providerStatus)
  const navigate = useNavigate()
  return <div className="workbench-shell">
    <button className="mobile-menu" aria-label={open ? '关闭导航' : '打开导航'} aria-expanded={open} onClick={() => setOpen(value => !value)}>{open ? <X /> : <Menu />}</button>
    {open && <button className="mobile-scrim" aria-label="关闭导航" onClick={() => setOpen(false)} />}
    <aside className={`app-sidebar ${open ? 'sidebar-open' : ''}`}>
      <button className="sidebar-brand" onClick={() => navigate('/')} aria-label="返回 LabPilot 起始页">
        <span>Lab</span><strong>Pilot</strong><i aria-hidden="true" />
      </button>
      <button className="sidebar-create" onClick={() => { navigate('/app/new'); setOpen(false) }}><FlaskConical size={17} aria-hidden="true" />建立采购任务</button>
      <nav aria-label="工作台导航">
        {navigation.map(item => <NavLink key={item.to} to={item.to} onClick={() => setOpen(false)} className={({ isActive }) => isActive ? 'active' : ''}>
          <item.icon size={17} aria-hidden="true" /><span>{item.label}</span>
          {item.label === '待复核' && task && <b>1</b>}
        </NavLink>)}
      </nav>
      <div className="sidebar-history"><h2>最近的采购</h2>{recent.length ? recent.slice(0,12).map(item => <button key={item.id} className={item.id === task?.id ? 'selected' : ''} onClick={() => navigate(`/app/task/${item.id}`)}><strong>{item.model || item.title || item.scenario?.slice(0,22) || '采购任务'}</strong><small>{(item.updated_at || item.created_at) ? new Date(item.updated_at || item.created_at).toLocaleDateString('zh-CN') : '已保存'}</small></button>) : <p>开始一段对话后，任务会保存在这里。</p>}</div>
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
      </div>
    </aside>
    <main className="app-main"><Outlet /></main>
  </div>
}
