import React, { useEffect, useState } from 'react'
import { ActionButton, InlineNotice } from '../components/ui'

const base = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000/api/v1'
export default function AnalysisProgress({ taskId, busy, documents }) {
  const [progress, setProgress] = useState(null)
  const [error, setError] = useState('')
  const [cancelling, setCancelling] = useState(false)
  useEffect(() => {
    const controller = new AbortController(); let active = true; let timer
    setProgress(null); setError(''); setCancelling(false)
    async function read() {
      try {
        const response = await fetch(`${base}/tasks/${taskId}/analysis/progress`, { signal: controller.signal })
        if (!response.ok) throw new Error('资料分析进度暂时无法读取')
        const value = await response.json()
        if (active) { setProgress(value); setError('') }
      } catch (e) { if (active && e.name !== 'AbortError') setError(e.message) }
      if (active && busy === 'analysis') timer = setTimeout(read, 2000)
    }
    read()
    return () => { active = false; controller.abort(); clearTimeout(timer) }
  }, [taskId, busy])
  return <div aria-live="polite">
    {error && <InlineNotice tone="error" title="进度读取失败">{error}</InlineNotice>}
    {busy === 'analysis' && <p>正在提取型号和参数，已处理的内容会保存。你可以停止分析，稍后继续。</p>}
    {busy === 'analysis' && <ActionButton variant="ghost" disabled={cancelling || Boolean(progress?.control?.cancelled)} onClick={async () => {
      setCancelling(true); setError('')
      try {
        const response = await fetch(`${base}/tasks/${taskId}/analysis/cancel`, { method: 'POST' })
        if (!response.ok) throw new Error('停止请求未完成，请重试')
        setProgress(await response.json())
      } catch (e) { setError(e.message) } finally { setCancelling(false) }
    }}>停止后续分析</ActionButton>}
    {progress?.control?.cancelled && <p role="status">已请求停止。当前步骤结束后会保存结果，下次可继续处理。</p>}
  </div>
}
