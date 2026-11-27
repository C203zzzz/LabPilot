/**
 * 候选卡片的呈现口径。
 *
 * 这一份测试钉的是"卡片上还剩什么、什么被拿掉了"：
 *
 *   还剩：这条线索怎么来的（中性标签）、品类、证据质量问题（原文冲突／来源未核实）、
 *         逐项参数原文、读到的配置（另一份测试断言）、来源与价格。
 *   拿掉：一致／不同／未知三态、已核验比例、数值接近程度、「已核对的指标满足」这类
 *         结论、以及任何"可替代"的说法。
 *
 * 依据是回归基线里两条业务口径：「候选只展示能抓到的关键配置，不替用户判断是否满足
 * 需求」，以及 规范项 的「结果里只摆配置原文，不再输出满足 / 差异 / 未知」。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import * as presentation from '../src/services/alternativePresentation.js'
import {
  alternativePresentation,
  alternativeVerdict,
  alternativeVerdictLabel,
  evidenceNotes,
  isComparableDisposition,
  provisionalParameterNote,
} from '../src/services/alternativePresentation.js'

// 一个参数行就是后端 parameter_row 的返回值；这里只保留呈现逻辑用得到的字段。
const row = (name, status, extra = {}) => ({ required: { name, value: 100, unit: 'mm', direction: 'min' }, status, ...extra })
const candidate = (matrix, extra = {}) => ({ brand: '某厂', model: 'YX-100', url: 'https://example.com/yx-100', matrix, ...extra })
const noteKeys = notes => notes.map(note => note.key)
const namesOf = (notes, key) => (notes.find(note => note.key === key)?.items || []).map(item => item.name)

test('结论标签只回答"这条线索是怎么来的"，不再判满足与否', () => {
  const verified = alternativePresentation(candidate([row('转速', 'satisfied')], {
    comparison_disposition: 'comparable_candidate', status: 'eligible', similarity_score: 0.8,
  }))
  assert.equal(verified.verdict.label, '本轮核验的候选')
  assert.equal(verified.verdict.tone, 'neutral')
  const lead = alternativePresentation(candidate([row('转速', 'unknown')], { status: 'needs_review' }))
  assert.equal(lead.verdict.label, '本轮线索')
  const reference = alternativePresentation(candidate([row('转速', 'satisfied')], { status: 'eligible' }), { reference: true })
  assert.equal(reference.verdict.label, '同品牌参考')
  // 旧标签（"已核对的指标满足"/"关键指标不满足"/"参数尚未核全"）一个都不许回来。
  for (const item of [verified, lead, reference]) {
    for (const word of ['满足', '不满足', '核全', '可比较候选', '推荐比较']) {
      assert.ok(!item.verdict.label.includes(word), `结论标签里还在判「${word}」：${item.verdict.label}`)
    }
  }
})

test('任何候选的结论都不会自称可替代', () => {
  const cases = [
    candidate([row('a', 'satisfied')], { status: 'eligible', comparison_disposition: 'recommended_comparison', similarity_score: 1 }),
    candidate([row('a', 'satisfied'), row('b', 'violated')], { status: 'needs_review', comparison_disposition: 'comparable_candidate', similarity_score: 0.5 }),
    candidate([row('a', 'violated')], { status: 'ineligible' }),
    candidate([row('a', 'unknown')], { status: 'needs_review' }),
    candidate([], {}),
  ]
  for (const item of cases) {
    const { verdict } = alternativePresentation(item)
    assert.equal(verdict.substitutable, false)
    assert.ok(!verdict.label.includes('替代'), `结论标签不该出现「替代」：${verdict.label}`)
    assert.ok(verdict.boundary, '每种结论都要带一句边界说明')
  }
})

test('证据质量只剩两类：原文互相冲突、来源尚未核实', () => {
  const notes = evidenceNotes(candidate([
    row('转速', 'satisfied'),
    row('容量', 'violated'),
    row('温度', 'unknown', { raw_evidence: [{ url: 'https://example.com/a', excerpt: '温度 4℃' }] }),
    row('压力', 'unknown', { conflict: true, raw_evidence: [{ url: 'https://example.com/a', excerpt: '两种写法' }] }),
    row('湿度', 'satisfied', { evidence_status: 'provisional' }),
  ]))
  assert.deepEqual(noteKeys(notes), ['conflict', 'provisional'])
  assert.deepEqual(namesOf(notes, 'conflict'), ['压力'])
  assert.deepEqual(namesOf(notes, 'provisional'), ['湿度'])
  assert.equal(notes[0].items[0].note, '原文互相冲突')
  // 「已核实满足／已核实不满足／缺少证据」这三类（都是需求判断或待核实铺陈）不再成组。
  for (const key of ['satisfied', 'violated', 'missing', 'pendingReview', 'provisionalViolated']) {
    assert.ok(!noteKeys(notes).includes(key), `${key} 这一类还在成组展示`)
  }
  assert.ok(notes.every(note => !/满足|不满足/.test(note.label)))
})

test('临时参数的提示保留条数，但不再提"替代结论"', () => {
  const provisional = alternativePresentation(candidate([
    row('转速', 'satisfied', { evidence_status: 'provisional' }),
    row('容量', 'unknown'),
  ], {}))
  assert.equal(provisional.provisionalCount, 1)
  assert.match(provisional.provisionalNote, /已从网页正文提取 1 项临时参数/)
  assert.ok(!provisional.provisionalNote.includes('替代'))
  assert.equal(provisionalParameterNote(0), null)
})

test('旧数据没有 matrix 时只给空结构，不回落到"满足／缺证"三类清单', () => {
  const { evidenceNotes: notes } = alternativePresentation({
    model: 'YX-100',
    comparison_disposition: 'comparable_candidate',
    similarity_score: 0.8,
    satisfied_parameters: [{ name: '转速' }],
    difference_parameters: [{ name: '容量' }],
    missing_parameters: [{ name: '温度' }],
  })
  assert.deepEqual(notes, [])
})

test('品类字段缺失时标注检索范围，不把基准品类当结论', () => {
  const withBaseline = alternativePresentation(candidate([row('a', 'satisfied')], {}), { baseline: { category: '离心机' } })
  assert.equal(withBaseline.category, '离心机（按本轮检索范围，仍需核对用途）')
  const explicit = alternativePresentation(candidate([row('a', 'satisfied')], { category: '台式高速离心机' }), { baseline: { category: '离心机' } })
  assert.equal(explicit.category, '台式高速离心机')
})

test('缺字段时给出空结构而不是抛异常', () => {
  const empty = alternativePresentation()
  assert.deepEqual(empty.evidenceNotes, [])
  assert.equal(empty.verdict.label, '本轮线索')
  assert.equal(empty.provisionalNote, null)
  assert.equal(empty.category, '同类仪器（按本轮检索范围，仍需核对用途）')
  assert.deepEqual(evidenceNotes(null), [])
  assert.deepEqual(evidenceNotes({ matrix: 'not-a-list' }), [])
  assert.equal(alternativeVerdictLabel(null), '本轮线索')
  assert.equal(alternativeVerdict(null).substitutable, false)
  assert.equal(isComparableDisposition(null), false)
})

test('判定与分数的出口已经下线，不许再被引回来', () => {
  for (const name of [
    'similarityPercent', 'verdictDetail', 'alternativeParameterGroups', 'PARAMETER_GROUP_DEFS',
    'SIMILARITY_BASIS', 'COMPARABLE_BOUNDARY', 'INELIGIBLE_BOUNDARY', 'UNRESOLVED_BOUNDARY',
    'EMPTY_CONCLUSION', 'configMatchPresentation', 'configMatchRows', 'configMatchConfidence',
    'configMatchSoftScore', 'CONFIG_MATCH_STATE_DEFS', 'CONFIG_MATCH_UNKNOWN_NOTE',
  ]) {
    assert.ok(!(name in presentation), `${name} 这个判定出口还在模块里`)
  }
  assert.ok('configListing' in presentation && 'evidenceNotes' in presentation)
})
