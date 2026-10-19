import React from 'react'
import { AlertTriangle, ArrowRight, CircleDashed, LoaderCircle, RefreshCw } from 'lucide-react'

export function StatusTag({ children, tone = 'neutral', icon: Icon }) {
  return <span className={`status-tag status-${tone}`}>{Icon && <Icon size={13} aria-hidden="true" />}{children}</span>
}

export function PageHeading({ title, description, action, meta }) {
  return <header className="page-heading">
    <div>
      <h1>{title}</h1>
      {description && <p>{description}</p>}
    </div>
    <div className="page-heading-actions">{meta}{action}</div>
  </header>
}

export function ActionButton({ children, variant = 'primary', icon: Icon = ArrowRight, className = '', ...props }) {
  return <button className={`action-button action-${variant} ${className}`} {...props}>
    <span>{children}</span>{Icon && <Icon size={17} aria-hidden="true" />}
  </button>
}

export function InlineNotice({ tone = 'info', title, children, action }) {
  const Icon = tone === 'error' || tone === 'warning' ? AlertTriangle : CircleDashed
  return <div className={`inline-notice notice-${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
    <Icon size={18} aria-hidden="true" />
    <div><strong>{title}</strong><div className="notice-content">{children}</div></div>
    {action}
  </div>
}

export function EmptyState({ icon: Icon = CircleDashed, title, children, action }) {
  return <div className="empty-state">
    <Icon size={25} aria-hidden="true" />
    <div><strong>{title}</strong><div className="notice-content">{children}</div></div>
    {action}
  </div>
}

export function LoadingLabel({ children }) {
  return <span className="loading-label"><LoaderCircle className="spin" size={15} aria-hidden="true" />{children}</span>
}

export function RetryButton({ onClick }) {
  return <button className="text-action" onClick={onClick}><RefreshCw size={14} aria-hidden="true" />重试</button>
}

export function PilotBeacon({ state = 'idle', label }) {
  return <div className={`pilot-beacon beacon-${state}`} aria-label={label || state}>
    <span className="beacon-orbit"><i /><i /><i /></span>
    <span className="beacon-copy">{label || (state === 'running' ? '正在执行' : state === 'done' ? '已完成' : '等待任务')}</span>
  </div>
}
