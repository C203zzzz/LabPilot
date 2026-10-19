import React, { useEffect, useState } from 'react'
import { runtimeApi } from '../services/api'

// 「本小时还剩多少次模型调用」。
//
// 为什么要有它（盲测单实测）：额度是**整台部署按小时共享**的（2026-09-29 前的默认上限
// 写死 20 次，那天按"先保功能、后收紧成本"提到 200，见 ``backend/infrastructure/deepseek.py``），
// 一份需要逐页视觉分析的 PDF 约 30 次——按旧上限，扫一份扫描件就把一小时用光。
// 在那之前用户拿不到任何预兆——资料传到一半、研究跑到一半才被挡下，而且当时三处
// 提示口径还不一致。这个组件把"已用/上限"摆在开始研究、上传资料之前。
//
// 提醒句由后端给（``GET /api/v1/runtime/model-budget`` 的 ``notice``），前端只负责
// 显示：额度文案与"被拦住时那句话"必须一处维护，否则两边会慢慢走样。
//
// 只读接口，读失败就整块不显示：额度提示不该自己变成一个错误提示。
export default function ModelBudgetNotice({ refreshKey = 0 }) {
  const [budget, setBudget] = useState(null)
  useEffect(() => {
    let cancelled = false
    const load = () => runtimeApi.modelBudget()
      .then(data => { if (!cancelled) setBudget(data) })
      .catch(() => { if (!cancelled) setBudget(null) })
    load()
    const timer = setInterval(load, 60000)
    return () => { cancelled = true; clearInterval(timer) }
  }, [refreshKey])
  if (!budget || !budget.limit) return null
  const used = Number(budget.used) || 0
  const limit = Number(budget.limit) || 0
  const remaining = Math.max(0, limit - used)
  const minutes = Math.max(1, Math.ceil((Number(budget.reset_in_seconds) || 0) / 60))
  return <div className={`model-budget-notice${remaining <= 5 ? ' is-low' : ''}`} role="status">
    <span className="model-budget-meter">本小时模型调用：已用 {used}/{limit} 次，还剩 {remaining} 次</span>
    {remaining <= 0
      ? <span className="model-budget-hint">{budget.exhausted_message}{minutes ? `（约 ${minutes} 分钟后有空名额）` : ''}</span>
      : budget.notice
        ? <span className="model-budget-hint">{budget.notice}{minutes ? `（约 ${minutes} 分钟后开始恢复）` : ''}</span>
        : null}
  </div>
}
