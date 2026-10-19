import assert from 'node:assert/strict'
import test from 'node:test'

import {
  alternativeParameterGroups,
  alternativePresentation,
  alternativeVerdictLabel,
  similarityPercent,
  verdictDetail,
} from '../src/services/alternativePresentation.js'

// 一个参数行就是后端 parameter_row 的返回值；这里只保留呈现逻辑用得到的字段。
const row = (name, status, extra = {}) => ({ required: { name, value: 100, unit: 'mm', direction: 'min' }, status, ...extra })
const candidate = (matrix, extra = {}) => ({ brand: '某厂', model: 'YX-100', url: 'https://example.com/yx-100', matrix, ...extra })
const keys = presentation => presentation.groups.map(group => group.key)
const namesOf = (presentation, key) => (presentation.groups.find(group => group.key === key)?.items || []).map(item => item.name)

test('证据不足单独成行，不再挤在一段文字里', () => {
  const survey = alternativePresentation(candidate([
    row('转速', 'satisfied'),
    row('容量', 'unknown'),
    row('温度', 'unknown', { raw_evidence: [{ url: 'https://example.com/a', excerpt: '温度 4℃' }] }),
  ], { comparison_disposition: 'needs_review', status: 'needs_review' }))
  assert.deepEqual(namesOf(survey, 'missing'), ['容量'])
  assert.deepEqual(namesOf(survey, 'pendingReview'), ['温度'])
  assert.deepEqual(keys(survey), ['pendingReview', 'missing'])
})

test('没有内容的类别整条不渲染，不出现空壳标签', () => {
  const clean = alternativePresentation(candidate([
    row('转速', 'satisfied'),
    row('容量', 'satisfied'),
  ], { comparison_disposition: 'recommended_comparison', status: 'eligible', similarity_score: 1 }))
  assert.deepEqual(keys(clean), ['satisfied'])
  assert.ok(clean.groups.every(group => group.items.length > 0))
  assert.ok(clean.groups.every(group => !/[：:]\s*$/.test(group.label)), '标签不能以冒号结尾等着后面空着')
  assert.ok(!keys(clean).includes('missing'))
  assert.ok(!keys(clean).includes('pendingReview'))
  assert.ok(!keys(clean).includes('violated'))
})

test('已核实不满足与临时可能不满足是两套文案', () => {
  const confirmed = alternativePresentation(candidate([row('转速', 'violated')], {
    status: 'ineligible', comparison_disposition: 'needs_review',
  }))
  const provisional = alternativePresentation(candidate([
    row('转速', 'violated', { evidence_status: 'provisional' }),
  ], { status: 'needs_review', comparison_disposition: 'needs_review' }))
  assert.deepEqual(keys(confirmed), ['violated'])
  assert.equal(confirmed.groups[0].label, '已核实不满足的指标')
  assert.equal(confirmed.groups[0].tone, 'danger')
  assert.deepEqual(keys(provisional), ['provisionalViolated'])
  assert.equal(provisional.groups[0].label, '临时正文证据显示可能不满足')
  assert.equal(provisional.groups[0].note, '来源身份确认后再作正式判断。')
  assert.notEqual(confirmed.groups[0].label, provisional.groups[0].label)
  assert.ok(!keys(provisional).includes('violated'), '临时证据不能被写成已核实不满足')
})

test('参数未核全的候选，结论不可能是「可替代」', () => {
  const unverified = alternativePresentation(candidate([
    row('转速', 'satisfied'),
    row('容量', 'unknown'),
  ], { status: 'needs_review', comparison_disposition: 'needs_review', similarity_score: 0.5 }))
  assert.equal(unverified.verdict.label, '参数尚未核全')
  assert.equal(unverified.verdict.substitutable, false)
  assert.ok(!unverified.verdict.label.includes('替代'))
  assert.equal(unverified.verdict.boundary, '当前不能判断可替代。')
  assert.equal(unverified.verdict.tone, 'warning')
  assert.equal(unverified.verdict.scoreTag, null)
  assert.ok(!keys(unverified).includes('satisfied'), '没到阈值时不列满足项，免得看起来像可行方案')
})

test('任何候选的结论都不会自称可替代', () => {
  const cases = [
    candidate([row('a', 'satisfied')], { status: 'eligible', comparison_disposition: 'recommended_comparison', similarity_score: 1 }),
    candidate([row('a', 'satisfied'), row('b', 'violated')], { status: 'needs_review', comparison_disposition: 'comparable_candidate', similarity_score: 0.5 }),
    candidate([row('a', 'violated')], { status: 'ineligible', comparison_disposition: 'needs_review' }),
    candidate([row('a', 'unknown')], { status: 'needs_review', comparison_disposition: 'needs_review' }),
    candidate([], {}),
  ]
  for (const item of cases) {
    const { verdict } = alternativePresentation(item)
    assert.equal(verdict.substitutable, false)
    assert.ok(!verdict.label.includes('替代'), `结论标签不该出现「替代」：${verdict.label}`)
    assert.ok(verdict.boundary, '每种结论都要带一句边界说明')
  }
})

