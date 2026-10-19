/**
 * 「查这个的价格」——两段式里的"用户点哪个才查哪个"。
 *
 * 钉住四件事（``docs/HANDOVER-alt-two-stage.md`` 第二、三节）：
 *
 * 1. **一次点击只发一个请求，且请求里只有一个 candidate_id**（路径里的那个；
 *    请求体是空对象，连候选标识都没有）——"每次只查一个"不靠自觉；
 * 2. **点第二次命中已有结果**：请求体不带 ``force``，后端默认返回缓存；
 *    界面上也要说清"这次没有新增检索"，而不是把缓存说成新查了一次；
 * 3. **只刷新被点的那一张卡片**：``applyPriceLookup`` 只改那一个键，别的候选
 *    按引用原样保留——别的卡片的按钮因此照常可用（``priceButtonState`` 只看
 *    这一个候选的进行中状态）；
 * 4. **"没查"与"查了没有"必须能分开看**，而且一个内部字段名、一个网址都不许
 *    出现在页面上（状态码一律翻译成人话）。
 */
import assert from 'node:assert/strict'
import test from 'node:test'

import {
  applyPriceLookup,
  beginLookup,
  candidatePriceBody,
  candidatePricePath,
  candidatePriceView,
  endLookup,
  lookupCandidatePrice,
  priceButtonState,
  priceLookupResult,
  priceLookupView,
} from '../src/services/alternativePriceLookup.js'

const ID_A = '9a8b7c6d5e4f3021'
const ID_B = '1122334455667788'

const priceRecord = { model: 'H1650K', kind: 'award', amount: 52000, currency: 'CNY', date: '2026-03-25', url: 'https://example.gov.cn/award/1' }

const response = (candidateId, lookup) => ({
  run_id: 'run-1',
  candidate_id: candidateId,
  price_lookup: {
    status: 'completed', cached: false, reason_code: null,
    detail: { status: 'priced', reason_code: null, price_count: 1 },
    prices: [priceRecord], sources: ['https://example.gov.cn/award/1'],
    spent: { search: 3, fetch: 6, model: 1 },
    ...lookup,
  },
  run: { id: 'run-1', alternatives: [] },
})

test('路径里只有这一个 candidate_id，请求体里一个标识都没有', () => {
  assert.equal(candidatePricePath('run-1', ID_A), `/research-runs/run-1/alternatives/${ID_A}/price`)
  assert.deepEqual(candidatePriceBody(), {})
  assert.deepEqual(Object.keys(candidatePriceBody()), [], '请求体必须为空对象：要查哪个由路径决定')
  // 万一将来有人往路径里塞第二个候选，这条会红。
  const path = candidatePricePath('run-1', ID_A)
  assert.equal(path.split('/').filter(part => [ID_A, ID_B].includes(part)).length, 1)
  assert.ok(!path.includes(ID_B))
})

test('点一次只发一个请求，而且路径/请求体里只带一个 candidate_id', async () => {
  const calls = []
  const result = await lookupCandidatePrice({
    runId: 'run-1', candidateId: ID_A,
    post: (path, body) => { calls.push({ path, body }); return Promise.resolve(response(ID_A)) },
  })
  assert.equal(calls.length, 1)
  assert.equal(calls[0].path, `/research-runs/run-1/alternatives/${ID_A}/price`)
  assert.deepEqual(calls[0].body, {})
  assert.ok(!JSON.stringify(calls[0].body).includes(ID_A))
  assert.ok(!JSON.stringify(calls[0].body).includes(ID_B))
  assert.equal(result.candidateId, ID_A)
  assert.equal(result.prices.length, 1)
  assert.equal(result.cached, false)
})

test('两个候选各点一次就是两个请求，任何一次请求里都不会同时出现两个候选', async () => {
  const calls = []
  const post = (path, body) => { calls.push({ path, body }); return Promise.resolve(response(path.includes(ID_A) ? ID_A : ID_B)) }
  await lookupCandidatePrice({ runId: 'run-1', candidateId: ID_A, post })
  await lookupCandidatePrice({ runId: 'run-1', candidateId: ID_B, post })
  assert.equal(calls.length, 2)
  assert.equal(calls.filter(call => call.path.includes(ID_A)).length, 1)
  assert.equal(calls.filter(call => call.path.includes(ID_B)).length, 1)
  for (const call of calls) {
    const ids = [ID_A, ID_B].filter(id => call.path.includes(id) || JSON.stringify(call.body).includes(id))
    assert.equal(ids.length, 1, '一次请求只允许涉及一个候选')
  }
})

