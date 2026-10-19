import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'

import {
  normalizeSummary,
  reportSummary,
  reportSummaryView,
  summaryBlockers,
  summaryBlocks,
  summaryFacts,
  summaryObject,
} from '../src/services/reportSummary.js'
import {candidateName, publicText} from '../src/services/publicText.js'

const full = {
  headline: '中科中佳 H1850R 有 2 条同型号历史成交，但都缺配置与税费口径，暂不建议据此压价。',
  facts: [
    { label: '目标型号', value: '中科中佳 H1850R' },
    { label: '已核实历史成交', value: '2 条' },
    { label: '成交单价区间', value: '32,980 ~ 41,600 元' },
  ],
  blockers: ['价格记录缺少配置与税费口径', '其他品牌候选尚未取得原厂参数'],
  narrative: '本轮核到两条同型号成交记录，分布在两个省份；两者都只写了设备名称与金额。',
}

test('a complete summary yields the four blocks in reading order', () => {
  const summary = reportSummary({ summary: full })
  assert.deepEqual(summaryBlocks(summary), ['headline', 'narrative', 'facts', 'blockers'])
  assert.equal(summary.headline, full.headline)
  assert.equal(summary.narrative, full.narrative)
  assert.deepEqual(summary.facts, full.facts)
  assert.deepEqual(summary.blockers, full.blockers)
})

test('an old report without any summary renders nothing and does not throw', () => {
  assert.equal(reportSummary({ report_id: 'report-1', sections: [{ key: 'prices', items: [] }] }), null)
  assert.equal(reportSummary({}), null)
  assert.equal(reportSummary(null), null)
  assert.equal(reportSummary(undefined), null)
  assert.deepEqual(summaryBlocks(reportSummary(undefined)), [])
})

test('a summary that carries nothing printable is not a block either', () => {
  assert.equal(normalizeSummary({ headline: '   ', facts: [], blockers: [], narrative: '' }), null)
  assert.equal(normalizeSummary({ facts: [{ label: '型号', value: '' }] }), null)
  assert.equal(normalizeSummary({ blockers: ['', '  '] }), null)
  // 结构完全不像总述的对象（例如别的 summary 字段）不会被当成总述印出来。
  assert.equal(reportSummary({ summary: '本报告仅部分完成' }), null)
  assert.equal(reportSummary({ summary: { title: '本次采购判断', text: 'ok' } }), null)
})

test('a missing narrative leaves no empty block behind', () => {
  const { narrative, ...withoutNarrative } = full
  assert.equal(narrative, full.narrative)
  const summary = reportSummary({ summary: withoutNarrative })
  assert.equal(summary.narrative, '')
  assert.deepEqual(summaryBlocks(summary), ['headline', 'facts', 'blockers'])
  assert.equal(reportSummary({ summary: { narrative: '   ' } }), null)
})

test('a fact whose value is empty is dropped rather than filled with a placeholder', () => {
  const summary = reportSummary({ summary: { ...full, facts: [
    { label: '目标型号', value: '中科中佳 H1850R' },
    { label: '成交单价区间', value: '' },
    { label: '成交日期', value: '   ' },
    { label: '采购数量', value: null },
    { label: '供应商', value: 'unknown' },
  ] } })
  assert.deepEqual(summary.facts, [{ label: '目标型号', value: '中科中佳 H1850R' }])
  assert.deepEqual(summaryBlocks(summary), ['headline', 'narrative', 'facts', 'blockers'])
  const printed = JSON.stringify(summary)
  for (const word of ['未提供', '原文未提取', '暂无', '待补充', 'null', 'undefined']) {
    assert.ok(!printed.includes(word), `总述里不该出现「${word}」`)
  }
})

test('facts survive a number value, and malformed entries are skipped', () => {
  assert.deepEqual(summaryFacts([
    { label: '已核实成交', value: 2 },
    { label: '成交单价', value: 0 },
    { label: '', value: '无标签' },
    { label: '无值' },
    'not-an-object',
    null,
    { label: '对象值', value: { lowest: 1 } },
  ]), [{ label: '已核实成交', value: '2' }, { label: '成交单价', value: '0' }])
  assert.deepEqual(summaryFacts(undefined), [])
  assert.deepEqual(summaryFacts('x'), [])
})