test('相似度口径只在达到阈值的候选上出现', () => {
  const reached = alternativePresentation(candidate([row('a', 'satisfied'), row('b', 'violated')], {
    comparison_disposition: 'comparable_candidate', similarity_score: 0.5,
  }))
  assert.equal(reached.verdict.label, '可比较候选')
  assert.equal(reached.verdict.scoreTag, '已核验 50%')
  assert.match(reached.verdict.basis, /阈值为 70%/)
  assert.match(reached.verdict.boundary, /不等于完全替代/)
  const notReached = alternativePresentation(candidate([row('a', 'unknown')], {
    comparison_disposition: 'needs_review', status: 'needs_review', similarity_score: 0.2,
  }))
  assert.equal(notReached.verdict.basis, '')
  assert.equal(notReached.verdict.scoreTag, null)
})

test('后端那句结论不再把判定念第二遍', () => {
  const { verdict } = alternativePresentation(candidate([row('a', 'satisfied')], {
    comparison_disposition: 'recommended_comparison',
    status: 'eligible',
    similarity_score: 1,
    conclusion: '推荐比较：全部已列关键指标均有可核验证据；仍需单独核对价格、配置和售后',
  }))
  assert.equal(verdict.label, '推荐比较')
  assert.equal(verdict.detail, '全部已列关键指标均有可核验证据；仍需单独核对价格、配置和售后')
  assert.equal(verdictDetail({ conclusion: '同档；价格待核实' }, '推荐比较'), '同档；价格待核实')
  assert.equal(verdictDetail({}, '推荐比较'), '')
})

test('已经有负向事实时不再补一句正向结论', () => {
  const { verdict } = alternativePresentation(candidate([row('a', 'unknown')], {
    comparison_disposition: 'needs_review',
    status: 'needs_review',
    conclusion: '推荐比较：全部已列关键指标均有可核验证据',
  }))
  assert.equal(verdict.detail, '')
})

test('算不出相似度时不能显示成 0%', () => {
  assert.equal(similarityPercent({ similarity_score: null }), null)
  assert.equal(similarityPercent({}), null)
  assert.equal(similarityPercent({ similarity_score: '' }), null)
  assert.equal(similarityPercent({ similarity_score: 0 }), '0%')
  assert.equal(similarityPercent({ similarity_score: 0.853 }), '85%')
  const { verdict } = alternativePresentation(candidate([row('a', 'satisfied'), row('b', 'violated')], {
    comparison_disposition: 'comparable_candidate', similarity_score: null,
  }))
  assert.equal(verdict.scoreTag, '已核验 达到阈值')
  assert.ok(!verdict.scoreTag.includes('0%'))
})

test('临时参数的提示保持原措辞与条数', () => {
  const provisional = alternativePresentation(candidate([
    row('转速', 'satisfied', { evidence_status: 'provisional' }),
    row('容量', 'unknown'),
  ], {}))
  assert.equal(provisional.provisionalCount, 1)
  assert.equal(provisional.provisionalNote, '已从网页正文提取 1 项临时参数；来源身份尚未核实，不计入合格替代结论。')
  assert.deepEqual(keys(provisional), ['provisional', 'missing'])
  assert.ok(!keys(provisional).includes('satisfied'), '临时证据不算已核验满足')
  assert.equal(alternativePresentation(candidate([row('转速', 'satisfied')], {})).provisionalNote, null)
})

test('原文互相冲突的行归入待核对，不当作满足', () => {
  const conflicting = alternativePresentation(candidate([
    row('转速', 'unknown', { conflict: true, evidence_status: 'confirmed', raw_evidence: [{ url: 'https://example.com/a', excerpt: '两种写法' }] }),
  ], {}))
  assert.deepEqual(keys(conflicting), ['pendingReview'])
  assert.equal(conflicting.groups[0].items[0].note, '原文互相冲突')
  assert.ok(!keys(conflicting).includes('satisfied'))
})