test('点第二次走缓存：请求体不带 force，界面上说清"本次没有新增检索"', async () => {
  // 替身后端：第一次真查（有花费），第二次命中已有结果（spent 三个零、cached=true）。
  let hit = 0
  const calls = []
  const post = (path, body) => {
    calls.push({ path, body })
    hit += 1
    return Promise.resolve(hit === 1
      ? response(ID_A)
      : response(ID_A, { cached: true, spent: { search: 0, fetch: 0, model: 0 } }))
  }
  const first = await lookupCandidatePrice({ runId: 'run-1', candidateId: ID_A, post })
  const second = await lookupCandidatePrice({ runId: 'run-1', candidateId: ID_A, post })
  assert.equal(calls.length, 2)
  for (const call of calls) {
    assert.deepEqual(call.body, {}, '不传 force 才会命中已有结果；传了就变成重复花钱')
    assert.equal(call.path, `/research-runs/run-1/alternatives/${ID_A}/price`)
  }
  assert.equal(first.cached, false)
  assert.equal(second.cached, true)
  const firstView = priceLookupView({ prices: [] }, first)
  const secondView = priceLookupView({ prices: [] }, second)
  assert.match(firstView.note, /本次新增：检索 3 次、读取来源 6 个、模型调用 1 次/)
  assert.match(secondView.note, /没有新增检索/)
  assert.notEqual(firstView.note, secondView.note)
})

test('响应里的候选不是点的那个时，宁可什么都不更新', async () => {
  assert.equal(priceLookupResult(response(ID_B), ID_A), null)
  await assert.rejects(
    () => lookupCandidatePrice({ runId: 'run-1', candidateId: ID_A, post: () => Promise.resolve(response(ID_B)) }),
    /不是这个候选的价格/)
})

test('只刷新被点的那一张卡：别的候选条目按引用原样保留', () => {
  const before = { [ID_A]: { label: '旧的 A' }, [ID_B]: { label: 'B' } }
  const after = applyPriceLookup(before, ID_A, { label: '新的 A' })
  assert.equal(after[ID_A].label, '新的 A')
  assert.equal(after[ID_B], before[ID_B], '别的候选必须按引用原样保留')
  assert.equal(Object.keys(after).length, 2)
  assert.equal(applyPriceLookup(before, '', { label: 'x' }), before)
})

test('其它候选的按钮保持可用：进行中只锁被点的那一个', () => {
  const pending = beginLookup({}, ID_A)
  const busy = priceButtonState({ candidateId: ID_A, pending })
  const other = priceButtonState({ candidateId: ID_B, pending })
  assert.equal(busy.disabled, true)
  assert.equal(busy.label, '正在查这个候选的价格')
  assert.equal(other.disabled, false, '用户改主意时，另一个候选必须还能点')
  assert.equal(other.label, '查这个的价格')
  const idle = priceButtonState({ candidateId: ID_A, pending: endLookup(pending, ID_A) })
  assert.equal(idle.disabled, false)
  // 后端说这个候选还没核验出结论（响应里的 status='pending'）时也先别点。
  assert.equal(priceButtonState({ candidateId: ID_A, status: 'pending' }).disabled, true)
})

test('没有候选编号的老数据不给按钮，也不退回数组下标', () => {
  const state = priceButtonState({ candidateId: '', pending: {} })
  assert.equal(state.disabled, true)
  assert.equal(state.label, '查这个的价格')
  assert.match(state.hint, /候选编号/)
})

