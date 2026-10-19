import React from 'react'

import { publicText } from '../services/publicText'
import { reportSummaryView } from '../services/reportSummary'
import { LinkedText } from './SourceLink'

/**
 * 报告总述：结论先行的那一块。
 *
 * 报告正文默认折叠，所以总述必须印在折叠外面 —— 藏在折叠里的结论等于没写。
 * 后端可能整块不给（老报告没有这个字段）、可能不给 narrative（模型不可用时
 * 只少这一段）、某条 fact 的 value 也可能为空。有内容的块才算块，没有就不渲染，
 * 也不拿「未提供 / 原文未提取」占位：这件事由 services/reportSummary.js 判定，
 * 这里只按它给出的块排版。
 *
 * 总述里的每一段文字（headline、narrative、facts 的 label 和 value、blockers）
 * 都先过 publicText 再渲染 —— narrative 是模型生成的，facts 直接来自 run 数据，
 * 两边都可能带 unknown 这类内部词和内部 ID。翻译统一在 reportSummaryView 里做，
 * 这里不再各处传一次；这正是报告正文那一套的同一个入口。
 */
export default function ReportSummaryBlock({ summary, taskState }) {
  const view = reportSummaryView(summary, value => publicText(value, taskState))
  if (!view) return null
  const shown = key => view.blocks.includes(key)
  return <div className="report-summary" aria-label="报告总述">
    {shown('headline') && <p className="report-summary-headline"><LinkedText>{view.headline}</LinkedText></p>}
    {shown('narrative') && <p className="report-summary-narrative"><LinkedText>{view.narrative}</LinkedText></p>}
    {shown('facts') && <dl className="report-summary-facts">{view.facts.map((fact, index) => <div key={index}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}</dl>}
    {shown('blockers') && <div className="report-summary-blockers"><strong>影响采购判断的未完成事项</strong><ul>{view.blockers.map((blocker, index) => <li key={index}><LinkedText>{blocker}</LinkedText></li>)}</ul></div>}
  </div>
}