test('blockers are de-duplicated and empty ones dropped', () => {
  assert.deepEqual(summaryBlockers(['缺配置', '', '缺配置', '   ', null, '缺税费']), ['缺配置', '缺税费'])
  assert.deepEqual(summaryBlockers(undefined), [])
})

test('a headline-only summary still prints its headline', () => {
  const summary = reportSummary({ summary: { headline: '本轮没有取得可核实的成交单价。' } })
  assert.deepEqual(summaryBlocks(summary), ['headline'])
  assert.equal(summary.narrative, '')
  assert.deepEqual(summary.facts, [])
  assert.deepEqual(summary.blockers, [])
})

test('the summary is read from whichever layer the backend attached it to', () => {
  assert.equal(summaryObject({ summary: full }), full)
  assert.equal(summaryObject({ report: { summary: full } }), full)
  assert.equal(summaryObject({ report_summary: full }), full)
  assert.equal(summaryObject({ snapshot: { summary: full } }), full)
  assert.equal(summaryObject({ report: { sections: [] } }), null)
  assert.equal(summaryObject(undefined), null)
  // 记录层没有总述时，报告层里的那份仍然被认出来。
  assert.equal(reportSummary({ report: { summary: { headline: '结论' } } }).headline, '结论')
})

test('the gaps wording is accepted where blockers does not exist', () => {
  const summary = reportSummary({ summary: { headline: '结论', gaps: ['缺配置', '缺配置', ''] } })
  assert.deepEqual(summary.blockers, ['缺配置'])
})

test('facts keep the backend wording verbatim, only trimmed', () => {
  const summary = reportSummary({ summary: {
    headline: ' 已核实 2 条成交记录 ',
    facts: [{ label: ' 目标型号 ', value: ' 中科中佳 H1850R ' }],
    blockers: ['  价格记录缺少配置与税费口径  '],
  } })
  assert.equal(summary.headline, '已核实 2 条成交记录')
  assert.deepEqual(summary.facts, [{ label: '目标型号', value: '中科中佳 H1850R' }])
  assert.deepEqual(summary.blockers, ['价格记录缺少配置与税费口径'])
})

// --- 总述文字必须过公开翻译层（publicText）------------------------------------
//
// narrative 是模型生成的，facts 的 label/value 直接来自 run 数据，两边都可能带
// 内部英文枚举和内部 ID。报告正文一直靠 publicText 把它们翻成中文，总述走同一个
// 入口；下面这些测试就是防止有人绕过它直接渲染。

const taskState = {
  candidates: [{id: 'cand-1', brand: '中科中佳', model: 'H1850R'}],
  documents: [{id: 'doc-1', source_name: '采购需求.pdf'}],
}

const translate = value => publicText(value, taskState)

test('the translation layer still behaves as the report body expects', () => {
  assert.equal(publicText('unknown', taskState), '资料未提供')
  assert.equal(publicText('tender_body_unverified: 正文未取到', taskState), '采购公告正文待核实：正文未取到')
  assert.equal(publicText('provider_failed:tavily: 检索中断', taskState), '检索服务未完成：检索中断')
  assert.equal(publicText('cand-1 的成交记录', taskState), '中科中佳 H1850R 的成交记录')
  assert.equal(publicText('doc-1 第 3 页', taskState), '采购需求.pdf 第 3 页')
  assert.equal(publicText('M01/M02 的硬约束', taskState), '选型 的必须满足的条件')
  assert.equal(publicText('', taskState), '')
  assert.equal(publicText(null, taskState), '')
  assert.equal(candidateName(taskState.candidates, 'cand-1'), '中科中佳 H1850R')
  assert.equal(candidateName(taskState.candidates, 'gone'), '待核对设备')
})