test('"没查"与"查了没有"在界面文案上是两件事', () => {
  const notAttempted = candidatePriceView({ price_lookup: { status: 'not_attempted', reason_code: 'duplicate_model' }, prices: [] })
  const readWithout = candidatePriceView({ price_lookup: { status: 'read_without_price', reason_code: null }, prices: [] })
  const notRequested = candidatePriceView({ price_lookup: { status: 'not_requested', reason_code: 'not_requested' }, prices: [] })
  const refused = candidatePriceView({ price_lookup: { status: 'refused' }, prices: [] })
  const priced = candidatePriceView({ price_lookup: { status: 'priced', price_count: 2 }, prices: [priceRecord, priceRecord] })
  assert.equal(notAttempted.family, '没查')
  assert.equal(notRequested.family, '没查')
  assert.match(notAttempted.label, /^没查：/)
  assert.match(notAttempted.note, /只查一次/)
  assert.match(notRequested.label, /点了“查这个的价格”才会去查/)
  assert.equal(readWithout.family, '查了没有')
  assert.match(readWithout.label, /^查了没有：/)
  assert.equal(refused.family, '查到了但没有采用')
  assert.ok(!refused.label.includes('查了没有'), '有来源但没被采信，不能与"查了没有"混成一句')
  assert.equal(priced.family, '查到了')
  assert.match(priced.label, /已查到这个候选的成交价/)
  // 四个族名两两不同：界面上必须能看出"没花钱去查"和"查了没结果"的区别。
  assert.equal(new Set([notAttempted.family, readWithout.family, refused.family, priced.family]).size, 4)
})

test('页面上不出现内部字段名、状态码与网址', () => {
  const cases = [
    { status: 'priced', price_count: 1 },
    { status: 'refused', reason_code: null },
    { status: 'read_without_price' },
    { status: 'sources_not_read' },
    { status: 'no_price_source' },
    { status: 'not_attempted', reason_code: 'retained_from_previous_round' },
    { status: 'not_attempted', reason_code: 'lead_below_requirement' },
    { status: 'not_attempted', reason_code: 'source_not_usable' },
    { status: 'not_attempted', reason_code: 'verification_limit' },
    { status: 'not_attempted', reason_code: 'no_sources_found' },
    { status: 'not_attempted', reason_code: 'scan_not_recorded' },
    { status: 'not_requested', reason_code: 'not_requested' },
    { status: 'unrecorded' },
    { status: '某个没见过的码', reason_code: 'something_new' },
    {},
  ]
  for (const lookup of cases) {
    const view = candidatePriceView({ price_lookup: lookup, prices: lookup.status === 'priced' ? [priceRecord] : [] })
    const shown = [view.label, view.note].join(' ')
    assert.ok(!/[a-z]+_[a-z]+/.test(shown), `出现了内部字段名：${shown}`)
    assert.ok(!shown.includes('http'), `出现了网址：${shown}`)
    assert.ok(!/status|reason_code|candidate_id|price_lookup/.test(shown), `出现了字段名：${shown}`)
    assert.ok(view.label.length > 0)
  }
  // 认不出来的状态不许猜成"没查"或"查了没有"——那两句都带事实含义（花没花钱）。
  const unknown = candidatePriceView({ price_lookup: { status: '某个没见过的码', reason_code: 'something_new' }, prices: [] })
  assert.equal(unknown.family, '')
  assert.match(unknown.label, /没有记录清楚/)
})

test('没查过时就用候选自己的留痕；查过之后这张卡改用这次的结果', () => {
  const candidate = { model: 'H1650K', prices: [], price_lookup: { status: 'read_without_price', reason_code: null } }
  const stored = priceLookupView(candidate)
  assert.match(stored.label, /^查了没有：/)
  assert.equal(stored.cached, null)
  const after = priceLookupView(candidate, priceLookupResult(response(ID_A, { detail: { status: 'priced' }, prices: [priceRecord] }), ID_A))
  assert.equal(after.prices.length, 1)
  assert.match(after.label, /已查到这个候选的成交价/)
  const pending = priceLookupView(candidate, priceLookupResult(response(ID_A, { status: 'pending' }), ID_A))
  assert.match(pending.label, /还没核验出结论/)
  assert.equal(pending.prices.length, 0, '没查成时不许显示任何金额')
  const failed = priceLookupView(candidate, priceLookupResult(response(ID_A, { status: 'failed' }), ID_A))
  assert.match(failed.label, /没有完成/)
})

test('缺字段、空响应都不抛异常', async () => {
  assert.equal(priceLookupResult(null, ID_A), null)
  assert.deepEqual(candidatePriceView({}).prices, [])
  await assert.rejects(() => lookupCandidatePrice({ runId: 'run-1', candidateId: '', post: () => Promise.resolve({}) }), /没有编号/)
  await assert.rejects(() => lookupCandidatePrice({ runId: 'run-1', candidateId: ID_A }), /没有接上/)
  assert.equal(priceButtonState().disabled, true)
  assert.deepEqual(endLookup({}, ID_A), {})
})