test('六个类别互斥，同一个参数不会被说两遍', () => {
  const groups = alternativeParameterGroups(candidate([
    row('已核实满足', 'satisfied'),
    row('已核实不满足', 'violated'),
    row('缺证据', 'unknown'),
    row('有原文待核对', 'unknown', { raw_evidence: [{ url: 'https://example.com/a', excerpt: 'x' }] }),
    row('临时满足', 'satisfied', { evidence_status: 'provisional' }),
    row('原文冲突', 'unknown', { conflict: true }),
  ], {}))
  const names = groups.flatMap(group => group.items.map(item => item.name))
  assert.equal(new Set(names).size, names.length, '同一个参数只允许出现在一类里')
  assert.deepEqual([...names].sort(), ['临时满足', '原文冲突', '已核实不满足', '已核实满足', '有原文待核对', '缺证据'].sort())
  assert.deepEqual(groups.map(group => group.key), ['violated', 'satisfied', 'pendingReview', 'provisional', 'missing'])
})

test('旧数据没有 matrix 时回落到后端算好的三类清单', () => {
  const { groups } = alternativePresentation({
    model: 'YX-100',
    comparison_disposition: 'comparable_candidate',
    similarity_score: 0.8,
    satisfied_parameters: [{ name: '转速' }],
    difference_parameters: [{ name: '容量' }],
    missing_parameters: [{ name: '温度' }],
  })
  assert.deepEqual(groups.map(group => group.key), ['violated', 'satisfied', 'missing'])
  assert.deepEqual(namesOf({ groups }, 'missing'), ['温度'])
})

test('同品牌参考有自己的结论，同样不称可替代', () => {
  const reference = alternativePresentation(candidate([row('转速', 'satisfied')], {
    comparison_disposition: 'recommended_comparison', status: 'eligible', similarity_score: 1,
  }), { reference: true })
  assert.equal(reference.verdict.label, '同品牌参考')
  assert.equal(reference.verdict.tone, 'muted')
  assert.equal(reference.verdict.substitutable, false)
  assert.equal(reference.verdict.boundary, '')
})

test('缺字段时给出空结构而不是抛异常', () => {
  const empty = alternativePresentation()
  assert.deepEqual(empty.groups, [])
  assert.equal(empty.verdict.label, '参数尚未核全')
  assert.equal(empty.provisionalNote, null)
  assert.equal(empty.category, '同类仪器（按本轮检索范围，仍需核对用途）')
  assert.deepEqual(alternativeParameterGroups(null), [])
  assert.deepEqual(alternativeParameterGroups({ matrix: 'not-a-list' }), [])
  assert.equal(alternativeVerdictLabel(null), '参数尚未核全')
  assert.equal(verdictDetail(null, '推荐比较'), '')
})

test('品类字段缺失时标注检索范围，不把基准品类当结论', () => {
  const withBaseline = alternativePresentation(candidate([row('a', 'satisfied')], {}), { baseline: { category: '离心机' } })
  assert.equal(withBaseline.category, '离心机（按本轮检索范围，仍需核对用途）')
  const explicit = alternativePresentation(candidate([row('a', 'satisfied')], { category: '台式高速离心机' }), { baseline: { category: '离心机' } })
  assert.equal(explicit.category, '台式高速离心机')
})

test('已核实不满足的指标带上“差多少”（后端算好的那一句，前端不自己编）', () => {
  const shown = alternativePresentation(candidate([
    row('最高转速', 'violated', { shortfall_text: '差额：实读比要求低 17.5%（以要求值为基准）。' }),
  ], { status: 'ineligible', comparison_disposition: 'needs_review' }))
  const item = shown.groups.find(group => group.key === 'violated').items[0]
  assert.equal(item.name, '最高转速')
  assert.equal(item.note, '差额：实读比要求低 17.5%（以要求值为基准）。')
})

test('算不出差额时卡片上没有那句话，也不出现 0 或编出来的百分比', () => {
  const shown = alternativePresentation(candidate([
    row('最高转速', 'violated', { shortfall_text: '差额：无法比较（要求值是 0 或负数，没有可作分母的基准，无法比较）。' }),
    row('容量', 'violated'),
  ], { status: 'ineligible', comparison_disposition: 'needs_review' }))
  const items = shown.groups.find(group => group.key === 'violated').items
  assert.equal(items[0].note, '差额：无法比较（要求值是 0 或负数，没有可作分母的基准，无法比较）。')
  assert.equal(items[1].note, '', '后端没给这一句时留空，不许前端编一个数')
})

test('临时来源的“可能不满足”不显示差额（结论本身还没定）', () => {
  const shown = alternativePresentation(candidate([
    row('最高转速', 'violated', { evidence_status: 'provisional', shortfall_text: '差额：实读比要求低 17.5%（以要求值为基准）。' }),
  ], { status: 'needs_review', comparison_disposition: 'needs_review' }))
  const item = shown.groups.find(group => group.key === 'provisionalViolated').items[0]
  assert.equal(item.note, '')
})