test('every line of the summary is translated before it reaches the page', () => {
  const view = reportSummaryView(reportSummary({ summary: {
    headline: 'cand-1 的成交记录状态是 unknown，tender_body_unverified: 正文没取到。',
    narrative: '本轮 provider_failed:tavily: 只找到 unknown 条可用记录。',
    facts: [
      { label: '目标型号', value: 'cand-1' },
      { label: '证据出处', value: 'doc-1' },
      { label: '记录状态', value: 'unknown' },
    ],
    blockers: ['cand-1 的价格记录缺少配置口径'],
  } }), translate)
  const printed = JSON.stringify(view)
  for (const leak of ['unknown', 'tender_body_unverified', 'provider_failed', 'cand-1', 'doc-1']) {
    assert.ok(!printed.includes(leak), `总述里不该漏出「${leak}」`)
  }
  assert.ok(view.headline.includes('中科中佳 H1850R'))
  assert.ok(view.headline.includes('资料未提供'))
  assert.ok(view.headline.includes('采购公告正文待核实：'))
  assert.ok(view.narrative.includes('检索服务未完成：'))
  assert.deepEqual(view.facts, [
    { label: '目标型号', value: '中科中佳 H1850R' },
    { label: '证据出处', value: '采购需求.pdf' },
  ])
  // value 本身就是内部枚举 unknown 的那一条，在归一化阶段就丢掉了 ——
  // 它要是留着，要么漏出 unknown，要么翻成「资料未提供」，两种都是这次的毛病。
  assert.ok(!view.facts.some(fact => fact.label === '记录状态'))
  assert.deepEqual(view.blockers, ['中科中佳 H1850R 的价格记录缺少配置口径'])
})

test('a view without a translation function still renders its own text', () => {
  const view = reportSummaryView(reportSummary({ summary: { headline: '结论' } }))
  assert.deepEqual(view.blocks, ['headline'])
  assert.equal(view.headline, '结论')
  assert.deepEqual(view.facts, [])
  assert.equal(reportSummaryView(null, translate), null)
})

// --- 结构回归：总述不能藏进折叠里，翻译层也不能再被复制一份 --------------------

const workspace = readFileSync(new URL('../src/pages/TaskWorkspace.jsx', import.meta.url), 'utf8')
const block = readFileSync(new URL('../src/components/ReportSummaryBlock.jsx', import.meta.url), 'utf8')

test('the summary block is rendered outside the collapsible report body', () => {
  const start = workspace.indexOf('const expanded = selectedReportId === key')
  const bodyStart = workspace.indexOf('{expanded && <div className="report-detail"')
  assert.ok(start > 0, '找不到报告列表的渲染入口')
  assert.ok(bodyStart > start, '找不到折叠的报告正文')
  const collapsed = workspace.slice(start, bodyStart)
  assert.match(collapsed, /const summary = reportSummary\(payload\)/)
  assert.match(collapsed, /<ReportSummaryBlock summary=\{summary\} taskState=\{taskState\} \/>/)
  // 折叠开关不参与总述的渲染条件。
  assert.ok(!/expanded &&[\s\S]{0,80}<ReportSummaryBlock/.test(workspace))
})

test('the summary goes through the same translation entry as the report body', () => {
  assert.match(workspace, /import \{ candidateName, publicText \} from '\.\.\/services\/publicText'/)
  assert.match(block, /reportSummaryView\(summary, value => publicText\(value, taskState\)\)/)
  // 翻译层只留 service 里那一份；组件里不该再有副本。
  assert.ok(!/function publicText\(/.test(workspace), 'publicText 不该在组件里再定义一份')
  assert.ok(!/function candidateName\(/.test(workspace), 'candidateName 不该在组件里再定义一份')
  // 既有报告正文的调用点保持原样。
  for (const call of [
    'publicText(section.summary, taskState)',
    'publicText(item.label, taskState)',
    'publicText(item.text, taskState)',
    'publicText(section.title,taskState)',
  ]) {
    assert.ok(workspace.includes(call), `报告正文的调用点被改动了：${call}`)
  }
})
